"""
VPS Panel — Ana Uygulama (FastAPI)
"""
import os, secrets, subprocess, json, sqlite3, bcrypt, psutil, re, shlex, shutil
import base64, hashlib, pwd, tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

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
        CREATE TABLE IF NOT EXISTS activity_log (
            id            INTEGER PRIMARY KEY,
            created_at    TEXT DEFAULT (datetime('now')),
            admin         TEXT DEFAULT '',
            action        TEXT NOT NULL,
            target        TEXT DEFAULT '',
            detail        TEXT DEFAULT '',
            status        TEXT DEFAULT 'ok',
            ip            TEXT DEFAULT ''
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

# ── Aktivite kayıtları ─────────────────────────────────────
LOG_KEEP      = 10000   # en son bu kadar kayıt tutulur
LOG_PAGE_SIZE = 50
LOG_CATEGORIES = {"auth": "Oturum", "user": "Kullanıcı", "ssh": "SSH Anahtarı",
                  "domain": "Domain", "ssl": "SSL", "nginx": "Nginx",
                  "settings": "Ayarlar"}
ACTION_LABELS = {
    "auth.login":            "Giriş",
    "auth.login_failed":     "Başarısız giriş",
    "auth.logout":           "Çıkış",
    "user.create":           "Kullanıcı oluşturma",
    "user.delete":           "Kullanıcı silme",
    "user.toggle":           "Kullanıcı aktif/pasif",
    "ssh.key_add":           "SSH anahtarı ekleme",
    "ssh.key_generate":      "SSH anahtarı üretme",
    "ssh.key_delete":        "SSH anahtarı silme",
    "domain.create":         "Domain ekleme",
    "domain.delete":         "Domain silme",
    "ssl.issue":             "SSL sertifikası alma",
    "ssl.renew":             "SSL yenileme",
    "nginx.save":            "Nginx config kaydetme",
    "nginx.reload":          "Nginx reload",
    "settings.domain":       "Panel domain ayarı",
    "settings.domain_remove": "Panel domain kaldırma",
    "settings.update":       "Panel güncelleme",
}
templates.env.globals["ACTION_LABELS"] = ACTION_LABELS

def client_ip(request: Request) -> str:
    # Uvicorn yalnızca 127.0.0.1'de dinler; X-Real-IP'yi nginx kendisi yazar
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "")

def log_action(request: Request, action: str, target: str = "", detail: str = "",
               status: str = "ok", admin: Optional[str] = None):
    """Aktivite kaydı yaz. Ayrı bağlantı kullanır, bu yüzden çağıran kendi
    yazma işlemini commit/rollback ettikten SONRA çağrılmalıdır (aksi halde
    SQLite kilitlenir). Kayıt yazılamazsa asıl işlemi bozmaz."""
    try:
        if admin is None:
            s = session_get(request)
            admin = s["admin"] if s else ""
        db = get_db()
        try:
            db.execute(
                "INSERT INTO activity_log (admin, action, target, detail, status, ip) "
                "VALUES (?,?,?,?,?,?)",
                (admin, action, target[:200], detail[:500], status, client_ip(request)[:45]))
            db.execute("DELETE FROM activity_log WHERE id <= (SELECT MAX(id) FROM activity_log) - ?",
                       (LOG_KEEP,))
            db.commit()
        finally:
            db.close()
    except Exception:
        pass

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
        log_action(request, "auth.login", admin=username)
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie("sid", sid, httponly=True, samesite="lax", max_age=86400*7)
        return resp
    log_action(request, "auth.login_failed", username, status="error", admin="")
    return templates.TemplateResponse(request, "login.html",
        {"error": "Kullanıcı adı veya şifre hatalı"})

