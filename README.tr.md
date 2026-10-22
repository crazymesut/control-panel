# VPS Panel

[English](README.md) | [Türkçe](README.tr.md)

Bir VPS üzerinde Linux kullanıcılarını, kullanıcı bazlı rootless Docker ortamlarını, domainleri ve HTTPS yapılandırmasını yönetmek için web kontrol paneli. FastAPI, Jinja2, SQLite ve Nginx ile geliştirilmiştir. Mevcut web arayüzü Türkçedir.

## Özellikler

- CPU, bellek ve disk kullanımını gösteren dashboard.
- Linux kullanıcısı oluşturma, etkinleştirme, devre dışı bırakma ve silme.
- Her kullanıcı için rootless Docker kurulumu, proje dizinleri ve örnek Compose dosyası.
- Kullanıcı başına 1.000 portluk blok atama ve domain hedeflerini bu blokla doğrulama.
- Nginx üzerinden domainleri yerel uygulama portlarına yönlendirme.
- Certbot ile Let's Encrypt sertifikası alma ve yenileme.
- Ayarlar sayfasından panele özel domain ve HTTPS tanımlama.
- Kullanıcıların çalışan containerlarını listeleme.
- systemd, UFW, Fail2ban, Nginx, Docker ve Python bağımlılıklarını hazırlayan kurulum betiği.

## Gereksinimler

- systemd ve `apt` kullanan, root erişimine sahip Ubuntu veya Debian sunucu.
- Uygulamanın tip tanımları için Python 3.10 veya üzeri. Kurulum betiği dağıtım adını kontrol eder; işletim sistemi veya Python sürümünü zorunlu tutmaz.
- Sistem paketlerini ve bağımlılıkları indirmek için internet bağlantısı.
- Depoyu klonlamak için Git.
- Domain sertifikaları için sunucuya yönlendirilmiş DNS kayıtları ve dışarıdan erişilebilen 80 ve 443 portları.

Mümkünse temiz bir VPS kullanın. `setup.sh` paketleri yükseltir, UFW kurallarını sıfırlar, varsayılan Nginx sitesini değiştirir, Fail2ban yapılandırır ve `/home` izinlerini değiştirir. Panel servisi sistem kaynaklarını yönetebilmek için root olarak çalışır.

## Kurulum

```bash
git clone https://github.com/crazymesut/control-panel.git
cd control-panel
sudo bash setup.sh
```

Kurulum betiği uygulamayı `/opt/vps-panel` dizinine kopyalar, Python sanal ortamını oluşturur ve `vps-panel` systemd servisini etkinleştirir.

Dosya üzerinden yapılandırmak için [`.env.example`](.env.example) dosyasını kopyalayın, izinlerini sınırlandırın ve `.env` dosyasını düzenleyin:

```bash
cp .env.example .env
chmod 600 .env
```

E-posta adresinizi ve yönetici kullanıcı adınızı belirtin. Rastgele şifre için `PANEL_PASSWORD` değerini boş bırakın. Ardından dosyayı açıkça yükleyerek kurulumu çalıştırın; betik depo dizinindeki `.env` dosyasını otomatik okumaz:

```bash
sudo bash -c 'set -a; source ./.env; set +a; bash setup.sh'
```

`.env` Git takibinin dışındadır. Bash dosyanın içeriğini çalıştırdığı için yalnızca güvendiğiniz dosyaları yükleyin. `PANEL_PORT_INTERNAL` kurulumda `PANEL_PORT + 10000` olarak hesaplanır; yorum satırındaki örnek çalışma zamanı içindir.

Alternatif olarak değişkenleri doğrudan iletebilirsiniz:

```bash
sudo env PANEL_ADMIN=admin \
  PANEL_EMAIL=siz@example.com \
  PANEL_PORT=8765 \
  bash setup.sh
```

| Değişken | Varsayılan | Açıklama |
| --- | --- | --- |
| `PANEL_ADMIN` | `admin` | İlk yönetici kullanıcı adı |
| `PANEL_PASSWORD` | Rastgele oluşturulur | İlk yönetici şifresi |
| `PANEL_EMAIL` | `admin@example.com` | Certbot'a iletilen e-posta adresi |
| `PANEL_PORT` | `8765` | Panelin dış HTTPS portu |

Sertifika istemeden önce `PANEL_EMAIL` için gerçek bir adres belirtin. Oluşturulan giriş bilgileri kurulum sonunda gösterilir ve `/root/vps-panel-credentials.txt` dosyasına kaydedilir. Servis ortam değişkenleri `600` dosya izniyle `/opt/vps-panel/.env` içinde saklanır.

### Mevcut kurulumla ilgili notlar

- Depoda `panel/panel.db` bulunur ve kurulum bu dosyayı hedef dizine kopyalar. Temiz kurulumda uygulamanın yeni bir veritabanı oluşturması için bu dosyayı kurulumu çalıştırmadan önce `panel/` dışına taşıyın. Ortam değişkenleri yalnızca aynı kullanıcı adı mevcut değilse yönetici oluşturur; mevcut şifreleri sıfırlamaz.
- Kurulum betiği Nginx hedefini `${PANEL_PORT}0` olarak yazarken Uvicorn'u `PANEL_PORT + 10000` portunda başlatır. Varsayılan değerlerle bunlar `87650` ve `18765` olur. Kurulumdan önce `setup.sh` içindeki `proxy_pass` satırının hedefini `http://127.0.0.1:$((PANEL_PORT + 10000))` olarak düzeltin. Aksi durumda Nginx doğrulaması veya panele erişim başarısız olabilir.
- Kurulumu tekrar çalıştırırken Nginx, `sites-enabled/vps-users` yolunun bir dizin olduğunu bildirirse `/etc/nginx/nginx.conf` içinde `/etc/nginx/sites-enabled/*.conf` ve `/etc/nginx/sites-enabled/vps-users/*.conf` include satırlarını kontrol edin. Betik bu düzenlemeleri şu anda ilk Nginx doğrulamasından sonra yapar.

