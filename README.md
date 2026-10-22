# VPS Panel

[English](README.md) | [Türkçe](README.tr.md)

A web control panel for managing Linux users, per-user rootless Docker environments, domains, and HTTPS on a VPS. Built with FastAPI, Jinja2, SQLite, and Nginx. The current web interface is in Turkish.

## Features

- Dashboard with CPU, memory, and disk usage.
- Create, activate, deactivate, and delete Linux users.
- Per-user rootless Docker setup, project directories, and a sample Compose file.
- Assign a block of 1,000 ports to each user and validate domain targets against that block.
- Route domains to local application ports through Nginx.
- Request and renew Let's Encrypt certificates using Certbot.
- Configure a dedicated panel domain and HTTPS from the settings page.
- Activity log of logins (including failed attempts), user, domain, SSL, and settings changes, with filters on the Logs page and a recent-activity card on the dashboard. The latest 10,000 entries are kept in `panel.db`.
- List running containers for each user.
- Installer for systemd, UFW, Fail2ban, Nginx, Docker, and Python dependencies.

## Requirements

- An Ubuntu or Debian server with systemd, `apt`, and root access.
- Python 3.10 or newer for the application's type annotations. The installer checks the distribution name, but does not enforce OS or Python versions.
- Internet access to download system packages and dependencies.
- Git to clone this repository.
- For domain certificates: DNS records pointing to the server and publicly reachable ports 80 and 443.

Use a fresh VPS where possible. `setup.sh` upgrades packages, resets UFW rules, replaces the default Nginx site, configures Fail2ban, and changes `/home` permissions. The panel service runs as root to manage system resources.

## Installation

```bash
git clone https://github.com/crazymesut/control-panel.git
cd control-panel
sudo bash setup.sh
```

The installer copies the application to `/opt/vps-panel`, creates a Python virtual environment, and enables the `vps-panel` systemd service.

To configure installation with a file, copy [`.env.example`](.env.example), restrict its permissions, and edit `.env`:

```bash
cp .env.example .env
chmod 600 .env
```

Set your email and administrator username. Leave `PANEL_PASSWORD` empty for a generated password. Then explicitly load the file for setup; the installer does not read the checkout's `.env` automatically:

```bash
sudo bash -c 'set -a; source ./.env; set +a; bash setup.sh'
```

`.env` is excluded from Git. Only source a file you trust: Bash executes its contents. `PANEL_PORT_INTERNAL` is calculated by setup as `PANEL_PORT + 10000`; the commented example is for runtime reference.

Alternatively, pass configuration directly:

```bash
sudo env PANEL_ADMIN=admin \
  PANEL_EMAIL=you@example.com \
  PANEL_PORT=8765 \
  bash setup.sh
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `PANEL_ADMIN` | `admin` | Initial administrator username |
| `PANEL_PASSWORD` | Randomly generated | Initial administrator password |
| `PANEL_EMAIL` | `admin@example.com` | Email passed to Certbot |
| `PANEL_PORT` | `8765` | External HTTPS port for the panel |

Set `PANEL_EMAIL` to a real address before requesting certificates. Generated credentials are printed at installation and saved in `/root/vps-panel-credentials.txt`. The service environment is stored in `/opt/vps-panel/.env` with mode `600`.

### Current installation caveats

- The repository includes `panel/panel.db`, and setup copies it to the installation directory. For a fresh installation, move this bundled database out of `panel/` before running setup so that the application creates a new database. Environment variables create an admin only when that username does not already exist; they do not reset existing passwords.
- The installer writes the Nginx upstream as `${PANEL_PORT}0`, but starts Uvicorn on `PANEL_PORT + 10000`. With defaults these are `87650` and `18765`. Before installation, correct the `proxy_pass` line in `setup.sh` to use `http://127.0.0.1:$((PANEL_PORT + 10000))`. Otherwise Nginx validation or panel access can fail.
- On a retry, if Nginx reports that `sites-enabled/vps-users` is a directory, check that `/etc/nginx/nginx.conf` includes `/etc/nginx/sites-enabled/*.conf` and `/etc/nginx/sites-enabled/vps-users/*.conf`. Setup currently adjusts these includes after its first Nginx validation.

After installation, open `https://SERVER_IP:8765` (or your configured port). The initial certificate is self-signed, so browsers display a certificate warning. A panel domain with a Let's Encrypt certificate can be configured under **Ayarlar** (Settings).

## Usage

1. Sign in with the administrator credentials.
2. Open **Kullanıcılar** (Users) and create a user with a username, SSH password, and main domain. Usernames must start with a lowercase letter and contain 3–31 lowercase letters, digits, or underscores.
3. The first user receives ports `10000–10999`; its main domain initially targets port `10080`. Later users receive subsequent blocks.
4. Connect over SSH and start an application on the assigned port:

   ```bash
   ssh alice@SERVER_IP
   docker run -d --name web -p 10080:80 nginx:alpine
   docker ps
   myports
   ```

5. Point the domain's DNS to the server. Use **Domainler** (Domains) to add more domains or request SSL. Request SSL separately for the main domain after creating the user.

Each user receives `~/projects`, `~/docker`, `~/data`, and `~/.ssh`. To use the generated Compose example, create some web content first:

```bash
cd ~/projects
mkdir -p data/web
echo '<h1>Hello from VPS Panel</h1>' > data/web/index.html
docker compose -f docker-compose.example.yml up -d
```

Use either this example or the `docker run` example on port `10080`; running both on the same port causes a conflict.

Deactivation locks the account password, removes domain symlinks from Nginx, and attempts to stop the user's Docker service. Deleting a user removes the Linux account, home directory, and domain configurations. Back up user data before deletion.

### Port allocation limits

UFW opens TCP ports `22`, `80`, `443`, the configured panel port, and `10000–59999`. The allocator currently generates prefixes `10–99`, including blocks outside that firewall range and above the maximum TCP port `65535`. Keep assigned blocks within `10000–59999` until the allocator is corrected. Port blocks are checked when adding domain targets; they are not enforced as per-user network restrictions.

## Updates and operations

From your repository checkout, obtain the desired revision and run:

```bash
git pull --ff-only
sudo bash update.sh
```

The update script replaces application files, preserves the installed `panel.db` and `settings.json`, upgrades Python dependencies, and restarts the service. It leaves `.env` in place and does not rerun system, firewall, or Nginx setup. Back up the installation and user data before updating. Restarting the service clears in-memory login sessions.

```bash
sudo systemctl status vps-panel
sudo journalctl -u vps-panel -n 100 --no-pager
sudo systemctl restart vps-panel
sudo nginx -t
sudo ufw status
```

For backups, include `/opt/vps-panel` (especially `panel.db`, `.env`, and `settings.json`), `/home`, relevant Nginx configurations, and `/etc/letsencrypt`. Stop the panel service while copying its SQLite database for a consistent file backup. Protect backups because they contain credentials, certificates, and user data.

## Project layout

```text
control-panel/
├── setup.sh                  # System and application installer
├── update.sh                 # Application updater
└── panel/
    ├── main.py               # FastAPI application and SQLite initialization
    ├── panel.db              # Bundled SQLite database; see installation caveats
    ├── scripts/
    │   ├── create_user.sh    # Linux user and rootless Docker provisioning
    │   └── delete_user.sh    # User and home directory removal
    └── templates/            # Jinja2 web interface
```

The installer also creates `static/`, `.env`, and `venv/` in `/opt/vps-panel`. Panel domain settings are saved to `settings.json` when configured. The web interface loads Tailwind CSS from a CDN.
