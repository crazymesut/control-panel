# VPS Panel

[English](README.md) | [Türkçe](README.tr.md)

A web control panel for managing Linux users, per-user rootless Docker environments, domains, and HTTPS on a VPS. Built with FastAPI, Jinja2, SQLite, and Nginx. The current web interface is in Turkish.

## Features

- Dashboard with CPU, memory, and disk usage.
- Create, activate, deactivate, and delete Linux users.
- Per-user rootless Docker setup, project directories, and a sample Compose file.
- Assign a block of 1,000 ports to each user and validate domain targets against that block.
- Route domains to local application ports through Nginx.
- View and edit the server's Nginx configuration files on the Nginx page. Saving runs `nginx -t` and reloads only if it passes; otherwise the file is restored automatically. Each successful save keeps a backup in `/var/backups/vps-panel-nginx` (latest 20 per file).
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

- The repository ships only a schema template, `panel/panel.db.example`. Setup copies it to `/opt/vps-panel/panel.db` only when that file does not exist; an existing database is never touched, so re-running setup or updating cannot overwrite live data. Environment variables create an admin only when that username does not already exist; they do not reset existing passwords, so re-running setup on an existing installation prints a new password that does not apply.
- Setup installs a catch-all default server in `/etc/nginx/conf.d/00-default.conf`: requests for unknown hosts or the bare IP on ports 80/443 are dropped instead of being served by whichever vhost sorts first. Open the panel on `https://SERVER_IP:8765`; port 80 on the bare IP no longer redirects.

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

The **Nginx** page lists `nginx.conf`, `conf.d/*.conf`, `sites-available/*`, and the per-user domain files. Pick a file, edit it, and press **Kaydet & Doğrula** (or Ctrl/⌘+S). Files that are not linked into `sites-enabled` are marked inactive; `nginx -t` does not read them, so their syntax is not validated. The panel regenerates a domain's file when that domain is added (or the panel domain is set again), which overwrites manual edits to that file.

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

The update script replaces application files, never touches the installed `panel.db`, `settings.json`, or `.env`, upgrades Python dependencies, and restarts the service. It leaves `.env` in place and does not rerun system, firewall, or Nginx setup. Back up the installation and user data before updating. Restarting the service clears in-memory login sessions.

```bash
sudo systemctl status vps-panel
sudo journalctl -u vps-panel -n 100 --no-pager
sudo systemctl restart vps-panel
sudo nginx -t
sudo ufw status
```

For backups, include `/opt/vps-panel` (especially `panel.db`, `.env`, and `settings.json`), `/home`, relevant Nginx configurations, and `/etc/letsencrypt`. Stop the panel service while copying its SQLite database for a consistent file backup. Protect backups because they contain credentials, certificates, and user data.

### Update from the panel

**Settings → Panel Update → Update** runs `deploy.sh` on the server: `git pull --ff-only`, `update.sh`, then a health check of the panel. It starts as a separate systemd unit (`vps-panel-update`), so the service restart does not kill it; the output is kept in `/var/log/vps-panel-update.log` and shown on the page. The restart clears login sessions, so sign in again afterwards. You can also run `sudo bash deploy.sh` on the server instead of the two commands above.

The button needs to know which checkout to pull. `setup.sh` and `update.sh` record it in `/opt/vps-panel/.repo_dir` when run from a git checkout, so run `sudo bash update.sh` once from the checkout after upgrading to this version.

### CI/CD with GitHub Actions

`.github/workflows/ci-cd.yml` checks every push and pull request (shell syntax, `main.py` imports, every template compiles). A push to `main` that passes the checks then connects to the server over SSH and runs `deploy.sh`.

The server's checkout must be able to `git pull` without prompting (public repository, or a read-only deploy key or token configured on the server). One-time setup:

1. Create a key used only for deploys: `ssh-keygen -t ed25519 -N '' -C github-deploy -f deploy_key`.
2. On the server, add the public key to `/root/.ssh/authorized_keys` locked to the deploy command (adjust the path to your checkout), so a leaked key can do nothing else:

   ```text
   command="cd /root/vps-panel && bash deploy.sh",restrict ssh-ed25519 AAAA... github-deploy
   ```

3. In the repository under **Settings → Secrets and variables → Actions**, add the secrets `DEPLOY_HOST` (server address), `DEPLOY_SSH_KEY` (contents of the private `deploy_key`) and `DEPLOY_KNOWN_HOSTS` (output of `ssh-keyscan -p 22 <host>`; compare it with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the server). Optional variables: `DEPLOY_USER` (default `root`), `DEPLOY_PORT` (default `22`), `DEPLOY_PATH` (default `~/vps-panel`).
4. Delete the local `deploy_key` and `deploy_key.pub`.

Anyone who can push to `main` can run code as root on the server through this pipeline, so enable branch protection on `main`. A run can also be started manually from the Actions tab.

## Project layout

```text
control-panel/
├── setup.sh                  # System and application installer
├── update.sh                 # Application updater
├── deploy.sh                 # git pull + update.sh + health check (panel button, CI/CD)
├── .github/workflows/
│   └── ci-cd.yml             # Checks on every push; deploys main to the server over SSH
└── panel/
    ├── main.py               # FastAPI application and SQLite initialization
    ├── panel.db.example      # Empty SQLite schema; copied to panel.db on first install only
    ├── scripts/
    │   ├── create_user.sh    # Linux user and rootless Docker provisioning
    │   └── delete_user.sh    # User and home directory removal
    └── templates/            # Jinja2 web interface
```

The installer also creates `static/`, `.env`, and `venv/` in `/opt/vps-panel`. Panel domain settings are saved to `settings.json` when configured. The web interface loads Tailwind CSS from a CDN.
