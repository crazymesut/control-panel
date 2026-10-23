# ============================================================
#  VPS Panel — Test Ortamı (Ubuntu 24.04 + systemd)
#  setup.sh / update.sh ve panel akışlarını (kullanıcı ekle/sil/güncelle,
#  domain, nginx) gerçek bir sunucuya dokunmadan denemek içindir.
#
#  ÜRETİMDE KULLANMAYIN: setup.sh systemd, UFW ve Fail2ban ister; bu
#  yüzden container ayrıcalıklı (privileged) çalışır.
#  Kullanım için bkz. docker/compose.yml
# ============================================================
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    container=docker

# systemd + test araçları. Aşağıdaki liste setup.sh adım 1'dekinin aynısıdır;
# burada yalnızca katman önbelleğine alınır, setup.sh yine de apt-get çalıştırır.
# (setup.sh paket listesi değişirse bu listeyi de güncelleyin.)
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends \
      systemd systemd-sysv dbus iproute2 iputils-ping procps sudo \
      ca-certificates sqlite3 jq sshpass \
 && apt-get install -y -qq \
      curl wget git unzip \
      python3 python3-pip python3-venv \
      nginx certbot python3-certbot-nginx \
      fail2ban ufw \
      uidmap dbus-user-session slirp4netns fuse-overlayfs \
      openssh-server openssl \
      htop net-tools \
 && rm -rf /var/lib/apt/lists/*

# Container içinde anlamsız ya da başarısız olan systemd birimlerini kapat
RUN systemctl mask \
      systemd-udevd.service systemd-udevd-kernel.socket systemd-udevd-control.socket \
      systemd-udev-trigger.service systemd-modules-load.service \
      systemd-remount-fs.service systemd-binfmt.service systemd-firstboot.service \
      sys-kernel-debug.mount sys-kernel-tracing.mount dev-hugepages.mount \
      getty.target console-getty.service getty@.service serial-getty@.service

# Repo, çalışma zamanında /src olarak bağlanır (bkz. compose.yml)
WORKDIR /src

# systemd temiz kapanış sinyali
STOPSIGNAL SIGRTMIN+3

CMD ["/sbin/init"]