@app.get("/logout")
async def logout(request: Request):
    if session_get(request):
        log_action(request, "auth.logout")
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
    recent_logs = db.execute("SELECT * FROM activity_log ORDER BY id DESC LIMIT 8").fetchall()
    db.close()

    cpu  = psutil.cpu_percent(interval=0.5)
    mem  = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return templates.TemplateResponse(request, "dashboard.html", {
        "admin":   session["admin"],
        "users":   users,
        "domains": domains,
        "recent_logs": recent_logs,
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

        log_action(request, "user.create", username,
                   f"port {port_start}-{port_start+999}, domain {main_domain}")
        return RedirectResponse(
            f"/users?msg=Kullanıcı+{username}+oluşturuldu+(Port:+{port_start}-{port_start+999},+Domain:+{main_domain})",
            status_code=302)
    except Exception as e:
        db.rollback()
        log_action(request, "user.create", username, str(e), status="error")
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
        rc, out, err = run(["bash", str(script), username], check=False)
        detail = ("domainler: " + ", ".join(d["domain"] for d in doms)) if doms else ""
        if rc != 0:
            detail = f"DB'den silindi, sistem kullanıcısı silinemedi: {err or out}"
        log_action(request, "user.delete", username, detail,
                   status="ok" if rc == 0 else "error")
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

        # SSH erişimini aç.
        # Hesabın süresi dolmuşken runuser (aşağıdaki docker adımı) PAM
        # account kontrolüne takılır, bu yüzden açma işlemi en başta yapılır.
        if new_status == "active":
            run(["passwd", "--unlock", username], check=False)
            run(["chage", "-E", "-1", username], check=False)

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

        # SSH erişimini kapat — docker durdurulduktan SONRA.
        # passwd --lock yalnızca parolayı kilitler, authorized_keys ile giriş
        # açık kalırdı; hesabın süresini de doldurarak her iki yolu kapatıyoruz.
        if new_status == "inactive":
            run(["passwd", "--lock", username], check=False)
            run(["chage", "-E", "1", username], check=False)   # 2 Ocak 1970 → süresi dolmuş

        try:
            nginx_reload()
        except Exception:
            pass

        durum = "pasif" if new_status == "inactive" else "aktif"
        log_action(request, "user.toggle", username, f"{row['status']} → {new_status}")
        return RedirectResponse(f"/users?msg=Kullanıcı+{username}+{durum}+yapıldı+(web+erişimi+{'kapatıldı' if new_status == 'inactive' else 'açıldı'})", status_code=302)
    finally:
        db.close()

# ══════════════════════════════════════════════════════════
#  SSH ANAHTAR YÖNETİMİ
# ══════════════════════════════════════════════════════════
# Desteklenen genel anahtar türleri → ekranda gösterilen etiket.
# ssh-dss (DSA) bilerek dışarıda: OpenSSH 7.0'dan beri varsayılan olarak kapalı.
PUBKEY_TYPES = {
    "ssh-ed25519":                        "ED25519",
    "ssh-rsa":                            "RSA",
    "ecdsa-sha2-nistp256":                "ECDSA",
    "ecdsa-sha2-nistp384":                "ECDSA",
    "ecdsa-sha2-nistp521":                "ECDSA",
    "sk-ssh-ed25519@openssh.com":         "ED25519-SK",
    "sk-ecdsa-sha2-nistp256@openssh.com": "ECDSA-SK",
}
RSA_MIN_BITS     = 2048
PUBKEY_MAX_BYTES = 16 * 1024    # yapıştırılan metin için üst sınır
KEYGEN_TYPES     = {"ed25519": [], "rsa": ["-b", "4096"]}
NEWKEY_TTL_SEC   = 600          # üretilen özel anahtarın bellekte kalma süresi

# Üretilen özel anahtarlar: token → kayıt. Diske veya DB'ye hiç yazılmaz,
# bir kez gösterilip silinir (bkz. pop_generated).
_new_keys: dict[str, dict] = {}

def find_user(username: str) -> Optional[sqlite3.Row]:
    db = get_db()
    try:
        return db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    finally:
        db.close()

def ssh_dir(username: str) -> Path:
    return Path(pwd.getpwnam(username).pw_dir) / ".ssh"

def auth_keys_path(username: str) -> Path:
    return ssh_dir(username) / "authorized_keys"

def _ssh_blob_fields(blob: bytes) -> list[bytes]:
    """SSH tel formatı: art arda 4 bayt uzunluk + veri."""
    fields, i = [], 0
    while i + 4 <= len(blob):
        n = int.from_bytes(blob[i:i + 4], "big")
        i += 4
        if n > len(blob) - i:
            raise ValueError("anahtar gövdesi bozuk")
        fields.append(blob[i:i + n])
        i += n
    if i != len(blob) or not fields:
        raise ValueError("anahtar gövdesi bozuk")
    return fields

def _key_bits(ktype: str, fields: list[bytes]) -> int:
    if ktype == "ssh-rsa":
        if len(fields) < 3:                      # tür, üs (e), modül (n)
            raise ValueError("anahtar gövdesi eksik")
        return len(fields[2].lstrip(b"\x00")) * 8
    m = re.search(r"nistp(\d+)", ktype)
    return int(m.group(1)) if m else 256

def parse_pubkey(text: str) -> dict:
    """Genel anahtarı doğrula ve normalize et. Geçersizse ValueError atar."""
    text = text.strip()
    if not text:
        raise ValueError("Genel anahtar boş olamaz")
    if len(text.encode()) > PUBKEY_MAX_BYTES:
        raise ValueError("Genel anahtar çok uzun")
    if "\n" in text or "\r" in text:
        raise ValueError("Tek satırlık bir genel anahtar yapıştırın (id_ed25519.pub içeriği)")

    parts = text.split()
    if len(parts) < 2:
        raise ValueError("Anahtar biçimi hatalı: '<tür> <anahtar> [açıklama]' bekleniyor")
    ktype, b64 = parts[0], parts[1]
    if ktype not in PUBKEY_TYPES:
        if ktype == "ssh-dss":
            raise ValueError("DSA anahtarları desteklenmiyor, ed25519 kullanın")
        raise ValueError(f"Desteklenmeyen anahtar türü: {ktype[:40]} "
                         "(ssh-ed25519, ssh-rsa veya ecdsa-* bekleniyor)")
    try:
        blob = base64.b64decode(b64, validate=True)
    except Exception:
        raise ValueError("Anahtar gövdesi base64 olarak çözülemedi — eksik kopyalanmış olabilir")

    fields = _ssh_blob_fields(blob)
    if fields[0].decode("utf-8", "replace") != ktype:
        raise ValueError("Anahtar gövdesi belirtilen tür ile uyuşmuyor")
    bits = _key_bits(ktype, fields)
    if ktype == "ssh-rsa" and bits < RSA_MIN_BITS:
        raise ValueError(f"RSA anahtarı en az {RSA_MIN_BITS} bit olmalı (bu anahtar {bits} bit)")

    # Açıklama tek satıra indirgenir; görünmeyen karakterler atılır
    comment = "".join(c for c in " ".join(parts[2:]) if c.isprintable())[:120].strip()
    line = f"{ktype} {b64}" + (f" {comment}" if comment else "")
    return {
        "type": ktype, "label": PUBKEY_TYPES[ktype], "bits": bits,
        "comment": comment, "line": line,
        "fingerprint": "SHA256:" + base64.b64encode(
            hashlib.sha256(blob).digest()).decode().rstrip("="),
        "key_id": hashlib.sha256(line.encode()).hexdigest()[:16],
    }

def read_auth_keys(username: str) -> list[dict]:
    """authorized_keys içeriğini listele. Çözümlenemeyen satırlar da
    (silinebilmeleri için) 'raw' olarak döner."""
    path = auth_keys_path(username)
    if not path.exists():
        return []
    keys = []
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            keys.append(parse_pubkey(line))
        except ValueError:
            keys.append({
                "type": "?", "label": "Bilinmiyor", "bits": 0,
                "comment": "", "line": line, "fingerprint": "",
                "key_id": hashlib.sha256(line.encode()).hexdigest()[:16],
            })
    return keys

def write_auth_keys(username: str, keys: list[dict]):
    """authorized_keys'i atomik yaz; sahiplik ve izinleri sshd'nin
    kabul edeceği şekilde ayarla (.ssh 700, authorized_keys 600)."""
    pw   = pwd.getpwnam(username)
    path = auth_keys_path(username)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    os.chown(path.parent, pw.pw_uid, pw.pw_gid)

    tmp = path.with_name(path.name + ".panel-tmp")
    try:
        tmp.write_text("".join(k["line"] + "\n" for k in keys))
        os.chmod(tmp, 0o600)
        os.chown(tmp, pw.pw_uid, pw.pw_gid)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

def generate_keypair(comment: str, ktype: str) -> tuple[str, str]:
    """Yeni anahtar çifti üret → (özel anahtar, genel anahtar).
    Anahtar yalnızca geçici dizinde oluşur, dönüşte diskten silinir."""
    with tempfile.TemporaryDirectory() as td:
        kf = Path(td) / "id"
        run(["ssh-keygen", "-q", "-t", ktype, *KEYGEN_TYPES[ktype],
             "-N", "", "-C", comment, "-f", str(kf)])
        return kf.read_text(), kf.with_name("id.pub").read_text().strip()

def stash_generated(username: str, private: str, key: dict) -> str:
    """Özel anahtarı tek seferlik gösterim için belleğe al, token döndür."""
    now = datetime.now(timezone.utc)
    for tok, rec in list(_new_keys.items()):
        if (now - rec["at"]).total_seconds() > NEWKEY_TTL_SEC:
            _new_keys.pop(tok, None)
    token = secrets.token_urlsafe(24)
    _new_keys[token] = {"username": username, "private": private, "key": key, "at": now}
    return token

def pop_generated(token: str, username: str) -> Optional[dict]:
    """Üretilen anahtarı bir kez döndür ve bellekten sil."""
    rec = _new_keys.pop(token, None) if token else None
    if not rec or rec["username"] != username:
        return None
    if (datetime.now(timezone.utc) - rec["at"]).total_seconds() > NEWKEY_TTL_SEC:
        return None
    return rec

def privkey_filename(username: str, ktype: str) -> str:
    """İndirilen özel anahtar dosyasının adı: 'ahmet_id_ed25519' gibi."""
    return f"{username}_id_{PUBKEY_TYPES.get(ktype, 'key').lower().replace('-', '_')}"

def ssh_redirect(username: str, **params) -> RedirectResponse:
    return RedirectResponse(f"/users/{username}/ssh?{urlencode(params)}", status_code=302)

@app.get("/users/{username}/ssh", response_class=HTMLResponse)
async def ssh_keys_page(request: Request, username: str, session=Depends(require_login),
                        msg: str = "", err: str = "", new: str = ""):
    user = find_user(username)
    if not user:
        return RedirectResponse("/users?err=Kullanıcı+bulunamadı", status_code=302)
    try:
        keys, read_err = read_auth_keys(username), ""
    except Exception as e:
        keys, read_err = [], f"authorized_keys okunamadı: {e}"
    generated = pop_generated(new, username)
    return templates.TemplateResponse(request, "ssh_keys.html", {
        "admin": session["admin"], "user": user, "keys": keys,
        "msg": msg, "err": err or read_err,
        "generated": generated,
        "filename": privkey_filename(username, generated["key"]["type"]) if generated else "",
        "server_ip": get_server_ip(),
        "keys_path": str(auth_keys_path(username)) if not read_err else "",
    })

@app.post("/users/{username}/ssh/add")
async def ssh_key_add(request: Request, username: str, session=Depends(require_login),
                      pubkey: str = Form(...)):
    if not find_user(username):
        return RedirectResponse("/users?err=Kullanıcı+bulunamadı", status_code=302)
    try:
        key = parse_pubkey(pubkey)
        keys = read_auth_keys(username)
        if any(k["key_id"] == key["key_id"] for k in keys):
            return ssh_redirect(username, err="Bu anahtar zaten ekli")
        if key["fingerprint"] and any(k["fingerprint"] == key["fingerprint"] for k in keys):
            return ssh_redirect(username, err="Bu anahtar zaten ekli (açıklaması farklı)")
        write_auth_keys(username, keys + [key])
    except Exception as e:
        log_action(request, "ssh.key_add", username, str(e), status="error")
        return ssh_redirect(username, err=str(e)[:160])
    log_action(request, "ssh.key_add", username,
               f"{key['label']} {key['fingerprint']}" + (f" ({key['comment']})" if key["comment"] else ""))
    return ssh_redirect(username, msg=f"{key['label']} anahtarı eklendi")

@app.post("/users/{username}/ssh/generate")
async def ssh_key_generate(request: Request, username: str, session=Depends(require_login),
                           key_type: str = Form("ed25519"), comment: str = Form("")):
    if not find_user(username):
        return RedirectResponse("/users?err=Kullanıcı+bulunamadı", status_code=302)
    if key_type not in KEYGEN_TYPES:
        return ssh_redirect(username, err="Geçersiz anahtar türü")
    comment = "".join(c for c in comment.strip() if c.isprintable() and c not in '"\\')[:60] \
              or f"{username}@{get_server_ip()}"
    try:
        private, public = generate_keypair(comment, key_type)
        key = parse_pubkey(public)
        keys = read_auth_keys(username)
        write_auth_keys(username, keys + [key])
        token = stash_generated(username, private, key)
    except Exception as e:
        log_action(request, "ssh.key_generate", username, str(e), status="error")
        return ssh_redirect(username, err=str(e)[:160])
    log_action(request, "ssh.key_generate", username,
               f"{key['label']} {key['fingerprint']} ({key['comment']})")
    return ssh_redirect(username, new=token)

@app.post("/users/{username}/ssh/delete")
async def ssh_key_delete(request: Request, username: str, session=Depends(require_login),
                         key_id: str = Form(...)):
    if not find_user(username):
        return RedirectResponse("/users?err=Kullanıcı+bulunamadı", status_code=302)
    try:
        keys = read_auth_keys(username)
        gone = [k for k in keys if k["key_id"] == key_id]
        if not gone:
            return ssh_redirect(username, err="Anahtar bulunamadı (sayfa eski olabilir)")
        write_auth_keys(username, [k for k in keys if k["key_id"] != key_id])
    except Exception as e:
        log_action(request, "ssh.key_delete", username, str(e), status="error")
        return ssh_redirect(username, err=str(e)[:160])
    k = gone[0]
    log_action(request, "ssh.key_delete", username,
               f"{k['label']} {k['fingerprint'] or k['line'][:40]}"
               + (f" ({k['comment']})" if k["comment"] else ""))
    return ssh_redirect(username, msg=f"{k['label']} anahtarı silindi")

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
        log_action(request, "domain.create", domain,
                   f"port {target_port}, kullanıcı {user['username']}")
        ssl_ok = False
        if get_ssl:
            try:
                # 2) certbot --nginx: sertifikayı alır + nginx config'e SSL ekler
                certbot_issue(domain)
                ssl_ok = True
                db.execute("UPDATE domains SET ssl_enabled=1 WHERE domain=?", (domain,))
                db.commit()
                log_action(request, "ssl.issue", domain)
            except RuntimeError as ce:
                # SSL başarısız → HTTP ile devam et, hata mesajı göster
                log_action(request, "ssl.issue", domain, str(ce), status="error")
                return RedirectResponse(
                    f"/domains?msg=Domain+{domain}+eklendi+(SSL+hatası:+{str(ce)[:100]})",
                    status_code=302)
        suffix = "+(HTTPS+aktif)" if ssl_ok else ""
        return RedirectResponse(f"/domains?msg=Domain+{domain}+eklendi{suffix}", status_code=302)
    except Exception as e:
        db.rollback()
        log_action(request, "domain.create", domain, str(e), status="error")
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
            log_action(request, "ssl.issue", domain)
            return RedirectResponse(f"/domains?msg={domain}+için+SSL+sertifikası+alındı", status_code=302)
        except RuntimeError as ce:
            log_action(request, "ssl.issue", domain, str(ce), status="error")
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
            log_action(request, "ssl.renew", domain, err or out, status="error")
            return RedirectResponse(f"/domains?err=Yenileme+hatası:+{(err or out)[:120]}", status_code=302)
        nginx_reload()
        log_action(request, "ssl.renew", domain)
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
        log_action(request, "domain.delete", row["domain"])
        return RedirectResponse(f"/domains?msg=Domain+{row['domain']}+silindi", status_code=302)
    finally:
        db.close()

# ══════════════════════════════════════════════════════════
#  KAYITLAR
# ══════════════════════════════════════════════════════════
@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, session=Depends(require_login),
                    category: str = "", status: str = "", page: int = 1):
    if category not in LOG_CATEGORIES:
        category = ""
    if status not in ("ok", "error"):
        status = ""
    where, params = [], []
    if category:
        where.append("action LIKE ?")
        params.append(f"{category}.%")
    if status:
        where.append("status = ?")
        params.append(status)
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    db = get_db()
    total = db.execute(f"SELECT COUNT(*) FROM activity_log {clause}", params).fetchone()[0]
    pages = max(1, -(-total // LOG_PAGE_SIZE))
    page = min(max(page, 1), pages)
    entries = db.execute(
        f"SELECT * FROM activity_log {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [LOG_PAGE_SIZE, (page - 1) * LOG_PAGE_SIZE]
    ).fetchall()
    db.close()
    return templates.TemplateResponse(request, "logs.html", {
        "admin": session["admin"],
        "entries": entries, "total": total, "page": page, "pages": pages,
        "category": category, "status": status, "categories": LOG_CATEGORIES,
    })

# ══════════════════════════════════════════════════════════
#  NGINX CONFIG EDİTÖRÜ
# ══════════════════════════════════════════════════════════
NGINX_ROOT        = Path('/etc/nginx')
NGINX_BACKUP_DIR  = Path('/var/backups/vps-panel-nginx')
NGINX_BACKUP_KEEP = 20           # dosya başına tutulan yedek sayısı
NGINX_MAX_BYTES   = 512 * 1024   # düzenlenebilir en büyük dosya

def nginx_enabled_targets() -> set:
    """sites-enabled (ve vps-users) altındaki symlink'lerin hedefleri."""
    targets = set()
    for d in (NGINX_ROOT / "sites-enabled", NGINX_ROOT / "sites-enabled" / "vps-users"):
        if d.is_dir():
            targets.update(p.resolve() for p in d.iterdir() if p.is_symlink())
    return targets

def nginx_files() -> list[dict]:
    """Düzenlenebilir config dosyaları. Okuma/yazma yalnızca bu listedeki anahtarlarla
    yapılır; istemciden gelen yol hiçbir zaman doğrudan dosya yolu olarak kullanılmaz."""
    enabled = nginx_enabled_targets()
    found = []

    def add(group: str, path: Path, is_site: bool):
        if (path.name.startswith(".") or path.is_symlink() or not path.is_file()
                or path.stat().st_size > NGINX_MAX_BYTES):
            return
        found.append({
            "key":     path.relative_to(NGINX_ROOT).as_posix(),
            "name":    path.name,
            "group":   group,
            "enabled": (path.resolve() in enabled) if is_site else None,
        })

    add("Ana yapılandırma", NGINX_ROOT / "nginx.conf", False)
    for group, d, pattern, is_site in (
        ("conf.d",               NGINX_ROOT / "conf.d",                        "*.conf", False),
        ("Siteler",              NGINX_ROOT / "sites-available",               "*",      True),
        ("Kullanıcı domainleri", NGINX_ROOT / "sites-available" / "vps-users", "*",      True),
    ):
        if d.is_dir():
            for p in sorted(d.glob(pattern)):
                add(group, p, is_site)
    return found

def nginx_find(key: str) -> Optional[dict]:
    return next((f for f in nginx_files() if f["key"] == key), None)

def nginx_write(path: Path, content: str):
    """Atomik yaz (aynı dizinde geçici dosya + rename); mevcut izinleri korur.
    Geçici ad '.' ile başlar ve '.conf' ile bitmez, bu yüzden nginx include'ları onu yakalamaz."""
    tmp = path.with_name(f".{path.name}.vps-tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)

def nginx_backup_prefix(key: str) -> str:
    return key.replace("/", "__")

def nginx_backups(key: str) -> list[str]:
    """Dosyanın yedek adları, en yeniden eskiye."""
    if not NGINX_BACKUP_DIR.is_dir():
        return []
    pat = re.compile(rf"^{re.escape(nginx_backup_prefix(key))}\.\d{{8}}-\d{{6}}\.bak$")
    return sorted((p.name for p in NGINX_BACKUP_DIR.iterdir() if pat.match(p.name)), reverse=True)

def nginx_backup(key: str, content: str) -> str:
    NGINX_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(NGINX_BACKUP_DIR, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"{nginx_backup_prefix(key)}.{stamp}.bak"
    (NGINX_BACKUP_DIR / name).write_text(content, encoding="utf-8")
    for old in nginx_backups(key)[NGINX_BACKUP_KEEP:]:
        (NGINX_BACKUP_DIR / old).unlink(missing_ok=True)
    return name

def nginx_apply(key: str, content: str) -> tuple[bool, str, str]:
    """İçeriği yaz → nginx -t → reload. nginx -t ya da reload başarısız olursa eski içerik
    geri yazılır. Dönüş: (başarılı, nginx çıktısı, yedek adı)."""
    path = NGINX_ROOT / key
    old = path.read_text(encoding="utf-8")
    nginx_write(path, content)
    reload_failed = False
    try:
        rc, out, err = run(["nginx", "-t"], check=False)
        output = err or out
        if rc == 0:
            rc, out, err = run(["systemctl", "reload", "nginx"], check=False)
            if rc != 0:
                reload_failed, output = True, f"Reload başarısız: {err or out}"
    except Exception:
        nginx_write(path, old)
        raise
    if rc != 0:
        nginx_write(path, old)
        if reload_failed:
            run(["systemctl", "reload", "nginx"], check=False)   # çalışan eski haline dön
        return False, output, ""
    return True, output, nginx_backup(key, old)

def nginx_render(request: Request, session: dict, key: str = "", content: Optional[str] = None,
                 backup: str = "", msg: str = "", err: str = "", output: str = ""):
    files = nginx_files()
    current = next((f for f in files if f["key"] == key), None)
    if key and not current and not err:
        err = "Dosya bulunamadı"
    saved, loaded_backup, backups = None, "", []
    if current:
        try:
            saved = (NGINX_ROOT / key).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            saved = None
            err = err or f"Dosya okunamadı: {e}"
        names = nginx_backups(key)
        backups = [{
            "name": n,
            "iso":  datetime.strptime(n[-19:-4], "%Y%m%d-%H%M%S").strftime("%Y-%m-%dT%H:%M:%SZ"),
        } for n in names]
        if content is None and saved is not None:
            content = saved
            if backup in names:
                content = (NGINX_BACKUP_DIR / backup).read_text(encoding="utf-8")
                loaded_backup = backup
    groups: dict[str, list] = {}
    for f in files:
        groups.setdefault(f["group"], []).append(f)
    _, nginx_state, _ = run(["systemctl", "is-active", "nginx"], check=False)
    return templates.TemplateResponse(request, "nginx.html", {
        "admin": session["admin"],
        "groups": groups, "current": current, "content": content, "saved": saved,
        "backups": backups, "loaded_backup": loaded_backup,
        "nginx_state": nginx_state or "unknown",
        "max_kb": NGINX_MAX_BYTES // 1024,
        "msg": msg, "err": err, "output": output,
    })

def nginx_redirect(key: str, **params) -> RedirectResponse:
    q = {"file": key, **{k: v for k, v in params.items() if v}}
    return RedirectResponse("/nginx?" + urlencode(q), status_code=302)

@app.get("/nginx", response_class=HTMLResponse)
async def nginx_page(request: Request, session=Depends(require_login),
                     file: str = "", backup: str = "", msg: str = "", err: str = ""):
    return nginx_render(request, session, file, backup=backup, msg=msg, err=err)

@app.post("/nginx/save", response_class=HTMLResponse)
async def nginx_save(request: Request, session=Depends(require_login),
                     file: str = Form(...), content: str = Form("")):
    f = nginx_find(file)
    if not f:
        return nginx_redirect("", err="Dosya bulunamadı")
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    if not content.endswith("\n"):
        content += "\n"
    if "\x00" in content or len(content.encode("utf-8")) > NGINX_MAX_BYTES:
        return nginx_render(request, session, file, content=content,
                            err=f"İçerik geçersiz (NUL karakter içeriyor ya da {NGINX_MAX_BYTES // 1024} KB'tan büyük)")
    try:
        old = (NGINX_ROOT / file).read_text(encoding="utf-8")
        if content == old:
            return nginx_redirect(file, msg="Değişiklik yok")
        ok, output, backup_name = nginx_apply(file, content)
    except Exception as e:
        log_action(request, "nginx.save", file, str(e), status="error")
        return nginx_render(request, session, file, content=content, err=f"Kaydedilemedi: {e}")
    if not ok:
        log_action(request, "nginx.save", file, output, status="error")
        return nginx_render(request, session, file, content=content, output=output,
                            err="Kaydedilmedi: nginx -t doğrulaması başarısız. Dosya önceki haline döndürüldü; "
                                "düzenlemeniz aşağıda korunuyor.")
    log_action(request, "nginx.save", file,
               f"{len(old.splitlines())} → {len(content.splitlines())} satır, yedek {backup_name}")
    return nginx_redirect(file, msg="Kaydedildi: nginx -t geçti ve nginx reload edildi")

@app.post("/nginx/test", response_class=HTMLResponse)
async def nginx_test(request: Request, session=Depends(require_login), file: str = Form("")):
    rc, out, err = run(["nginx", "-t"], check=False)
    if rc == 0:
        return nginx_render(request, session, file, msg="Yapılandırma geçerli", output=err or out)
    return nginx_render(request, session, file, err="Yapılandırma hatalı", output=err or out)

@app.post("/nginx/reload", response_class=HTMLResponse)
async def nginx_reload_route(request: Request, session=Depends(require_login), file: str = Form("")):
    rc, out, err = run(["nginx", "-t"], check=False)
    output = err or out
    if rc == 0:
        rc, out, err = run(["systemctl", "reload", "nginx"], check=False)
        output = err or out or output
    if rc != 0:
        log_action(request, "nginx.reload", status="error", detail=output)
        return nginx_render(request, session, file, err="Reload yapılamadı", output=output)
    log_action(request, "nginx.reload")
    return nginx_render(request, session, file, msg="Nginx reload edildi", output=output)

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

# ── Panel güncelleme (deploy.sh: git pull + update.sh) ─────
UPDATE_UNIT   = "vps-panel-update"
UPDATE_LOG    = Path("/var/log/vps-panel-update.log")
REPO_DIR_FILE = BASE_DIR / ".repo_dir"   # setup.sh / update.sh checkout yolunu buraya yazar
UPDATE_TAIL   = 60                       # arayüzde gösterilecek son satır sayısı
ANSI_RE       = re.compile(r"\x1b\[[0-9;]*m")

def update_repo_dir() -> Optional[Path]:
    """deploy.sh'in bulunduğu git checkout'u; kayıtlı değilse ya da bozuksa None."""
    try:
        d = Path(REPO_DIR_FILE.read_text().strip())
    except OSError:
        return None
    return d if (d / "deploy.sh").is_file() and (d / ".git").exists() else None

def update_running() -> bool:
    _, out, _ = run(["systemctl", "is-active", UPDATE_UNIT], check=False)
    return out in ("active", "activating")

def update_log_tail() -> str:
    try:
        lines = UPDATE_LOG.read_text(errors="replace").splitlines()[-UPDATE_TAIL:]
    except OSError:
        return ""
    return ANSI_RE.sub("", "\n".join(lines))

def update_version(repo: Path) -> str:
    _, out, _ = run(["git", "-C", str(repo), "log", "-1", "--format=%h · %s · %cs"], check=False)
    return out

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
    repo = update_repo_dir()
    return templates.TemplateResponse(request, "settings.html", {
        "admin":           session["admin"],
        "server_ip":       get_server_ip(),
        "panel_port":      os.environ.get("PANEL_PORT", "8765"),
        "panel_domain":    panel_domain,
        "panel_domain_ssl": ssl_ok,
        "update_repo":     str(repo) if repo else "",
        "update_version":  update_version(repo) if repo else "",
        "update_running":  update_running(),
        "update_log":      update_log_tail(),
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
        log_action(request, "settings.domain", domain, str(e), status="error")
        return RedirectResponse(f"/settings?err=Nginx+hatası:+{str(e)[:120]}", status_code=302)
    log_action(request, "settings.domain", domain,
               f"eski: {old_domain}" if old_domain and old_domain != domain else "")
    if get_ssl:
        try:
            certbot_issue(domain)
        except RuntimeError as ce:
            log_action(request, "ssl.issue", domain, str(ce), status="error")
            return RedirectResponse(
                f"/settings?msg=Domain+ayarlandı+(SSL+hatası:+{str(ce)[:100]})",
                status_code=302)
        log_action(request, "ssl.issue", domain, "panel domaini")
        return RedirectResponse(f"/settings?msg=Panel+domain+ayarlandı+ve+SSL+alındı+(https://{domain})", status_code=302)
    return RedirectResponse(f"/settings?msg=Panel+domain+ayarlandı+(http://{domain})", status_code=302)

@app.post("/settings/domain-remove")
async def settings_domain_remove(request: Request, session=Depends(require_login)):
    cfg = load_settings()
    domain = cfg.pop("panel_domain", "")
    if domain:
        remove_nginx_panel_domain(domain)
    save_settings(cfg)
    if domain:
        log_action(request, "settings.domain_remove", domain)
    return RedirectResponse("/settings?msg=Panel+domain+ayarı+kaldırıldı", status_code=302)

@app.post("/settings/update")
async def settings_update(request: Request, session=Depends(require_login)):
    """deploy.sh'i systemd-run ile ayrı bir birimde başlat. Script servisi yeniden
    başlattığı için panel sürecinin çocuğu olarak çalışsa kendisi de öldürülürdü."""
    repo = update_repo_dir()
    if not repo:
        return RedirectResponse(
            "/settings?err=Git+deposu+kayıtlı+değil:+sunucuda+depo+dizininde+bir+kez+sudo+bash+update.sh+çalıştırın",
            status_code=302)
    if update_running():
        return RedirectResponse("/settings?msg=Güncelleme+zaten+çalışıyor", status_code=302)
    try:
        UPDATE_LOG.write_text("")
        run(["systemd-run", f"--unit={UPDATE_UNIT}", "--collect", "--no-block",
             "--description=VPS Panel güncelleme",
             "-p", f"StandardOutput=append:{UPDATE_LOG}",
             "-p", f"StandardError=append:{UPDATE_LOG}",
             "/bin/bash", str(repo / "deploy.sh")])
    except Exception as e:
        log_action(request, "settings.update", str(repo), str(e), status="error")
        return RedirectResponse(f"/settings?err=Güncelleme+başlatılamadı:+{str(e)[:120]}", status_code=302)
    log_action(request, "settings.update", str(repo))
    return RedirectResponse("/settings?msg=Güncelleme+başlatıldı", status_code=302)

@app.get("/settings/update/status")
async def settings_update_status(session=Depends(require_login)):
    return {"running": update_running(), "log": update_log_tail()}

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
