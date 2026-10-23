#!/bin/bash
# ============================================================
#  VPS Panel — Uçtan Uca Test (test container'ı içinde çalışır)
#  Kullanım (repo kökünden):
#    docker compose -f docker/compose.yml exec panel-test bash /src/docker/e2e.sh
#
#  Kapsam: kurulum (setup.sh), giriş, kullanıcı ekle / pasifleştir /
#  aktifleştir / sil, domain ekle / sil, Nginx editörü, panel domain ayarı,
#  güncelleme (update.sh) ve setup.sh'in yeniden çalıştırılması.
#  Kapsam dışı: Let's Encrypt (genel DNS ve erişilebilir 80/443 gerekir).
#
#  Ortam değişkenleri:
#    E2E_SKIP_DOCKER=1   rootless Docker adımını (imaj indirir) atla
#    PANEL_PASSWORD      kurulumda kullanılacak admin şifresi
#
#  Test kullanıcıları (e2e_*) panel üzerinden oluşturulup silinir.
#  Temiz bir sonuç için container'ı sıfırdan başlatın: compose down -v
# ============================================================
set -uo pipefail

SRC="${SRC:-/src}"
PANEL_DIR="/opt/vps-panel"
PANEL_PORT="${PANEL_PORT:-8765}"
ADMIN="${PANEL_ADMIN:-admin}"
ADMIN_PASS="${PANEL_PASSWORD:-E2e-Test-Pass-1}"
BASE="https://127.0.0.1:${PANEL_PORT}"

U1=e2e_alice; U1_PASS='Sup3rSecret!alice'; D1=alice.e2e.test
U2=e2e_bob;   U2_PASS='Sup3rSecret!bob';   D2=bob.e2e.test
U3=e2e_carol; U3_PASS='Sup3rSecret!carol'; D3=carol.e2e.test
PANEL_DOMAIN=panel.e2e.test

JAR="$(mktemp)"; BODY="$(mktemp)"; WWW="$(mktemp -d)"
BACKEND_PID=""
PASS_N=0; FAIL_N=0; FAILED=()

if [[ -t 1 ]]; then
    GREEN='\033[0;32m'; RED='\033[0;31m'; BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
else
    GREEN=''; RED=''; BLUE=''; BOLD=''; NC=''
fi

# ── Yardımcılar ────────────────────────────────────────────
section() { echo -e "\n${BOLD}${BLUE}━━━ $1 ━━━${NC}"; }
info()    { echo -e "  ${BLUE}[i]${NC} $1"; }
ok()      { echo -e "  ${GREEN}✓${NC} $1"; PASS_N=$((PASS_N + 1)); }
bad()     { echo -e "  ${RED}✗ $1${NC}"; [[ -n "${2:-}" ]] && echo "      $2"
            FAIL_N=$((FAIL_N + 1)); FAILED+=("$1"); }

eq()       { [[ "$2" == "$3" ]] && ok "$1" || bad "$1" "beklenen: '$2'  gelen: '$3'"; }
contains() { [[ "$2" == *"$3"* ]] && ok "$1" || bad "$1" "'$3' bulunamadı: ${2:0:200}"; }
absent()   { [[ "$2" != *"$3"* ]] && ok "$1" || bad "$1" "'$3' bulunmamalıydı: ${2:0:200}"; }
check()    { local d="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$d"; else bad "$d" "komut başarısız: $*"; fi; }
check_not(){ local d="$1"; shift; if "$@" >/dev/null 2>&1; then bad "$d" "komut başarılı olmamalıydı: $*"; else ok "$d"; fi; }

db()       { sqlite3 -batch "$PANEL_DIR/panel.db" "$1"; }
db_id()    { db "SELECT id FROM users WHERE username='$1'"; }
dom_id()   { db "SELECT id FROM domains WHERE domain='$1'"; }
dom_port() { db "SELECT target_port FROM domains WHERE domain='$1'"; }
urldecode(){ python3 -c 'import sys, urllib.parse as u; print(u.unquote_plus(sys.argv[1]))' "$1"; }

