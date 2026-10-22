"""
VPS Panel — Ana Uygulama (FastAPI)
"""
import os, secrets, subprocess, json, sqlite3, bcrypt, psutil, re, shlex
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Form, Depends, Response, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

# ── Yollar ─────────────────────────────────────────────────
BASE_DIR     = Path(__file__).parent
DB_PATH      = BASE_DIR / "panel.db"
SCRIPTS_DIR  = BASE_DIR / "scripts"
NGINX_AVAIL  = Path('/etc/nginx/sites-available/vps-users')
NGINX_ENAB   = Path('/etc/nginx/sites-enabled/vps-users')
SSL_DIR      = Path('/etc/letsencrypt/live')

# ── Ortam Değişkenleri ──────────────────────────────────────
ADMIN_EMAIL  = os.environ.get("PANEL_EMAIL", "admin@example.com")

# ── Uygulama ───────────────────────────────────────────────
app = FastAPI(title="VPS Panel", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# ── Oturum deposu (bellek içi) ─────────────────────────────
_sessions: dict[str, dict] = {}

# ══════════════════════════════════════════════════════════
#  VERİTABANI
# ══════════════════════════════════════════════════════════
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS admins (
            id            INTEGER PRIMARY KEY,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at    TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            port_prefix   INTEGER UNIQUE NOT NULL,
            main_domain   TEXT DEFAULT '',
            status        TEXT DEFAULT 'active',
            created_at    TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS domains (
            id            INTEGER PRIMARY KEY,
            domain        TEXT UNIQUE NOT NULL,
            user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            target_port   INTEGER NOT NULL,
            ssl_enabled   INTEGER DEFAULT 0,
            created_at    TEXT DEFAULT (datetime('now'))
        );
    """)
    db.commit()

    # İlk admin oluştur
    admin_user = os.environ.get("PANEL_ADMIN", "admin")
    admin_pass = os.environ.get("PANEL_PASSWORD", "changeme123!")
    row = db.execute("SELECT id FROM admins WHERE username=?", (admin_user,)).fetchone()
    if not row:
        hashed = bcrypt.hashpw(admin_pass.encode(), bcrypt.gensalt()).decode()
        db.execute("INSERT INTO admins (username, password_hash) VALUES (?,?)",
                   (admin_user, hashed))
        db.commit()
    db.close()

@app.on_event("startup")
def startup():
    init_db()
    # Mevcut DB'ye main_domain kolonu ekle (yoksa)
    db = get_db()
    try:
        db.execute("ALTER TABLE users ADD COLUMN main_domain TEXT DEFAULT ''")
        db.commit()
    except Exception:
        pass  # Zaten var
    finally:
        db.close()

# ══════════════════════════════════════════════════════════
#  YARDIMCI FONKSİYONLAR
# ══════════════════════════════════════════════════════════
def session_get(request: Request) -> Optional[dict]:
    sid = request.cookies.get("sid")
    return _sessions.get(sid) if sid else None

def require_login(request: Request) -> dict:
    s = session_get(request)
    if not s:
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    return s

def next_port_prefix(db) -> int:
    """10'dan başlayarak kullanılmamış port prefix bul."""
    used = {r["port_prefix"] for r in db.execute("SELECT port_prefix FROM users").fetchall()}
    for p in range(10, 100):
        if p not in used:
            return p
    raise ValueError("Kullanılabilir port prefix kalmadı (max 90 kullanıcı)")

def run(cmd: list[str], check=True) -> tuple[int, str, str]:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.returncode, r.stdout.strip(), r.stderr.strip()

def nginx_reload():
    run(["nginx", "-t"])
    run(["systemctl", "reload", "nginx"])

def write_nginx_domain(domain: str, target_port: int, ssl: bool = False):
    """Domain için Nginx reverse proxy config yaz."""
    if ssl and (SSL_DIR / domain / "fullchain.pem").exists():
        ssl_block = f"""
    listen 443 ssl;
    ssl_certificate     {SSL_DIR}/{domain}/fullchain.pem;
    ssl_certificate_key {SSL_DIR}/{domain}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;"""
        http_block = f"""
server {{
    listen 80;
    server_name {domain};
    return 301 https://$host$request_uri;
}}"""
    else:
        ssl_block = "\n    listen 80;"
        http_block = ""

    config = f"""{http_block}
server {{{ssl_block}
    server_name {domain};
    client_max_body_size 100M;

    location / {{
        proxy_pass         http://127.0.0.1:{target_port};
        proxy_http_version 1.1;
        proxy_set_header   Upgrade           $http_upgrade;
        proxy_set_header   Connection        'upgrade';
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
    }}
}}
"""
    conf_path = NGINX_AVAIL / f"{domain}.conf"
    conf_path.write_text(config)
    link = NGINX_ENAB / f"{domain}.conf"
    if not link.exists():
        link.symlink_to(conf_path)
    nginx_reload()

def remove_nginx_domain(domain: str):
    for p in [NGINX_ENAB / f"{domain}.conf", NGINX_AVAIL / f"{domain}.conf"]:
        p.unlink(missing_ok=True)
    try:
        nginx_reload()
    except Exception:
        pass

def certbot_issue(domain: str):
    """Let's Encrypt sertifikası al; certbot nginx config'ini otomatik günceller."""
    rc, out, err = run([
        "certbot", "--nginx",
        "-d", domain,
        "--non-interactive",
        "--agree-tos",
        "-m", ADMIN_EMAIL,
        "--redirect",          # HTTP → HTTPS yönlendirmesini otomatik ekle
    ], check=False)
    if rc != 0:
        raise RuntimeError(f"Certbot hatası: {err or out}")

# ══════════════════════════════════════════════════════════
#  AUTH ROTASLARI
# ══════════════════════════════════════════════════════════
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if session_get(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": None})

@app.post("/login", response_class=HTMLResponse)
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    db = get_db()
    row = db.execute("SELECT * FROM admins WHERE username=?", (username,)).fetchone()
    db.close()
    if row and bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
        sid = secrets.token_hex(32)
        _sessions[sid] = {"admin": username}
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie("sid", sid, httponly=True, samesite="lax", max_age=86400*7)
        return resp
    return templates.TemplateResponse(request, "login.html",
        {"error": "Kullanıcı adı veya şifre hatalı"})

@app.get("/logout")
async def logout(request: Request):
    sid = request.cookies.get("sid")
    _sessions.pop(sid, None)
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("sid")
    return resp

# ══════════════════════════════════════════════════════════
#  DASHBOARD
# ══════════════════════════════════════════════════════════
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session=Depends(require_login)):
    db = get_db()
    users   = db.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    domains = db.execute(
        "SELECT d.*, u.username FROM domains d JOIN users u ON d.user_id=u.id ORDER BY d.created_at DESC"
    ).fetchall()
    db.close()

    cpu  = psutil.cpu_percent(interval=0.5)
    mem  = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return templates.TemplateResponse(request, "dashboard.html", {
        "admin":   session["admin"],
        "users":   users,
        "domains": domains,
        "cpu":     cpu,
        "mem_used":  round(mem.used / 1024**3, 1),
        "mem_total": round(mem.total / 1024**3, 1),
        "mem_pct":   mem.percent,
        "disk_used":  round(disk.used / 1024**3, 1),
        "disk_total": round(disk.total / 1024**3, 1),
        "disk_pct":   disk.percent,
    })

# ══════════════════════════════════════════════════════════
#  KULLANICI YÖNETİMİ
# ══════════════════════════════════════════════════════════
@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, session=Depends(require_login),
                     msg: str = "", err: str = ""):
    db = get_db()
    users = db.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    db.close()
    return templates.TemplateResponse(request, "users.html", {
        "admin": session["admin"],
        "users": users, "msg": msg, "err": err,
    })

@app.post("/users/create")
async def user_create(request: Request, session=Depends(require_login),
                      username: str = Form(...), password: str = Form(...),
                      main_domain: str = Form(...)):
    # Validasyonlar
    if not re.match(r'^[a-z][a-z0-9_]{2,30}$', username):
        return RedirectResponse("/users?err=Geçersiz+kullanıcı+adı+(küçük+harf,+3-31+karakter)", status_code=302)
    main_domain = main_domain.strip().lower()
    if not re.match(r'^[a-z0-9][a-z0-9\-\.]{1,250}[a-z0-9]$', main_domain):
        return RedirectResponse("/users?err=Geçersiz+domain+adı", status_code=302)

    db = get_db()
    try:
        # Domain başka kullanıcıda kayıtlı mı?
        if db.execute("SELECT id FROM domains WHERE domain=?", (main_domain,)).fetchone():
            return RedirectResponse(f"/users?err={main_domain}+zaten+başka+bir+kullanıcıya+ait", status_code=302)

        prefix = next_port_prefix(db)
        pwd_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        port_start = prefix * 1000

        # DB'ye kullanıcı ekle
        cur = db.execute(
            "INSERT INTO users (username, password_hash, port_prefix, main_domain) VALUES (?,?,?,?)",
            (username, pwd_hash, prefix, main_domain)
        )
        user_id = cur.lastrowid

        # Önce sistem kullanıcısını oluştur
        script = SCRIPTS_DIR / "create_user.sh"
        run(["bash", str(script), username, password, str(port_start)])

        # Domain'i DB'ye ekle ve nginx'e yaz (varsayılan port: prefix*1000+80)
        default_port = port_start + 80
        db.execute(
            "INSERT INTO domains (domain, user_id, target_port) VALUES (?,?,?)",
            (main_domain, user_id, default_port)
        )
        db.commit()
        write_nginx_domain(main_domain, default_port, ssl=False)

        return RedirectResponse(
            f"/users?msg=Kullanıcı+{username}+oluşturuldu+(Port:+{port_start}-{port_start+999},+Domain:+{main_domain})",
            status_code=302)
    except Exception as e:
        db.rollback()
        return RedirectResponse(f"/users?err={str(e)[:120]}", status_code=302)
    finally:
        db.close()

@app.post("/users/delete")
async def user_delete(request: Request, session=Depends(require_login),
                      user_id: int = Form(...)):
    db = get_db()
    try:
        row = db.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            return RedirectResponse("/users?err=Kullanıcı+bulunamadı", status_code=302)
        username = row["username"]
        # Domainlerini de temizle
        doms = db.execute("SELECT domain FROM domains WHERE user_id=?", (user_id,)).fetchall()
        for d in doms:
            remove_nginx_domain(d["domain"])
        db.execute("DELETE FROM users WHERE id=?", (user_id,))
        db.commit()
        # Linux kullanıcısını ve Docker rootless'ı sil
        script = SCRIPTS_DIR / "delete_user.sh"
        run(["bash", str(script), username], check=False)
        return RedirectResponse(f"/users?msg=Kullanıcı+{username}+silindi", status_code=302)
    finally:
        db.close()

@app.post("/users/toggle")
async def user_toggle(request: Request, session=Depends(require_login),
                      user_id: int = Form(...)):
    db = get_db()
    try:
        row = db.execute("SELECT status, username FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            return RedirectResponse("/users?err=Kullanıcı+bulunamadı", status_code=302)
        new_status = "inactive" if row["status"] == "active" else "active"
        username = row["username"]
        db.execute("UPDATE users SET status=? WHERE id=?", (new_status, user_id))
        db.commit()

        # SSH erişimini aç/kapat
        action = "unlock" if new_status == "active" else "lock"
        run(["passwd", f"--{action}", username], check=False)

        # Kullanıcının tüm domain nginx config'lerini aç/kapat
        domains = db.execute("SELECT domain FROM domains WHERE user_id=?", (user_id,)).fetchall()
        for d in domains:
            link = NGINX_ENAB / f"{d['domain']}.conf"
            src  = NGINX_AVAIL / f"{d['domain']}.conf"
            if new_status == "inactive":
                link.unlink(missing_ok=True)   # symlink sil → nginx domain'i görmez
            else:
                if src.exists() and not link.exists():
                    link.symlink_to(src)        # symlink geri ekle

        # Docker rootless servisini durdur/başlat
        uid_r = subprocess.run(["id", "-u", username], capture_output=True, text=True)
        if uid_r.returncode == 0:
            uid = uid_r.stdout.strip()
            docker_action = "start" if new_status == "active" else "stop"
            subprocess.run([
                "runuser", "-l", username, "-c",
                f"export XDG_RUNTIME_DIR=/run/user/{uid}; "
                f"systemctl --user {docker_action} docker.service 2>/dev/null || true"
            ], capture_output=True)

        try:
            nginx_reload()
        except Exception:
            pass

        durum = "pasif" if new_status == "inactive" else "aktif"
        return RedirectResponse(f"/users?msg=Kullanıcı+{username}+{durum}+yapıldı+(web+erişimi+{'kapatıldı' if new_status == 'inactive' else 'açıldı'})", status_code=302)
    finally:
        db.close()

# ══════════════════════════════════════════════════════════
#  DOMAIN YÖNETİMİ
# ══════════════════════════════════════════════════════════
@app.get("/domains", response_class=HTMLResponse)
async def domains_page(request: Request, session=Depends(require_login),
                       msg: str = "", err: str = ""):
    db = get_db()
    domains = db.execute(
        "SELECT d.*, u.username, u.port_prefix FROM domains d JOIN users u ON d.user_id=u.id ORDER BY d.created_at DESC"
    ).fetchall()
    users = db.execute("SELECT id, username, port_prefix FROM users WHERE status='active' ORDER BY username").fetchall()
    db.close()
    return templates.TemplateResponse(request, "domains.html", {
        "admin": session["admin"],
        "domains": domains, "users": users, "msg": msg, "err": err,
    })

@app.post("/domains/create")
async def domain_create(request: Request, session=Depends(require_login),
                        domain: str = Form(...), user_id: int = Form(...),
                        target_port: int = Form(...), get_ssl: bool = Form(False)):
    domain = domain.strip().lower()
    if not re.match(r'^[a-z0-9][a-z0-9\-\.]{1,250}[a-z0-9]$', domain):
        return RedirectResponse("/domains?err=Geçersiz+domain+adı", status_code=302)
    db = get_db()
    try:
        # Kullanıcının port prefix'ini kontrol et
        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            return RedirectResponse("/domains?err=Kullanıcı+bulunamadı", status_code=302)
        prefix = user["port_prefix"]
        port_min, port_max = prefix * 1000, prefix * 1000 + 999
        if not (port_min <= target_port <= port_max):
            return RedirectResponse(
                f"/domains?err=Port+{target_port}+bu+kullanıcının+bloğu+dışında+({port_min}-{port_max})",
                status_code=302)
        db.execute(
            "INSERT INTO domains (domain, user_id, target_port) VALUES (?,?,?)",
            (domain, user_id, target_port)
        )
        db.commit()
        # 1) HTTP nginx config yaz (certbot HTTP-01 challenge için gerekli)
        write_nginx_domain(domain, target_port, ssl=False)
        ssl_ok = False
        if get_ssl:
            try:
                # 2) certbot --nginx: sertifikayı alır + nginx config'e SSL ekler
                certbot_issue(domain)
                ssl_ok = True
                db.execute("UPDATE domains SET ssl_enabled=1 WHERE domain=?", (domain,))
                db.commit()
            except RuntimeError as ce:
                # SSL başarısız → HTTP ile devam et, hata mesajı göster
                return RedirectResponse(
                    f"/domains?msg=Domain+{domain}+eklendi+(SSL+hatası:+{str(ce)[:100]})",
                    status_code=302)
        suffix = "+(HTTPS+aktif)" if ssl_ok else ""
        return RedirectResponse(f"/domains?msg=Domain+{domain}+eklendi{suffix}", status_code=302)
    except Exception as e:
        db.rollback()
        return RedirectResponse(f"/domains?err={str(e)[:150]}", status_code=302)
    finally:
        db.close()

@app.post("/domains/ssl")
async def domain_ssl(request: Request, session=Depends(require_login),
                     domain_id: int = Form(...)):
    """Mevcut bir domain için sonradan SSL sertifikası al."""
    db = get_db()
    try:
        row = db.execute("SELECT domain, target_port FROM domains WHERE id=?", (domain_id,)).fetchone()
        if not row:
            return RedirectResponse("/domains?err=Domain+bulunamadı", status_code=302)
        domain = row["domain"]
        target_port = row["target_port"]
        try:
            certbot_issue(domain)
            db.execute("UPDATE domains SET ssl_enabled=1 WHERE id=?", (domain_id,))
            db.commit()
            return RedirectResponse(f"/domains?msg={domain}+için+SSL+sertifikası+alındı", status_code=302)
        except RuntimeError as ce:
            return RedirectResponse(f"/domains?err=SSL+hatası:+{str(ce)[:120]}", status_code=302)
    finally:
        db.close()

@app.post("/domains/ssl-renew")
async def domain_ssl_renew(request: Request, session=Depends(require_login),
                           domain_id: int = Form(...)):
    """Mevcut SSL sertifikasını yenile."""
    db = get_db()
    try:
        row = db.execute("SELECT domain FROM domains WHERE id=?", (domain_id,)).fetchone()
        if not row:
            return RedirectResponse("/domains?err=Domain+bulunamadı", status_code=302)
        domain = row["domain"]
        rc, out, err = run([
            "certbot", "renew",
            "--cert-name", domain,
            "--force-renewal",
            "--non-interactive",
        ], check=False)
        if rc != 0:
            return RedirectResponse(f"/domains?err=Yenileme+hatası:+{(err or out)[:120]}", status_code=302)
        nginx_reload()
        return RedirectResponse(f"/domains?msg={domain}+SSL+sertifikası+yenilendi", status_code=302)
    finally:
        db.close()

@app.post("/domains/delete")
async def domain_delete(request: Request, session=Depends(require_login),
                        domain_id: int = Form(...)):
    db = get_db()
    try:
        row = db.execute("SELECT domain FROM domains WHERE id=?", (domain_id,)).fetchone()
        if not row:
            return RedirectResponse("/domains?err=Domain+bulunamadı", status_code=302)
        remove_nginx_domain(row["domain"])
        db.execute("DELETE FROM domains WHERE id=?", (domain_id,))
        db.commit()
        return RedirectResponse(f"/domains?msg=Domain+{row['domain']}+silindi", status_code=302)
    finally:
        db.close()

# ══════════════════════════════════════════════════════════
#  AYARLAR
# ══════════════════════════════════════════════════════════
PANEL_NGINX_AVAIL = Path('/etc/nginx/sites-available')
PANEL_NGINX_ENAB  = Path('/etc/nginx/sites-enabled')
PANEL_PORT        = int(os.environ.get("PANEL_PORT_INTERNAL", "18765"))
SETTINGS_FILE     = BASE_DIR / "settings.json"

def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except Exception:
            pass
    return {}

def save_settings(data: dict):
    SETTINGS_FILE.write_text(json.dumps(data, indent=2))

def get_server_ip() -> str:
    try:
        r = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3)
        return r.stdout.strip().split()[0]
    except Exception:
        return "?"

def write_nginx_panel_domain(domain: str, internal_port: int):
    """Panel için nginx config yaz (sites-available kökünde)."""
    config = f"""server {{
    listen 80;
    server_name {domain};
    client_max_body_size 50M;

    location / {{
        proxy_pass         http://127.0.0.1:{internal_port};
        proxy_http_version 1.1;
        proxy_set_header   Upgrade           $http_upgrade;
        proxy_set_header   Connection        'upgrade';
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
    }}
}}
"""
    conf_path = PANEL_NGINX_AVAIL / f"{domain}.conf"
    conf_path.write_text(config)
    link = PANEL_NGINX_ENAB / f"{domain}.conf"
    if not link.exists():
        link.symlink_to(conf_path)
    nginx_reload()

def remove_nginx_panel_domain(domain: str):
    for p in [PANEL_NGINX_ENAB / f"{domain}.conf",
              PANEL_NGINX_AVAIL / f"{domain}.conf"]:
        p.unlink(missing_ok=True)
    try:
        nginx_reload()
    except Exception:
        pass

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, session=Depends(require_login),
                        msg: str = "", err: str = ""):
    cfg = load_settings()
    panel_domain = cfg.get("panel_domain", "")
    ssl_ok = False
    if panel_domain:
        ssl_ok = (SSL_DIR / panel_domain / "fullchain.pem").exists()
    return templates.TemplateResponse(request, "settings.html", {
        "admin":           session["admin"],
        "server_ip":       get_server_ip(),
        "panel_port":      os.environ.get("PANEL_PORT", "8765"),
        "panel_domain":    panel_domain,
        "panel_domain_ssl": ssl_ok,
        "msg": msg,
        "err": err,
    })

