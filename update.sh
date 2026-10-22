#!/bin/bash
# ============================================================
#  VPS Panel — Güncelleme Scripti
#  Kullanım: sudo bash update.sh
# ============================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
log()   { echo -e "${GREEN}[✓]${NC} $1"; }
info()  { echo -e "${BLUE}[i]${NC} $1"; }
error() { echo -e "${RED}[✗]${NC} $1"; exit 1; }

[[ $EUID -ne 0 ]] && error "Bu script root olarak çalıştırılmalıdır: sudo bash update.sh"

PANEL_DIR="/opt/vps-panel"
PANEL_SERVICE="vps-panel"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[[ ! -d "$PANEL_DIR" ]] && error "Panel kurulu değil. Önce setup.sh çalıştırın."
[[ ! -d "$SCRIPT_DIR/panel" ]] && error "panel/ dizini bulunamadı. update.sh ile aynı dizinde olmalı."

echo -e "\n${BOLD}━━━ VPS Panel Güncelleniyor ━━━${NC}\n"

# Panel dosyalarını kopyala (panel.db, settings.json ve .env hiç dokunulmaz)
info "Dosyalar kopyalanıyor..."
tar -C "$SCRIPT_DIR/panel" \
    --exclude='panel.db' --exclude='panel.db-*' --exclude='panel.db.example' \
    --exclude='settings.json' --exclude='.env' \
    -cf - . | tar -C "$PANEL_DIR" -xf -
log "Panel dosyaları güncellendi (DB ve ayarlar korundu)"

# Yeni Python bağımlılıklarını yükle
info "Python bağımlılıkları kontrol ediliyor..."
"$PANEL_DIR/venv/bin/pip" install -q --upgrade \
    fastapi "uvicorn[standard]" jinja2 python-multipart \
    bcrypt aiofiles psutil
log "Python bağımlılıkları güncellendi"

# Servisi yeniden başlat
systemctl restart "$PANEL_SERVICE"
sleep 2

if systemctl is-active --quiet "$PANEL_SERVICE"; then
    log "Panel servisi yeniden başlatıldı"
else
    error "Servis başlatılamadı! Kontrol: journalctl -u $PANEL_SERVICE -n 30"
fi

echo -e "\n${GREEN}${BOLD}✓ Güncelleme tamamlandı!${NC}\n"
