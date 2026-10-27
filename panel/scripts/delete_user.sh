#!/bin/bash
# ============================================================
#  Kullanıcı Silme Scripti
#  Kullanım: delete_user.sh <username>
# ============================================================
set -euo pipefail

USERNAME="$1"
[[ -z "$USERNAME" ]] && { echo "Kullanım: delete_user.sh <username>" >&2; exit 1; }
[[ "$USERNAME" == "root" || "$USERNAME" == "panel" ]] && \
    { echo "Bu kullanıcı silinemez: $USERNAME" >&2; exit 1; }

log() { echo "[delete_user] $1"; }

# ── Docker rootless durdur ──────────────────────────────────
if id "$USERNAME" &>/dev/null; then
    UID_VAL=$(id -u "$USERNAME")
    # Pasif kullanıcının hesabı süresi dolmuş işaretlidir; runuser bu durumda
    # PAM account kontrolüne takılır. Temizlik için süreyi geçici olarak kaldır.
    chage -E -1 "$USERNAME" 2>/dev/null || true
    runuser -l "$USERNAME" -c "
        export XDG_RUNTIME_DIR=/run/user/$UID_VAL
        systemctl --user stop  docker.service 2>/dev/null || true
        systemctl --user disable docker.service 2>/dev/null || true
        dockerd-rootless-setuptool.sh uninstall 2>/dev/null || true
    " 2>/dev/null || true
    log "Docker rootless durduruldu"

    # Linger kapat
    loginctl disable-linger "$USERNAME" 2>/dev/null || true

    # Kullanıcı processleri öldür
    pkill -u "$USERNAME" 2>/dev/null || true
    sleep 1

    # Linux kullanıcısını sil (home dahil)
    userdel -r "$USERNAME" 2>/dev/null || userdel "$USERNAME" 2>/dev/null || true
    rm -rf "/home/$USERNAME" 2>/dev/null || true
    log "Linux kullanıcısı silindi: $USERNAME"
fi

# subuid/subgid kayıtlarını temizle
sed -i "/^$USERNAME:/d" /etc/subuid /etc/subgid 2>/dev/null || true

# XDG runtime temizle
rm -rf "/run/user/$(id -u "$USERNAME" 2>/dev/null || echo 99999)" 2>/dev/null || true

log "Kullanıcı '$USERNAME' tamamen silindi"