@app.post("/settings/domain")
async def settings_domain(request: Request, session=Depends(require_login),
                          domain: str = Form(...), get_ssl: bool = Form(False)):
    domain = domain.strip().lower()
    if not re.match(r'^[a-z0-9][a-z0-9\-\.]{1,250}[a-z0-9]$', domain):
        return RedirectResponse("/settings?err=Geçersiz+domain+adı", status_code=302)
    cfg = load_settings()
    old_domain = cfg.get("panel_domain", "")
    # Eski domain config'ini kaldır
    if old_domain and old_domain != domain:
        remove_nginx_panel_domain(old_domain)
    try:
        write_nginx_panel_domain(domain, PANEL_PORT)
        cfg["panel_domain"] = domain
        save_settings(cfg)
    except Exception as e:
        return RedirectResponse(f"/settings?err=Nginx+hatası:+{str(e)[:120]}", status_code=302)
    if get_ssl:
        try:
            certbot_issue(domain)
        except RuntimeError as ce:
            return RedirectResponse(
                f"/settings?msg=Domain+ayarlandı+(SSL+hatası:+{str(ce)[:100]})",
                status_code=302)
        return RedirectResponse(f"/settings?msg=Panel+domain+ayarlandı+ve+SSL+alındı+(https://{domain})", status_code=302)
    return RedirectResponse(f"/settings?msg=Panel+domain+ayarlandı+(http://{domain})", status_code=302)