# Panele POST at; HTTP kodu CODE'a, çözülmüş yönlendirme adresi LOC'a, gövde $BODY'ye yazılır
post() {
    local path="$1" out; shift
    out="$(curl -sk -b "$JAR" -o "$BODY" -w '%{http_code} %{redirect_url}' "$@" "$BASE$path")"
    CODE="${out%% *}"; LOC="$(urldecode "${out#* }")"
}
get() {
    CODE="$(curl -sk -b "$JAR" -o "$BODY" -w '%{http_code}' "$BASE$1")"
}
login() {
    : > "$JAR"
    curl -sk -c "$JAR" -o /dev/null -w '%{http_code}' \
        --data-urlencode "username=$ADMIN" --data-urlencode "password=$ADMIN_PASS" "$BASE/login"
}
# Nginx üzerinden (port 80) Host başlığıyla istek; bağlantı kapatılırsa (444) 000 döner
web_code() { curl -s -o /dev/null -m 5 -w '%{http_code}' -H "Host: $1" "http://127.0.0.1${2:-/}"; }
web_head() { curl -s -D - -o /dev/null -m 5 -H "Host: $1" "http://127.0.0.1${2:-/}"; }
ssh_ok() {
    sshpass -p "$2" ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o PreferredAuthentications=password -o PubkeyAuthentication=no -o ConnectTimeout=5 \
        "$1@127.0.0.1" 'echo ssh-ok' 2>/dev/null | grep -q ssh-ok
}
wait_panel() {
    for _ in $(seq 1 30); do
        [[ "$(curl -sk -o /dev/null -w '%{http_code}' "$BASE/login")" == 200 ]] && return 0
        sleep 1
    done
    return 1
}
wait_port() {   # $1=port
    for _ in $(seq 1 20); do
        (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && return 0
        sleep 0.5
    done
    return 1
}

cleanup() {
    [[ -n "$BACKEND_PID" ]] && kill "$BACKEND_PID" 2>/dev/null
    rm -rf "$JAR" "$BODY" "$WWW" /tmp/e2e-update-src /tmp/e2e-nginx-*
}
trap cleanup EXIT

# ============================================================
section "0 — Hazırlık"
# ============================================================
for _ in $(seq 1 60); do
    state="$(systemctl is-system-running 2>/dev/null || true)"
    [[ "$state" == running || "$state" == degraded ]] && break
    sleep 1
done
info "systemd durumu: ${state:-?}"
[[ "${state:-}" == running || "${state:-}" == degraded ]] || { bad "systemd çalışmıyor (container --privileged ve /sbin/init ile mi başlatıldı?)"; exit 1; }

if [[ ! -d "$PANEL_DIR" ]]; then
    info "Panel kurulu değil, setup.sh çalıştırılıyor (log: /tmp/e2e-setup.log)"
    PANEL_PASSWORD="$ADMIN_PASS" PANEL_ADMIN="$ADMIN" PANEL_EMAIL=e2e@example.com \
        bash "$SRC/setup.sh" > /tmp/e2e-setup.log 2>&1
    rc=$?
    eq "setup.sh (ilk kurulum) çıkış kodu" 0 "$rc"
    [[ $rc -ne 0 ]] && { tail -20 /tmp/e2e-setup.log; exit 1; }
else
    info "Panel zaten kurulu, mevcut kurulum üzerinde test edilecek"
fi
wait_panel || { bad "panel yanıt vermiyor"; exit 1; }

# Önceki yarım kalmış bir koşudan artık kullanıcı varsa temizle
login >/dev/null
for u in $U1 $U2 $U3; do
    id="$(db_id $u)"
    [[ -n "$id" ]] && { info "Önceki koşudan kalan kullanıcı siliniyor: $u"; post /users/delete --data-urlencode "user_id=$id"; }
done

# ============================================================
section "1 — Kurulum doğrulaması"
# ============================================================
for svc in nginx vps-panel fail2ban; do
    eq "servis aktif: $svc" active "$(systemctl is-active $svc 2>/dev/null)"
done
# Ubuntu 24.04'te sshd soket etkinleştirmeli olabilir (ssh.socket)
check "sshd erişilebilir (ssh.service ya da ssh.socket)" bash -c "systemctl is-active --quiet ssh || systemctl is-active --quiet ssh.socket"
check "UFW aktif ve panel portu açık" bash -c "ufw status | grep -q 'Status: active' && ufw status | grep -q '$PANEL_PORT/tcp'"
check "Fail2ban sshd jail'i çalışıyor" fail2ban-client status sshd
check "nginx -t geçiyor" nginx -t
eq "/home izni 711" 711 "$(stat -c %a /home)"
eq ".env izni 600" 600 "$(stat -c %a $PANEL_DIR/.env)"
check ".env: PANEL_PORT_INTERNAL satırı yalnızca sayı (satır sonu yorumu yok)" \
    grep -qE '^PANEL_PORT_INTERNAL=[0-9]+$' "$PANEL_DIR/.env"
eq "panel.db izni 600" 600 "$(stat -c %a $PANEL_DIR/panel.db)"
eq "panel /login" 200 "$(curl -sk -o /dev/null -w '%{http_code}' "$BASE/login")"
eq "girişsiz / → yönlendirme" "307 $BASE/login" \
    "$(curl -sk -o /dev/null -w '%{http_code} %{redirect_url}' "$BASE/")"
eq "bilinmeyen Host :80'de reddedilir (444)" 000 "$(web_code nope.invalid)"
eq "IP ile :80 reddedilir (444)" 000 "$(curl -s -o /dev/null -m 5 -w '%{http_code}' http://127.0.0.1/)"

# ============================================================
section "2 — Giriş"
# ============================================================
before="$(db "SELECT COUNT(*) FROM activity_log WHERE action='auth.login_failed'")"
code="$(curl -sk -c "$JAR" -o "$BODY" -w '%{http_code}' --data-urlencode "username=$ADMIN" --data-urlencode "password=yanlis-sifre" "$BASE/login")"
eq "yanlış şifre → 200 (form tekrar)" 200 "$code"
contains "yanlış şifre → hata mesajı" "$(cat "$BODY")" "hatalı"
eq "yanlış şifrede oturum çerezi verilmez" 0 "$(grep -c '	sid	' "$JAR")"
eq "başarısız giriş kaydedildi" $((before + 1)) "$(db "SELECT COUNT(*) FROM activity_log WHERE action='auth.login_failed'")"
eq "doğru şifre → 302" 302 "$(login)"
get /;           eq "dashboard açılıyor" 200 "$CODE"
get /users;      eq "kullanıcılar sayfası" 200 "$CODE"
get /domains;    eq "domainler sayfası" 200 "$CODE"
get /logs;       eq "kayıtlar sayfası" 200 "$CODE"
get /nginx;      eq "nginx sayfası" 200 "$CODE"
get /settings;   eq "ayarlar sayfası" 200 "$CODE"
get /api/stats;  eq "/api/stats" 200 "$CODE"

# ============================================================
section "3 — Kullanıcı ekleme"
# ============================================================
for bad_name in Ab ab 1abc 'a-b-c' 'abc def'; do
    post /users/create --data-urlencode "username=$bad_name" --data-urlencode "password=Sup3rSecret!x" --data-urlencode "main_domain=bad.e2e.test"
    contains "geçersiz kullanıcı adı reddedilir: '$bad_name'" "$LOC" "Geçersiz kullanıcı adı"
done
post /users/create --data-urlencode "username=e2e_baddom" --data-urlencode "password=Sup3rSecret!x" --data-urlencode "main_domain=-bad_domain-"
contains "geçersiz domain reddedilir" "$LOC" "Geçersiz domain"
eq "reddedilen istekler DB'ye yazılmadı" 0 "$(db "SELECT COUNT(*) FROM users WHERE username LIKE 'e2e_bad%' OR username IN ('Ab','ab','1abc')")"

post /users/create --data-urlencode "username=$U1" --data-urlencode "password=$U1_PASS" --data-urlencode "main_domain=$D1"
contains "$U1 oluşturuldu" "$LOC" "oluşturuldu"
P1="$(db "SELECT port_prefix FROM users WHERE username='$U1'")"
A_PORT="$(dom_port $D1)"
eq "ana domain portu = blok başı + 80" $((P1 * 1000 + 80)) "$A_PORT"

H="/home/$U1"
check "Linux kullanıcısı var" id "$U1"
eq "home izni 700" 700 "$(stat -c %a $H)"
eq "home sahibi" "$U1" "$(stat -c %U $H)"
for d in projects docker data .ssh; do check "dizin var: ~/$d" test -d "$H/$d"; done
eq ".ssh izni 700" 700 "$(stat -c %a $H/.ssh)"
eq "authorized_keys izni 600" 600 "$(stat -c %a $H/.ssh/authorized_keys)"
check "README.md oluştu" test -s "$H/README.md"
check "docker-compose.example.yml oluştu" test -s "$H/projects/docker-compose.example.yml"
contains "compose örneği doğru portu içeriyor" "$(cat $H/projects/docker-compose.example.yml)" "\"$A_PORT:80\""
contains ".bashrc port bloğunu içeriyor" "$(cat $H/.bashrc)" "PORT_START=$((P1 * 1000))"
eq "passwd durumu: parola ayarlı (P)" P "$(passwd -S $U1 | awk '{print $2}')"
check "linger açık" test -e "/var/lib/systemd/linger/$U1"
check "SSH ile parolayla giriş çalışıyor" ssh_ok "$U1" "$U1_PASS"
check_not "SSH yanlış parolayla girişi reddediyor" ssh_ok "$U1" "yanlis-parola"
eq "DB: kullanıcı aktif" active "$(db "SELECT status FROM users WHERE username='$U1'")"
eq "DB: ana domain" "$D1" "$(db "SELECT main_domain FROM users WHERE username='$U1'")"
check "nginx conf yazıldı" test -f "/etc/nginx/sites-available/vps-users/$D1.conf"
check "nginx symlink oluştu" test -L "/etc/nginx/sites-enabled/vps-users/$D1.conf"
check "nginx conf doğru portu gösteriyor" grep -qE "proxy_pass +http://127\.0\.0\.1:$A_PORT;" "/etc/nginx/sites-available/vps-users/$D1.conf"
eq "etkinlik kaydı: user.create" 1 "$(db "SELECT COUNT(*) FROM activity_log WHERE action='user.create' AND target='$U1' AND status='ok'")"

# Yönlendirme: önce arka uç yok → 502; sonra basit bir arka uç başlat → 200
eq "arka uç yokken domain → 502" 502 "$(web_code $D1)"
echo "e2e-backend" > "$WWW/index.html"
python3 -m http.server "$A_PORT" --bind 127.0.0.1 --directory "$WWW" >/dev/null 2>&1 &
BACKEND_PID=$!
wait_port "$A_PORT"
eq "arka uç varken domain → 200" 200 "$(web_code $D1)"
eq "yanıt gövdesi arka uçtan geliyor" e2e-backend "$(curl -s -m 5 -H "Host: $D1" http://127.0.0.1/ | tr -d '\n')"

# Yinelenen / çakışan kayıtlar
post /users/create --data-urlencode "username=$U1" --data-urlencode "password=$U1_PASS" --data-urlencode "main_domain=baska.e2e.test"
contains "aynı kullanıcı adı reddedilir" "$LOC" "err="
eq "aynı kullanıcı adı DB'de tek satır" 1 "$(db "SELECT COUNT(*) FROM users WHERE username='$U1'")"
post /users/create --data-urlencode "username=e2e_other" --data-urlencode "password=$U1_PASS" --data-urlencode "main_domain=$D1"
contains "başka kullanıcının domaini reddedilir" "$LOC" "zaten başka bir kullanıcıya ait"
eq "çakışmada Linux kullanıcısı oluşmadı" 0 "$(getent passwd e2e_other | wc -l)"
post /users/create --data-urlencode "username=ubuntu" --data-urlencode "password=$U1_PASS" --data-urlencode "main_domain=ubuntu.e2e.test"
contains "mevcut sistem kullanıcısıyla çakışma reddedilir" "$LOC" "err="
eq "çakışmada DB'ye 'ubuntu' yazılmadı" 0 "$(db "SELECT COUNT(*) FROM users WHERE username='ubuntu'")"
eq "çakışmada 'ubuntu' domaini nginx'e yazılmadı" 0 "$(ls /etc/nginx/sites-available/vps-users | grep -c '^ubuntu')"
check "mevcut 'ubuntu' kullanıcısı bozulmadı" id ubuntu

# ============================================================
section "4 — İkinci kullanıcı ve port blokları"
# ============================================================
post /users/create --data-urlencode "username=$U2" --data-urlencode "password=$U2_PASS" --data-urlencode "main_domain=$D2"
contains "$U2 oluşturuldu" "$LOC" "oluşturuldu"
P2="$(db "SELECT port_prefix FROM users WHERE username='$U2'")"
B_PORT="$(dom_port $D2)"
[[ "$P2" != "$P1" ]] && ok "port blokları farklı ($P1 ≠ $P2)" || bad "port blokları çakışıyor ($P1)"
eq "kullanıcılar birbirinin home'unu okuyamaz" "Permission denied" \
    "$(runuser -u $U2 -- ls $H 2>&1 | grep -o 'Permission denied' | head -1)"
check "bob da SSH ile girebiliyor" ssh_ok "$U2" "$U2_PASS"

# ============================================================
section "5 — Domain ekleme / silme"
# ============================================================
UID1="$(db_id $U1)"
D1B="www.$D1"; D1C="api.$D1"
get /domains
contains "aktif kullanıcı domain formunda listelenir" "$(cat "$BODY")" "<option value=\"$UID1\""
post /domains/create --data-urlencode "domain=$D1B" --data-urlencode "user_id=$UID1" --data-urlencode "target_port=$A_PORT"
contains "domain eklendi" "$LOC" "eklendi"
check "yeni domain nginx conf + symlink" bash -c "test -f /etc/nginx/sites-available/vps-users/$D1B.conf && test -L /etc/nginx/sites-enabled/vps-users/$D1B.conf"
eq "yeni domain arka uca ulaşıyor" 200 "$(web_code $D1B)"

post /domains/create --data-urlencode "domain=$D1C" --data-urlencode "user_id=$UID1" --data-urlencode "target_port=$B_PORT"
contains "başkasının port bloğu reddedilir" "$LOC" "bloğu dışında"
eq "reddedilen domain DB'ye yazılmadı" 0 "$(db "SELECT COUNT(*) FROM domains WHERE domain='$D1C'")"
post /domains/create --data-urlencode "domain=$D1B" --data-urlencode "user_id=$UID1" --data-urlencode "target_port=$A_PORT"
contains "yinelenen domain reddedilir" "$LOC" "err="
eq "yinelenen domain tek satır" 1 "$(db "SELECT COUNT(*) FROM domains WHERE domain='$D1B'")"
post /domains/create --data-urlencode "domain=-bozuk-" --data-urlencode "user_id=$UID1" --data-urlencode "target_port=$A_PORT"
contains "geçersiz domain adı reddedilir" "$LOC" "Geçersiz domain"
post /domains/create --data-urlencode "domain=x.e2e.test" --data-urlencode "user_id=99999" --data-urlencode "target_port=$A_PORT"
contains "olmayan kullanıcı reddedilir" "$LOC" "Kullanıcı bulunamadı"

post /domains/delete --data-urlencode "domain_id=$(dom_id $D1B)"
contains "domain silindi" "$LOC" "silindi"
check_not "silinen domainin conf dosyası kalmadı" test -e "/etc/nginx/sites-available/vps-users/$D1B.conf"
check_not "silinen domainin symlink'i kalmadı" test -L "/etc/nginx/sites-enabled/vps-users/$D1B.conf"
eq "silinen domain artık yanıtlanmıyor" 000 "$(web_code $D1B)"
eq "ana domain etkilenmedi" 200 "$(web_code $D1)"
post /domains/delete --data-urlencode "domain_id=99999"
contains "olmayan domain silinemez" "$LOC" "Domain bulunamadı"

# ============================================================
section "6 — Kullanıcı pasifleştirme / aktifleştirme (güncelleme)"
# ============================================================
post /users/toggle --data-urlencode "user_id=$UID1"
contains "pasifleştirildi" "$LOC" "pasif"
eq "DB: inactive" inactive "$(db "SELECT status FROM users WHERE username='$U1'")"
eq "passwd durumu: kilitli (L)" L "$(passwd -S $U1 | awk '{print $2}')"
check_not "pasif kullanıcı SSH ile giremez" ssh_ok "$U1" "$U1_PASS"
check_not "nginx symlink kaldırıldı" test -L "/etc/nginx/sites-enabled/vps-users/$D1.conf"
check "nginx conf dosyası korundu" test -f "/etc/nginx/sites-available/vps-users/$D1.conf"
eq "pasif kullanıcının domaini yanıtlanmıyor" 000 "$(web_code $D1)"
check "nginx -t hâlâ geçiyor" nginx -t
check "diğer kullanıcı etkilenmedi (bob SSH)" ssh_ok "$U2" "$U2_PASS"
get /domains
absent "pasif kullanıcı domain formunda listelenmez" "$(cat "$BODY")" "<option value=\"$UID1\""

post /users/toggle --data-urlencode "user_id=$UID1"
contains "aktifleştirildi" "$LOC" "aktif"
eq "DB: active" active "$(db "SELECT status FROM users WHERE username='$U1'")"
eq "passwd durumu: açık (P)" P "$(passwd -S $U1 | awk '{print $2}')"
check "aktif kullanıcı tekrar SSH ile girebiliyor" ssh_ok "$U1" "$U1_PASS"
check "nginx symlink geri geldi" test -L "/etc/nginx/sites-enabled/vps-users/$D1.conf"
eq "domain tekrar yanıtlanıyor" 200 "$(web_code $D1)"
post /users/toggle --data-urlencode "user_id=99999"
contains "olmayan kullanıcı toggle edilemez" "$LOC" "Kullanıcı bulunamadı"

# ============================================================
section "7 — Rootless Docker ve panelden container listesi"
# ============================================================
if [[ "${E2E_SKIP_DOCKER:-0}" == 1 ]]; then
    info "E2E_SKIP_DOCKER=1 → atlandı"
else
    check "kullanıcının rootless Docker soketi var" test -S "/run/user/$(id -u $U1)/docker.sock"
    check "kullanıcı 'docker ps' çalıştırabiliyor" runuser -l "$U1" -c 'docker ps'
    DK_PORT=$((P1 * 1000 + 81)); DK_DOM="docker.$D1"
    out="$(runuser -l "$U1" -c "docker run -d --name web -p $DK_PORT:80 nginx:alpine" 2>&1)"
    [[ $? -eq 0 ]] && ok "kullanıcı container başlattı (nginx:alpine :$DK_PORT)" || bad "container başlatılamadı" "${out: -300}"
    wait_port "$DK_PORT" && ok "container portu dinliyor" || bad "container portu açılmadı"
    post /domains/create --data-urlencode "domain=$DK_DOM" --data-urlencode "user_id=$UID1" --data-urlencode "target_port=$DK_PORT"
    contains "container için domain eklendi" "$LOC" "eklendi"
    eq "domain → nginx → rootless container (200)" 200 "$(web_code $DK_DOM)"
    contains "container sayfası nginx karşılama sayfası" "$(curl -s -m 5 -H "Host: $DK_DOM" http://127.0.0.1/)" "Welcome to nginx"
    get "/api/user/$U1/containers"
    contains "panel API container'ı listeliyor" "$(cat "$BODY")" '"name":"web"'
    get "/api/user/yok_boyle_biri/containers"
    contains "olmayan kullanıcı için API hata döner" "$(cat "$BODY")" "Kullanıcı bulunamadı"
fi

# ============================================================
section "8 — Nginx editörü"
# ============================================================
KEY="sites-available/vps-users/$D1.conf"; CONF="/etc/nginx/$KEY"
BK=/var/backups/vps-panel-nginx
get /nginx;                      contains "dosya listesinde kullanıcı domaini var" "$(cat "$BODY")" "$D1.conf"
get "/nginx?file=$KEY";          eq "dosya açılıyor" 200 "$CODE"
contains "editörde dosya içeriği var" "$(cat "$BODY")" "proxy_pass"
get "/nginx?file=nginx.conf";    eq "nginx.conf açılıyor" 200 "$CODE"
get "/nginx?file=../../etc/passwd"
contains "yol gezinmesi (../) reddedilir" "$(cat "$BODY")" "Dosya bulunamadı"
get "/nginx?file=sites-enabled/vps-users/$D1.conf"
contains "symlink dosyalar düzenlenemez" "$(cat "$BODY")" "Dosya bulunamadı"

orig_sum="$(sha256sum "$CONF" | cut -d' ' -f1)"
sed 's|client_max_body_size 100M;|client_max_body_size 100M;\n    add_header X-E2E "1" always;|' "$CONF" > /tmp/e2e-nginx-good.conf
post /nginx/save --data-urlencode "file=$KEY" --data-urlencode "content@/tmp/e2e-nginx-good.conf"
contains "geçerli config kaydedildi" "$LOC" "Kaydedildi"
contains "yeni içerik diske yazıldı" "$(cat $CONF)" 'add_header X-E2E "1"'
contains "nginx yeni config ile yanıt veriyor (reload)" "$(web_head $D1)" "X-E2E: 1"
eq "yedek oluştu" 1 "$(ls $BK 2>/dev/null | grep -c "^sites-available__vps-users__$D1.conf\.")"
eq "yedek eski içeriği taşıyor" "$orig_sum" "$(sha256sum $BK/sites-available__vps-users__$D1.conf.*.bak | head -1 | cut -d' ' -f1)"
eq "yedek dizini izni 700" 700 "$(stat -c %a $BK)"
eq "dosya izni korundu" 644 "$(stat -c %a $CONF)"
check_not "geçici dosya kalmadı" bash -c "ls -A /etc/nginx/sites-available/vps-users | grep -q vps-tmp"

post /nginx/save --data-urlencode "file=$KEY" --data-urlencode "content@/tmp/e2e-nginx-good.conf"
contains "değişiklik yoksa kaydetmez" "$LOC" "Değişiklik"

good_sum="$(sha256sum "$CONF" | cut -d' ' -f1)"
printf 'server {\n    listen 80;\n    server_name %s;\n    bu_bir_direktif_degil on;\n}\n' "$D1" > /tmp/e2e-nginx-bad.conf
post /nginx/save --data-urlencode "file=$KEY" --data-urlencode "content@/tmp/e2e-nginx-bad.conf"
eq "bozuk config → sayfa geri döner (200)" 200 "$CODE"
contains "bozuk config reddedildi" "$(cat "$BODY")" "Kaydedilmedi"
contains "nginx hata çıktısı gösteriliyor" "$(cat "$BODY")" "unknown directive"
eq "bozuk config eski haline döndürüldü" "$good_sum" "$(sha256sum "$CONF" | cut -d' ' -f1)"
check "nginx -t hâlâ geçiyor" nginx -t
eq "nginx hâlâ eski config ile yanıt veriyor" 200 "$(web_code $D1)"
eq "bozuk kayıt yedek üretmedi" 1 "$(ls $BK | grep -c "^sites-available__vps-users__$D1.conf\.")"
contains "bozuk kayıt hata olarak loglandı" "$(db "SELECT status FROM activity_log WHERE action='nginx.save' ORDER BY id DESC LIMIT 1")" "error"

post /nginx/save --data-urlencode "file=../../etc/passwd" --data-urlencode "content=root::0:0::/:/bin/sh"
contains "yol gezinmesiyle kayıt reddedilir" "$LOC" "err="
check "/etc/passwd bozulmadı" grep -q "^$U1:" /etc/passwd

printf 'server {\0}\n' > /tmp/e2e-nginx-nul.conf
post /nginx/save --data-urlencode "file=$KEY" --data-urlencode "content@/tmp/e2e-nginx-nul.conf"
contains "NUL karakterli içerik reddedilir" "$(cat "$BODY")" "İçerik geçersiz"
eq "NUL denemesi dosyayı değiştirmedi" "$good_sum" "$(sha256sum "$CONF" | cut -d' ' -f1)"

get "/nginx?file=$KEY&backup=$(ls $BK | grep "^sites-available__vps-users__$D1.conf\." | head -1)"
absent "yedekten yükleme: eski içerik editöre geldi" "$(cat "$BODY")" "X-E2E"
post /nginx/test --data-urlencode "file=$KEY"
contains "nginx -t düğmesi" "$(cat "$BODY")" "geçerli"
post /nginx/reload --data-urlencode "file=$KEY"
contains "reload düğmesi" "$(cat "$BODY")" "reload edildi"

# ============================================================
section "9 — Panel domain ayarı"
# ============================================================
post /settings/domain --data-urlencode "domain=$PANEL_DOMAIN"
contains "panel domaini ayarlandı" "$LOC" "ayarlandı"
check "settings.json yazıldı" grep -q "$PANEL_DOMAIN" "$PANEL_DIR/settings.json"
check "panel domain nginx conf + symlink" bash -c "test -f /etc/nginx/sites-available/$PANEL_DOMAIN.conf && test -L /etc/nginx/sites-enabled/$PANEL_DOMAIN.conf"
eq "panel domain üzerinden /login (200)" 200 "$(web_code $PANEL_DOMAIN /login)"
post /settings/domain --data-urlencode "domain=-bozuk-"
contains "geçersiz panel domaini reddedilir" "$LOC" "Geçersiz domain"

# ============================================================
section "10 — Kayıtlar (activity log)"
# ============================================================
for a in auth.login auth.login_failed user.create user.toggle domain.create domain.delete nginx.save settings.domain; do
    n="$(db "SELECT COUNT(*) FROM activity_log WHERE action='$a'")"
    [[ "$n" -ge 1 ]] && ok "kayıt var: $a ($n)" || bad "kayıt yok: $a"
done
get "/logs?category=user";         eq "/logs kategori filtresi" 200 "$CODE"
get "/logs?status=error&page=99";  eq "/logs durum filtresi + taşan sayfa" 200 "$CODE"
get "/logs?category=%3Cscript%3E";  eq "/logs geçersiz kategori çökmez" 200 "$CODE"
get /;                             contains "dashboard son işlemleri gösteriyor" "$(cat "$BODY")" "$U1"

# ============================================================
section "11 — Kullanıcı silme"
# ============================================================
UID2_LINUX="$(id -u $U2)"
UID2="$(db_id $U2)"
post /users/delete --data-urlencode "user_id=$UID2"
contains "$U2 silindi" "$LOC" "silindi"
check_not "Linux kullanıcısı kalmadı" id "$U2"
check_not "home dizini silindi" test -e "/home/$U2"
eq "subuid kaydı temizlendi" 0 "$(grep -c "^$U2:" /etc/subuid)"
eq "subgid kaydı temizlendi" 0 "$(grep -c "^$U2:" /etc/subgid)"
check_not "linger dosyası kalmadı" test -e "/var/lib/systemd/linger/$U2"
check_not "nginx conf silindi" test -e "/etc/nginx/sites-available/vps-users/$D2.conf"
check_not "nginx symlink silindi" test -L "/etc/nginx/sites-enabled/vps-users/$D2.conf"
eq "DB: kullanıcı silindi" 0 "$(db "SELECT COUNT(*) FROM users WHERE username='$U2'")"
eq "DB: domainleri de silindi (cascade)" 0 "$(db "SELECT COUNT(*) FROM domains WHERE domain='$D2'")"
eq "silinen kullanıcının domaini yanıtlanmıyor" 000 "$(web_code $D2)"
check_not "systemd kullanıcı oturumu kalmadı (loginctl)" bash -c "loginctl list-users --no-legend | grep -q ' $U2 '"
check_not "/run/user/$UID2_LINUX kalmadı" test -e "/run/user/$UID2_LINUX"
eq "silmeden etkilenmeyen kullanıcı: domain" 200 "$(web_code $D1)"
check "silmeden etkilenmeyen kullanıcı: SSH" ssh_ok "$U1" "$U1_PASS"
eq "etkinlik kaydı: user.delete (ok)" 1 "$(db "SELECT COUNT(*) FROM activity_log WHERE action='user.delete' AND target='$U2' AND status='ok'")"
post /users/delete --data-urlencode "user_id=$UID2"
contains "olmayan kullanıcı silinemez" "$LOC" "Kullanıcı bulunamadı"

post /users/create --data-urlencode "username=$U3" --data-urlencode "password=$U3_PASS" --data-urlencode "main_domain=$D3"
contains "$U3 oluşturuldu" "$LOC" "oluşturuldu"
eq "silinen kullanıcının port bloğu yeniden kullanılıyor" "$P2" "$(db "SELECT port_prefix FROM users WHERE username='$U3'")"
check "$U3 SSH ile girebiliyor" ssh_ok "$U3" "$U3_PASS"
post /users/delete --data-urlencode "user_id=$(db_id $U3)"
contains "$U3 silindi" "$LOC" "silindi"

# ============================================================
section "12 — Güncelleme (update.sh)"
# ============================================================
snap() {
    { db "SELECT username, password_hash FROM admins ORDER BY id;
          SELECT id, username, password_hash, port_prefix, main_domain, status FROM users ORDER BY id;
          SELECT id, domain, user_id, target_port, ssl_enabled FROM domains ORDER BY id;"
      sha256sum "$PANEL_DIR/settings.json" "$PANEL_DIR/.env"; } | sha256sum | cut -d' ' -f1
}
SNAP_BEFORE="$(snap)"
USERS_BEFORE="$(db "SELECT COUNT(*) FROM users")"
LOGS_BEFORE="$(db "SELECT COUNT(*) FROM activity_log")"

UPD=/tmp/e2e-update-src
rm -rf "$UPD"; mkdir -p "$UPD"
tar -C "$SRC" --exclude=.git -cf - . | tar -C "$UPD" -xf -
MARK="e2e-update-marker-$RANDOM"
echo "<!-- $MARK -->" >> "$UPD/panel/templates/login.html"
# Geliştirici makinesinden gelebilecek çalışma zamanı dosyaları kurulu olanların üstüne yazılmamalı
echo "DECOY" > "$UPD/panel/panel.db"
echo '{"panel_domain": "decoy.example"}' > "$UPD/panel/settings.json"
echo "PANEL_PASSWORD=decoy" > "$UPD/panel/.env"

bash "$UPD/update.sh" > /tmp/e2e-update.log 2>&1
rc=$?
eq "update.sh çıkış kodu" 0 "$rc"
[[ $rc -ne 0 ]] && tail -15 /tmp/e2e-update.log
wait_panel && ok "güncelleme sonrası panel yanıt veriyor" || bad "güncelleme sonrası panel yanıt vermiyor"
eq "servis aktif" active "$(systemctl is-active vps-panel)"
contains "yeni şablon dağıtıldı (dosya)" "$(cat $PANEL_DIR/templates/login.html)" "$MARK"
contains "yeni şablon sunuluyor (HTTP)" "$(curl -sk $BASE/login)" "$MARK"
eq "DB, ayarlar ve .env korundu (sahte dosyalar yazılmadı)" "$SNAP_BEFORE" "$(snap)"
eq "kullanıcı sayısı değişmedi" "$USERS_BEFORE" "$(db "SELECT COUNT(*) FROM users")"
eq "panel.db SQLite olarak geçerli" ok "$(db "PRAGMA integrity_check")"
eq "eski oturum geçersiz (bellek içi oturumlar sıfırlandı)" 307 "$(curl -sk -b "$JAR" -o /dev/null -w '%{http_code}' "$BASE/")"
eq "yeniden giriş (admin şifresi geçerli kalmaya devam ediyor)" 302 "$(login)"
eq "kullanıcı domaini güncelleme sırasında kesilmedi" 200 "$(web_code $D1)"
eq "panel domain ayarı korundu" 200 "$(web_code $PANEL_DOMAIN /login)"
[[ "$(db "SELECT COUNT(*) FROM activity_log")" -ge "$LOGS_BEFORE" ]] && ok "etkinlik geçmişi korundu" || bad "etkinlik geçmişi azaldı"

# ============================================================
section "13 — setup.sh'i yeniden çalıştırma (idempotency)"
# ============================================================
SNAP_BEFORE="$(snap)"
PANEL_PASSWORD="$ADMIN_PASS" PANEL_ADMIN="$ADMIN" PANEL_EMAIL=e2e@example.com \
    bash "$SRC/setup.sh" > /tmp/e2e-setup2.log 2>&1
rc=$?
eq "setup.sh (ikinci çalıştırma) çıkış kodu" 0 "$rc"
[[ $rc -ne 0 ]] && tail -15 /tmp/e2e-setup2.log
contains "mevcut veritabanı korundu" "$(cat /tmp/e2e-setup2.log)" "Mevcut veritabanı korundu"
wait_panel && ok "panel yanıt veriyor" || bad "panel yanıt vermiyor"
eq "kullanıcı sayısı değişmedi" "$USERS_BEFORE" "$(db "SELECT COUNT(*) FROM users")"
eq "nginx.conf'ta vps-users include'u tekrarlanmadı" 1 "$(grep -c 'sites-enabled/vps-users' /etc/nginx/nginx.conf)"
check "nginx -t geçiyor" nginx -t
eq "kullanıcı domaini hâlâ çalışıyor" 200 "$(web_code $D1)"
eq "panel domaini hâlâ çalışıyor" 200 "$(web_code $PANEL_DOMAIN /login)"
check "UFW hâlâ aktif" bash -c "ufw status | grep -q 'Status: active'"
eq "admin girişi çalışıyor" 302 "$(login)"
eq ".env'deki dahili port satırı geçerli" 1 "$(grep -cE '^PANEL_PORT_INTERNAL=[0-9]+$' $PANEL_DIR/.env)"

# ============================================================
section "14 — Temizlik: panel domaini ve kullanıcı silme (container çalışırken)"
# ============================================================
post /settings/domain-remove
contains "panel domain ayarı kaldırıldı" "$LOC" "kaldırıldı"
check_not "panel domain conf'u silindi" test -e "/etc/nginx/sites-available/$PANEL_DOMAIN.conf"
check_not "settings.json'da panel domaini kalmadı" grep -q "$PANEL_DOMAIN" "$PANEL_DIR/settings.json"
eq "panel domain artık yanıtlanmıyor" 000 "$(web_code $PANEL_DOMAIN /login)"

UID1_LINUX="$(id -u $U1)"
if [[ "${E2E_SKIP_DOCKER:-0}" != 1 ]]; then
    check "silmeden önce kullanıcının container portu açık (:$DK_PORT)" bash -c "ss -ltn | grep -q ':$DK_PORT '"
fi
post /users/delete --data-urlencode "user_id=$(db_id $U1)"
contains "$U1 silindi" "$LOC" "silindi"
check_not "Linux kullanıcısı kalmadı" id "$U1"
check_not "home dizini silindi" test -e "/home/$U1"
eq "kullanıcının hiç süreci kalmadı" 0 "$(ps -eo uid | awk -v u="$UID1_LINUX" '$1==u' | wc -l)"
eq "rootless dockerd süreci kalmadı" 0 "$(pgrep -fc 'dockerd-rootless|rootlesskit' || true)"
if [[ "${E2E_SKIP_DOCKER:-0}" != 1 ]]; then
    check_not "container portu serbest kaldı (:$DK_PORT)" bash -c "ss -ltn | grep -q ':$DK_PORT '"
    eq "silinen kullanıcının container domaini yanıtlanmıyor" 000 "$(web_code $DK_DOM)"
fi
eq "ana domain yanıtlanmıyor" 000 "$(web_code $D1)"
eq "DB: kullanıcı ve domainleri silindi" 0 "$(db "SELECT (SELECT COUNT(*) FROM users WHERE username LIKE 'e2e\_%' ESCAPE '\\') + (SELECT COUNT(*) FROM domains WHERE domain LIKE '%.e2e.test')")"
eq "nginx'te kullanıcı conf'u kalmadı" 0 "$(ls /etc/nginx/sites-available/vps-users | grep -c 'e2e.test')"
eq "nginx'te kırık symlink kalmadı" 0 "$(find /etc/nginx/sites-enabled -xtype l | wc -l)"
check "nginx -t geçiyor" nginx -t
check_not "SSH artık girmiyor" ssh_ok "$U1" "$U1_PASS"
eq "panel hâlâ ayakta" active "$(systemctl is-active vps-panel)"
check_not "başarısız systemd birimi yok" bash -c "systemctl --failed --no-legend | grep -q ."

# ============================================================
echo -e "\n${BOLD}━━━ Sonuç ━━━${NC}"
echo -e "  ${GREEN}Başarılı: $PASS_N${NC}   ${RED}Başarısız: $FAIL_N${NC}"
if [[ $FAIL_N -gt 0 ]]; then
    echo -e "\n${RED}Başarısız kontroller:${NC}"
    printf '  - %s\n' "${FAILED[@]}"
    exit 1
fi