Kurulumdan sonra `https://SUNUCU_IP:8765` adresini açın; farklı port belirlediyseniz onu kullanın. İlk sertifika kendinden imzalı olduğu için tarayıcı sertifika uyarısı gösterir. **Ayarlar** bölümünden panel domaini tanımlayıp Let's Encrypt sertifikası alabilirsiniz.

## Kullanım

1. Yönetici bilgileriyle giriş yapın.
2. **Kullanıcılar** bölümünde kullanıcı adı, SSH şifresi ve ana domain girerek kullanıcı oluşturun. Kullanıcı adı küçük harfle başlamalı; 3–31 küçük harf, rakam veya alt çizgiden oluşmalıdır.
3. İlk kullanıcıya `10000–10999` port bloğu atanır; ana domain başlangıçta `10080` portuna yönlendirilir. Sonraki kullanıcılara sıradaki bloklar atanır.
4. SSH ile bağlanıp atanmış portta bir uygulama başlatın:

   ```bash
   ssh alice@SUNUCU_IP
   docker run -d --name web -p 10080:80 nginx:alpine
   docker ps
   myports
   ```

5. Domainin DNS kayıtlarını sunucuya yönlendirin. **Domainler** bölümünden ek domain tanımlayabilir veya SSL alabilirsiniz. Kullanıcı oluşturulduktan sonra ana domain için SSL'yi ayrıca isteyin.

Her kullanıcı için `~/projects`, `~/docker`, `~/data` ve `~/.ssh` dizinleri hazırlanır. Oluşturulan Compose örneğini kullanmak için önce web içeriğini oluşturun:

```bash
cd ~/projects
mkdir -p data/web
echo '<h1>VPS Panel ile merhaba</h1>' > data/web/index.html
docker compose -f docker-compose.example.yml up -d
```

`10080` portunda bu örneği veya `docker run` örneğini kullanın; ikisini aynı portta çalıştırmak port çakışmasına yol açar.

Kullanıcı devre dışı bırakıldığında hesap şifresi kilitlenir, domainlerin Nginx bağlantıları kaldırılır ve kullanıcının Docker servisi durdurulmaya çalışılır. Kullanıcı silindiğinde Linux hesabı, ev dizini ve domain yapılandırmaları kaldırılır. Silmeden önce kullanıcı verilerini yedekleyin.

### Port atama sınırları

UFW; TCP `22`, `80`, `443`, seçilen panel portu ve `10000–59999` aralığını açar. Mevcut port dağıtıcı `10–99` öneklerini üretir; bunların bir kısmı güvenlik duvarı aralığının ve en büyük TCP portu olan `65535` sınırının dışındadır. Dağıtıcı düzeltilene kadar atanan blokları `10000–59999` içinde tutun. Port bloğu domain hedefi eklerken kontrol edilir; kullanıcı bazlı ağ kısıtlaması olarak uygulanmaz.

## Güncelleme ve bakım

Depo dizininde istediğiniz sürümü alıp güncelleme betiğini çalıştırın:

```bash
git pull --ff-only
sudo bash update.sh
```

Güncelleme betiği uygulama dosyalarını değiştirir, kurulu `panel.db` ve `settings.json` dosyalarını korur, Python bağımlılıklarını yükseltir ve servisi yeniden başlatır. `.env` dosyasını yerinde bırakır; sistem, güvenlik duvarı veya Nginx kurulumunu tekrar çalıştırmaz. Güncellemeden önce kurulumu ve kullanıcı verilerini yedekleyin. Servisin yeniden başlaması bellekteki giriş oturumlarını temizler.

```bash
sudo systemctl status vps-panel
sudo journalctl -u vps-panel -n 100 --no-pager
sudo systemctl restart vps-panel
sudo nginx -t
sudo ufw status
```

Yedeklere `/opt/vps-panel` dizinini (özellikle `panel.db`, `.env` ve `settings.json`), `/home`, ilgili Nginx yapılandırmalarını ve `/etc/letsencrypt` dizinini dahil edin. Tutarlı bir dosya yedeği için SQLite veritabanını kopyalarken panel servisini durdurun. Yedekler giriş bilgileri, sertifikalar ve kullanıcı verileri içerdiğinden bunları koruyun.

## Proje yapısı

```text
control-panel/
├── setup.sh                  # Sistem ve uygulama kurulumu
├── update.sh                 # Uygulama güncellemesi
└── panel/
    ├── main.py               # FastAPI uygulaması ve SQLite başlangıç işlemleri
    ├── panel.db              # Depodaki SQLite veritabanı; kurulum notlarına bakın
    ├── scripts/
    │   ├── create_user.sh    # Linux kullanıcısı ve rootless Docker kurulumu
    │   └── delete_user.sh    # Kullanıcı ve ev dizini silme
    └── templates/            # Jinja2 web arayüzü
```

Kurulum ayrıca `/opt/vps-panel` altında `static/`, `.env` ve `venv/` oluşturur. Panel domaini yapılandırıldığında ayarlar `settings.json` dosyasına kaydedilir. Web arayüzü Tailwind CSS'i CDN üzerinden yükler.
