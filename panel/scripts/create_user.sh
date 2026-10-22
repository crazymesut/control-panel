#!/bin/bash
# ============================================================
#  Kullanıcı Oluşturma Scripti
#  Kullanım: create_user.sh <username> <password> <port_start>
#  Örnek:    create_user.sh ahmet S3cur3P@ss 10000
# ============================================================
set -euo pipefail

USERNAME="$1"
PASSWORD="$2"
PORT_START="$3"

log()   { echo "[create_user] $1"; }
error() { echo "[create_user] HATA: $1" >&2; exit 1; }

[[ -z "$USERNAME" || -z "$PASSWORD" || -z "$PORT_START" ]] && \
    error "Eksik parametre. Kullanım: create_user.sh <username> <password> <port_start>"

# ── 1. Linux kullanıcısı oluştur ───────────────────────────
if id "$USERNAME" &>/dev/null; then
    error "Kullanıcı '$USERNAME' zaten mevcut"
fi

useradd \
    --create-home \
    --shell /bin/bash \
    --comment "VPS Panel User" \
    "$USERNAME"

echo "$USERNAME:$PASSWORD" | chpasswd
log "Linux kullanıcısı oluşturuldu: $USERNAME"

# ── 2. Klasör yapısı ───────────────────────────────────────
USER_HOME="/home/$USERNAME"
mkdir -p "$USER_HOME"/{projects,docker,data,.ssh}
chown -R "$USERNAME:$USERNAME" "$USER_HOME"
chmod 700 "$USER_HOME"          # Diğer kullanıcılar bu dizine giremez
chmod 700 "$USER_HOME/.ssh"
log "Klasör yapısı oluşturuldu"

# ── 3. SSH yapılandırması ──────────────────────────────────
# Kullanıcı kendi home dizini dışına çıkamasın (isteğe bağlı sıkılaştırma)
# Not: tam chroot için /etc/ssh/sshd_config'de ek ayar gerekir

# Authorized keys dosyası oluştur (boş)
touch "$USER_HOME/.ssh/authorized_keys"
chmod 600 "$USER_HOME/.ssh/authorized_keys"
chown "$USERNAME:$USERNAME" "$USER_HOME/.ssh/authorized_keys"
log "SSH dizini hazırlandı"

# ── 4. Docker Rootless Kurulumu ────────────────────────────
# Kullanıcı için linger aktifleştir (oturum açmadan servis çalışsın)
loginctl enable-linger "$USERNAME"

# UID ve subuid/subgid ayarları
UID_VAL=$(id -u "$USERNAME")
GID_VAL=$(id -g "$USERNAME")

# subuid/subgid kayıtları ekle (yoksa)
if ! grep -q "^$USERNAME:" /etc/subuid 2>/dev/null; then
    # Kullanılmayan bir blok bul (her kullanıcıya 65536 UID)
    LAST_SUBUID=$(grep -oP ':\K[0-9]+(?=:65536)' /etc/subuid 2>/dev/null | sort -n | tail -1 || echo 100000)
    NEW_SUBUID=$((LAST_SUBUID + 65536))
    echo "$USERNAME:$NEW_SUBUID:65536" >> /etc/subuid
    echo "$USERNAME:$NEW_SUBUID:65536" >> /etc/subgid
    log "subuid/subgid ayarlandı: $NEW_SUBUID"
fi

# Docker rootless kurulumu kullanıcı olarak çalıştır
log "Docker rootless kuruluyor..."
export XDG_RUNTIME_DIR="/run/user/$UID_VAL"
mkdir -p "$XDG_RUNTIME_DIR"
chown "$USERNAME:$USERNAME" "$XDG_RUNTIME_DIR"

# systemd --user için gerekli ortam
runuser -l "$USERNAME" -c "
    export XDG_RUNTIME_DIR=/run/user/$UID_VAL
    export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$UID_VAL/bus

    # Docker rootless kur
    dockerd-rootless-setuptool.sh install --force 2>&1 || true

    # Servisi başlat
    systemctl --user start  docker.service 2>/dev/null || true
    systemctl --user enable docker.service 2>/dev/null || true
" 2>&1 || true

log "Docker rootless kurulumu tamamlandı"

# ── 5. Ortam değişkenleri (.bashrc) ───────────────────────
cat >> "$USER_HOME/.bashrc" << BASHRC

# ── VPS Panel ──────────────────────────────────────────────
export XDG_RUNTIME_DIR=/run/user/$(id -u $USERNAME)
export DOCKER_HOST=unix:///run/user/$(id -u $USERNAME)/docker.sock
export PORT_START=$PORT_START
export PORT_END=$((PORT_START + 999))

# Kolaylık: hangi portları kullanabilirsin?
alias myports='echo "Port bloğunuz: \$PORT_START – \$PORT_END"'

echo "🐳 Docker Rootless hazır | Port bloğu: $PORT_START–$((PORT_START + 999))"
BASHRC

chown "$USERNAME:$USERNAME" "$USER_HOME/.bashrc"

# ── 6. Docker Compose kurulumu ─────────────────────────────
if ! command -v docker-compose &>/dev/null && ! docker compose version &>/dev/null 2>&1; then
    COMPOSE_VERSION="v2.24.0"
    curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
        -o /usr/local/bin/docker-compose
    chmod +x /usr/local/bin/docker-compose
    log "Docker Compose kuruldu"
fi

# ── 7. Örnek docker-compose.yml ───────────────────────────
cat > "$USER_HOME/projects/docker-compose.example.yml" << COMPOSE
# ── Örnek Docker Compose ───────────────────────────────────
# Port bloğunuz: $PORT_START – $((PORT_START + 999))
# Web uygulamaları için önerilen portlar:
#   HTTP  → $((PORT_START + 80))
#   HTTPS → $((PORT_START + 443))
#   DB    → $((PORT_START + 5432))
version: '3.8'

services:
  web:
    image: nginx:alpine
    ports:
      - "$((PORT_START + 80)):80"   # Bu portu Nginx panelden domain ile eşleştirebilirsiniz
    volumes:
      - ./data/web:/usr/share/nginx/html:ro
    restart: unless-stopped
COMPOSE

chown "$USERNAME:$USERNAME" "$USER_HOME/projects/docker-compose.example.yml"

# ── 8. README ─────────────────────────────────────────────
cat > "$USER_HOME/README.md" << README
# VPS Panel — Kullanım Kılavuzu

## SSH Erişimi
\`\`\`bash
ssh $USERNAME@SUNUCU_IP
\`\`\`

## Port Bloğunuz
- Başlangıç : $PORT_START
- Bitiş     : $((PORT_START + 999))

## Docker Kullanımı
\`\`\`bash
# Container başlat
docker run -d -p $((PORT_START+80)):80 nginx:alpine

# Compose ile çalıştır
cd projects/
docker compose up -d

# Container listesi
docker ps
\`\`\`

## Dizin Yapısı
- \`~/projects/\` → Docker Compose projeleriniz
- \`~/data/\`     → Kalıcı verileriniz
- \`~/docker/\`   → Dockerfile'larınız

## Nginx & Domain
Panel üzerinden domain ekleyerek portlarınıza web trafiği yönlendirebilirsiniz.
README

chown "$USERNAME:$USERNAME" "$USER_HOME/README.md"

log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log "Kullanıcı '$USERNAME' başarıyla oluşturuldu"
log "Port bloğu: $PORT_START – $((PORT_START + 999))"
log "Home dizin: $USER_HOME"
log "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
