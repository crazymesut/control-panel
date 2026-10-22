#!/bin/bash
# ============================================================
#  VPS Panel - Ana Kurulum Scripti
#  Docker Rootless + Nginx + Güvenlik + Panel
# ============================================================
set -euo pipefail

# ---- Renkler ----
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

log()   { echo -e "${GREEN}[✓]${NC} $1"; }
info()  { echo -e "${BLUE}[i]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[✗]${NC} $1"; exit 1; }
step()  { echo -e "\n${BOLD}${BLUE}━━━ $1 ━━━${NC}"; }

# ---- Değişkenler ----
PANEL_PORT="${PANEL_PORT:-8765}"
PANEL_DIR="/opt/vps-panel"
PANEL_SERVICE="vps-panel"
PANEL_ADMIN="${PANEL_ADMIN:-admin}"
PANEL_PASSWORD="${PANEL_PASSWORD:-$(openssl rand -base64 16)}"
PANEL_EMAIL="${PANEL_EMAIL:-admin@example.com}"
SERVER_IP="$(curl -s --max-time 5 ifconfig.me || hostname -I | awk '{print $1}')"

# ---- Root kontrolü ----
[[ $EUID -ne 0 ]] && error "Bu script root olarak çalıştırılmalıdır: sudo bash setup.sh"

# ---- OS kontrolü ----
if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    [[ "$ID" != "ubuntu" && "$ID" != "debian" ]] && \
        error "Desteklenen dağıtımlar: Ubuntu 20.04+, Debian 11+"
    info "Sistem: $PRETTY_NAME"
else
    error "/etc/os-release bulunamadı"
fi

echo -e "\n${BOLD}╔══════════════════════════════════════╗"
echo    "║      VPS Panel Kurulum Başlıyor      ║"
echo -e "╚══════════════════════════════════════╝${NC}\n"

# ============================================================
step "1/7 — Sistem Güncelleme"
# ============================================================
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq \
    curl wget git unzip \
    python3 python3-pip python3-venv \
    nginx certbot python3-certbot-nginx \
    fail2ban ufw \
    uidmap dbus-user-session slirp4netns fuse-overlayfs \
    openssh-server openssl \
    htop net-tools
log "Sistem güncellendi"

# ============================================================
step "2/7 — Docker Kurulumu"
# ============================================================
if ! command -v docker &>/dev/null; then
    info "Docker kuruluyor..."
    curl -fsSL https://get.docker.com | sh
    apt-get install -y -qq docker-ce-rootless-extras
    log "Docker kuruldu"
else
    log "Docker zaten kurulu: $(docker --version)"
fi

# newuidmap / newgidmap için setuid
chmod u+s /usr/bin/newuidmap /usr/bin/newgidmap 2>/dev/null || true

# ============================================================
step "3/7 — Güvenlik Duvarı (UFW)"
# ============================================================
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp    comment 'SSH'
ufw allow 80/tcp    comment 'HTTP'
ufw allow 443/tcp   comment 'HTTPS'
ufw allow "$PANEL_PORT"/tcp comment 'VPS Panel'
# Kullanıcı port bloğu: 10000-59999
ufw allow 10000:59999/tcp comment 'Kullanıcı Portları'
ufw --force enable
log "UFW aktif: SSH(22), HTTP(80), HTTPS(443), Panel($PANEL_PORT), Kullanıcı(10000-59999)"

# ============================================================
step "4/7 — Fail2ban"
# ============================================================
cat > /etc/fail2ban/jail.local << 'FAIL2BAN'
[DEFAULT]
bantime  = 1h
findtime = 10m
maxretry = 5
backend  = systemd

[sshd]
enabled  = true
port     = ssh
maxretry = 3

[nginx-http-auth]
enabled  = true
FAIL2BAN

systemctl enable fail2ban --quiet
systemctl restart fail2ban
log "Fail2ban aktif"

# ============================================================
step "5/7 — Nginx Temel Ayarları"
# ============================================================
# Panel için self-signed SSL sertifikası
SSL_DIR="/etc/nginx/ssl"
mkdir -p "$SSL_DIR"
if [[ ! -f "$SSL_DIR/panel.crt" ]]; then
    openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
        -keyout "$SSL_DIR/panel.key" \
        -out    "$SSL_DIR/panel.crt" \
        -subj   "/C=TR/ST=Istanbul/L=Istanbul/O=VPS-Panel/CN=$SERVER_IP" \
        2>/dev/null
    log "Self-signed SSL sertifikası oluşturuldu"
fi

# Kullanıcı domain config dizinleri ve nginx.conf include'ları
# (nginx -t'den ÖNCE yapılmalı: sites-enabled/vps-users dizini '*' include'una takılır)
mkdir -p /etc/nginx/sites-available/vps-users
mkdir -p /etc/nginx/sites-enabled/vps-users

if ! grep -q "vps-users" /etc/nginx/nginx.conf; then
    # Wildcard'ı .conf uzantılı dosyalarla sınırla (dizinleri dışarıda bırak)
    sed -i 's|include /etc/nginx/sites-enabled/\*;|include /etc/nginx/sites-enabled/*.conf;|' \
        /etc/nginx/nginx.conf
    # Kullanıcı domain config'leri için ayrı include ekle
    sed -i '/include \/etc\/nginx\/sites-enabled\/\*\.conf;/a\\tinclude /etc/nginx/sites-enabled/vps-users/*.conf;' \
        /etc/nginx/nginx.conf
fi