@app.post("/settings/domain-remove")
async def settings_domain_remove(request: Request, session=Depends(require_login)):
    cfg = load_settings()
    domain = cfg.pop("panel_domain", "")
    if domain:
        remove_nginx_panel_domain(domain)
    save_settings(cfg)
    return RedirectResponse("/settings?msg=Panel+domain+ayarı+kaldırıldı", status_code=302)

# ══════════════════════════════════════════════════════════
#  API — Sistem Durumu (AJAX için)
# ══════════════════════════════════════════════════════════
@app.get("/api/stats")
async def api_stats(session=Depends(require_login)):
    cpu  = psutil.cpu_percent(interval=0.3)
    mem  = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return {
        "cpu": cpu,
        "mem_pct": mem.percent,
        "mem_used_gb": round(mem.used / 1024**3, 1),
        "mem_total_gb": round(mem.total / 1024**3, 1),
        "disk_pct": disk.percent,
        "disk_used_gb": round(disk.used / 1024**3, 1),
        "disk_total_gb": round(disk.total / 1024**3, 1),
    }

@app.get("/api/user/{username}/containers")
async def api_user_containers(username: str, session=Depends(require_login)):
    """Kullanıcıya ait çalışan Docker containerları listele."""
    try:
        # Rootless Docker DBUS_SESSION_BUS_ADDRESS gerektirir
        env = os.environ.copy()
        uid_result = subprocess.run(["id", "-u", username], capture_output=True, text=True)
        if uid_result.returncode != 0:
            return {"containers": [], "error": "Kullanıcı bulunamadı"}
        uid = uid_result.stdout.strip()
        env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
        env["DOCKER_HOST"] = f"unix:///run/user/{uid}/docker.sock"
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}"],
            capture_output=True, text=True, env=env, timeout=5
        )
        containers = []
        for line in result.stdout.strip().splitlines():
            if line:
                parts = line.split("\t")
                containers.append({
                    "name": parts[0] if len(parts) > 0 else "",
                    "status": parts[1] if len(parts) > 1 else "",
                    "ports": parts[2] if len(parts) > 2 else "",
                })
        return {"containers": containers}
    except Exception as e:
        return {"containers": [], "error": str(e)}
