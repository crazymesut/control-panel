#!/bin/bash
# ============================================================
#  VPS Panel — Dağıtım Scripti
#  Kodu çeker (git pull --ff-only), update.sh'i çalıştırır ve panelin
#  gerçekten yanıt verdiğini doğrular.
#  Çağıranlar: panel butonu (Ayarlar > Panel Güncelleme),
#  GitHub Actions (.github/workflows/ci-cd.yml) ve elle kullanım.
#  Kullanım (repo checkout'unda): sudo bash deploy.sh
# ============================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
log()   { echo -e "${GREEN}[✓]${NC} $1"; }
info()  { echo -e "${BLUE}[i]${NC} $1"; }
error() { echo -e "${RED}[✗]${NC} $1"; exit 1; }

[[ $EUID -ne 0 ]] && error "Bu script root olarak çalıştırılmalıdır: sudo bash deploy.sh"

# systemd-run altında HOME tanımsız olabilir; git yapılandırması/kimlik bilgileri için gerekli
export HOME="${HOME:-/root}"

PANEL_DIR="/opt/vps-panel"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo -e "\n${BOLD}━━━ VPS Panel Dağıtımı ━━━${NC}\n"

# Aynı anda tek dağıtım (panel butonu ile CI çakışmasın)
exec 9>/run/vps-panel-deploy.lock
flock -n 9 || error "Başka bir dağıtım çalışıyor"

cd "$SCRIPT_DIR"
[[ -e .git ]] || error "$SCRIPT_DIR bir git deposu değil"

OLD="$(git rev-parse --short HEAD)"
info "Kod çekiliyor (git pull --ff-only)..."
git pull --ff-only || error "git pull başarısız (yerel değişiklik ya da ayrışmış dal olabilir). Panele dokunulmadı."
NEW="$(git rev-parse --short HEAD)"
if [[ "$OLD" == "$NEW" ]]; then
    log "Kod zaten güncel ($NEW)"
else
    log "Kod güncellendi: $OLD → $NEW"
fi

# update.sh dosyaları kopyalar, bağımlılıkları yükler ve servisi yeniden başlatır
bash "$SCRIPT_DIR/update.sh"

# Servis "active" olsa da uygulama ayağa kalkmamış olabilir; /login'e istek at
PORT="$(grep -E '^PANEL_PORT_INTERNAL=[0-9]+$' "$PANEL_DIR/.env" 2>/dev/null | cut -d= -f2 || true)"
PORT="${PORT:-18765}"
info "Panel sağlık kontrolü (127.0.0.1:$PORT)..."
healthy=0
for _ in $(seq 1 20); do
    if curl -fsS -o /dev/null --max-time 3 "http://127.0.0.1:$PORT/login" 2>/dev/null; then
        healthy=1
        break
    fi
    sleep 1
done
[[ $healthy -eq 1 ]] || error "Panel yanıt vermiyor! Kontrol: journalctl -u vps-panel -n 30"
log "Panel yanıt veriyor"

echo -e "\n${GREEN}${BOLD}✓ Dağıtım tamamlandı: $NEW${NC}\n"