# Catch-all: tanınmayan Host/IP istekleri panele ya da başka bir vhost'a düşmesin.
# Aksi halde alfabetik olarak ilk gelen vhost implicit default olur ve panel,
# sertifika uyuşmazlığıyla herhangi bir host adı altında sunulur.
cat > /etc/nginx/conf.d/00-default.conf << 'NGINXCONF'
server {
    listen 80 default_server;
    server_name _;
    return 444;
}

server {
    listen 443 ssl default_server;
    server_name _;
    ssl_reject_handshake on;
}
NGINXCONF

# Nginx ana config
cat > /etc/nginx/sites-available/vps-panel << NGINXCONF
server {
    listen              ${PANEL_PORT} ssl;
    server_name         _;

    ssl_certificate     ${SSL_DIR}/panel.crt;
    ssl_certificate_key ${SSL_DIR}/panel.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    client_max_body_size 50M;

    location / {
        proxy_pass         http://127.0.0.1:$((PANEL_PORT + 10000));
        proxy_http_version 1.1;
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
    }
}
NGINXCONF

# Varsayılan siteyi devre dışı bırak
rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/vps-panel /etc/nginx/sites-enabled/vps-panel.conf

nginx -t && systemctl enable nginx --quiet && systemctl reload nginx
log "Nginx yapılandırıldı"

# /home dizinini kapat — kullanıcılar birbirinin klasörünü göremez
chmod 711 /home
log "/home dizin izni 711 olarak ayarlandı"

# ============================================================
step "6/7 — Panel Kurulumu"
# ============================================================
mkdir -p "$PANEL_DIR"/{templates,scripts,static}

# Python venv
python3 -m venv "$PANEL_DIR/venv"
"$PANEL_DIR/venv/bin/pip" install -q --upgrade pip
"$PANEL_DIR/venv/bin/pip" install -q \
    fastapi "uvicorn[standard]" jinja2 python-multipart \
    bcrypt aiofiles psutil

# Panel dosyalarını kopyala (script ile aynı dizinden)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -d "$SCRIPT_DIR/panel" ]]; then
    # Çalışma zamanı verileri (DB, ayarlar, .env) üzerine yazılmaz
    tar -C "$SCRIPT_DIR/panel" \
        --exclude='panel.db' --exclude='panel.db-*' --exclude='panel.db.example' \
        --exclude='settings.json' --exclude='.env' \
        -cf - . | tar -C "$PANEL_DIR" -xf -
    log "Panel dosyaları kopyalandı"

    # Veritabanı yalnızca ilk kurulumda şablondan oluşturulur; varsa dokunulmaz
    if [[ ! -f "$PANEL_DIR/panel.db" ]]; then
        cp "$SCRIPT_DIR/panel/panel.db.example" "$PANEL_DIR/panel.db"
        chmod 600 "$PANEL_DIR/panel.db"
        log "Yeni veritabanı oluşturuldu (panel.db.example)"
    else
        log "Mevcut veritabanı korundu (panel.db)"
    fi
else
    error "Panel dosyaları bulunamadı! 'panel/' dizininin setup.sh ile aynı yerde olduğundan emin olun."
fi

# Admin şifresi kaydet
cat > "$PANEL_DIR/.env" << ENV
PANEL_ADMIN=${PANEL_ADMIN}
PANEL_PASSWORD=${PANEL_PASSWORD}
PANEL_EMAIL=${PANEL_EMAIL}
PANEL_PORT_INTERNAL=$((PANEL_PORT + 10000))  # Dahili port (max 65535 sınırı içinde)
ENV
chmod 600 "$PANEL_DIR/.env"

# Systemd servisi
cat > /etc/systemd/system/${PANEL_SERVICE}.service << SYSTEMD
[Unit]
Description=VPS Panel
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${PANEL_DIR}
EnvironmentFile=${PANEL_DIR}/.env
ExecStart=${PANEL_DIR}/venv/bin/uvicorn main:app --host 127.0.0.1 --port \${PANEL_PORT_INTERNAL} --workers 1
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SYSTEMD

systemctl daemon-reload
systemctl enable "$PANEL_SERVICE" --quiet
systemctl restart "$PANEL_SERVICE"
sleep 2

if systemctl is-active --quiet "$PANEL_SERVICE"; then
    log "Panel servisi çalışıyor"
else
    error "Panel servisi başlatılamadı! 'journalctl -u $PANEL_SERVICE' ile kontrol edin."
fi

# ============================================================
step "7/7 — Tamamlandı"
# ============================================================

echo -e "\n${BOLD}${GREEN}╔══════════════════════════════════════════════╗"
echo    "║          KURULUM TAMAMLANDI! 🎉             ║"
echo    "╠══════════════════════════════════════════════╣"
printf  "║  Panel URL    : https://%-20s║\n" "$SERVER_IP:$PANEL_PORT"
printf  "║  Kullanıcı adı: %-28s║\n" "$PANEL_ADMIN"
printf  "║  Şifre        : %-28s║\n" "$PANEL_PASSWORD"
echo    "╠══════════════════════════════════════════════╣"
echo    "║  ⚠  Şifreyi not alın, tekrar gösterilmez!   ║"
echo -e "╚══════════════════════════════════════════════╝${NC}\n"

# Şifreyi güvenli kaydet
echo "Panel Admin Şifresi: $PANEL_PASSWORD" > /root/vps-panel-credentials.txt
chmod 600 /root/vps-panel-credentials.txt
info "Şifre /root/vps-panel-credentials.txt dosyasına kaydedildi"
info "Tarayıcınızda güvenlik uyarısını geçmek için 'Gelişmiş > Devam et' seçin (self-signed SSL)"
