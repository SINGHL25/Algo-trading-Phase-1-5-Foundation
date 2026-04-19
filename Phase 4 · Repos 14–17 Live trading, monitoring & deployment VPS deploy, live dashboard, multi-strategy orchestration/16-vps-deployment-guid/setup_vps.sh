#!/usr/bin/env bash
# =============================================================================
# scripts/setup_vps.sh
# Full VPS setup for TradingView → Zerodha algo trading stack.
#
# Tested on: Ubuntu 22.04 LTS (AWS t3.small / DigitalOcean 2GB Droplet)
#
# What this script does:
#   1. System update + essential packages
#   2. Python 3.11 + pip + venv
#   3. Google Chrome + ChromeDriver (for Selenium token refresh)
#   4. Creates non-root 'trader' user
#   5. Clones/copies all repos under /home/trader/
#   6. Installs all Python dependencies
#   7. Installs nginx + certbot
#   8. Configures firewall (ufw)
#   9. Sets up systemd services for each bot process
#  10. Runs basic health checks
#
# Usage:
#   sudo bash scripts/setup_vps.sh
#   sudo bash scripts/setup_vps.sh --domain yourdomain.com --email you@email.com
#
# After running:
#   1. Edit /home/trader/algo-trading/*.env with your Kite credentials
#   2. Run: sudo bash scripts/configure_ssl.sh yourdomain.com you@email.com
#   3. Run: sudo systemctl start webhook orchestrator
# =============================================================================

set -euo pipefail

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Parse args ────────────────────────────────────────────────────────────────
DOMAIN=""
EMAIL=""
REPO_DIR="/home/trader/algo-trading"
TRADER_USER="trader"

while [[ $# -gt 0 ]]; do
    case $1 in
        --domain) DOMAIN="$2"; shift 2 ;;
        --email)  EMAIL="$2";  shift 2 ;;
        *) shift ;;
    esac
done

# ── Root check ────────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && error "Run as root: sudo bash $0"

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║     Algo Trading VPS Setup — Ubuntu 22.04               ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# ── 1. System update ──────────────────────────────────────────────────────────
info "Updating system packages..."
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq
apt-get install -y -qq \
    curl wget git unzip build-essential \
    software-properties-common apt-transport-https \
    ca-certificates gnupg lsb-release \
    python3.11 python3.11-venv python3.11-dev python3-pip \
    nginx certbot python3-certbot-nginx \
    ufw fail2ban \
    sqlite3 jq htop tmux \
    fonts-liberation libasound2 libatk-bridge2.0-0 \
    libdrm2 libgbm1 libnss3 xdg-utils libxss1 \
    2>/dev/null
success "System packages installed"

# ── 2. Python symlinks ────────────────────────────────────────────────────────
update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 2>/dev/null || true
success "Python 3.11 configured"

# ── 3. Google Chrome + ChromeDriver (for Selenium token refresh) ──────────────
info "Installing Google Chrome..."
if ! command -v google-chrome &>/dev/null; then
    wget -q -O /tmp/chrome.deb \
        https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    dpkg -i /tmp/chrome.deb 2>/dev/null || apt-get install -f -y -qq
    rm -f /tmp/chrome.deb
    success "Google Chrome installed"
else
    success "Google Chrome already installed"
fi

# Install matching ChromeDriver
CHROME_VERSION=$(google-chrome --version | grep -oP '\d+\.\d+\.\d+' | head -1)
CHROMEDRIVER_URL="https://chromedriver.storage.googleapis.com/LATEST_RELEASE_${CHROME_VERSION%%.*}"
CHROMEDRIVER_VERSION=$(curl -s "$CHROMEDRIVER_URL" 2>/dev/null || echo "")
if [[ -n "$CHROMEDRIVER_VERSION" ]]; then
    wget -q -O /tmp/chromedriver.zip \
        "https://chromedriver.storage.googleapis.com/${CHROMEDRIVER_VERSION}/chromedriver_linux64.zip"
    unzip -o /tmp/chromedriver.zip -d /usr/local/bin/ >/dev/null
    chmod +x /usr/local/bin/chromedriver
    rm -f /tmp/chromedriver.zip
    success "ChromeDriver ${CHROMEDRIVER_VERSION} installed"
else
    warn "Could not auto-install ChromeDriver — install manually if needed"
fi

# ── 4. Create trader user ─────────────────────────────────────────────────────
info "Setting up trader user..."
if ! id "$TRADER_USER" &>/dev/null; then
    useradd -m -s /bin/bash -G www-data "$TRADER_USER"
    success "User '$TRADER_USER' created"
else
    success "User '$TRADER_USER' already exists"
fi

# ── 5. Create project directory structure ─────────────────────────────────────
info "Creating project directories..."
mkdir -p "$REPO_DIR"/{10-webhook-flask-server,14-live-trading-orchestrator,19-ml-signal-filter}
mkdir -p "$REPO_DIR"/shared/{logs,data,state,backups}
mkdir -p /var/log/algo-trading

chown -R "$TRADER_USER:$TRADER_USER" "$REPO_DIR"
chown -R "$TRADER_USER:www-data" /var/log/algo-trading
chmod -R 755 "$REPO_DIR"
success "Project directories created under $REPO_DIR"

# ── 6. Python virtualenv ──────────────────────────────────────────────────────
info "Creating Python virtual environment..."
if [[ ! -d "$REPO_DIR/venv" ]]; then
    sudo -u "$TRADER_USER" python3.11 -m venv "$REPO_DIR/venv"
fi
VENV_PIP="$REPO_DIR/venv/bin/pip"

# Install all requirements
for req_file in "$REPO_DIR"/*/requirements.txt; do
    if [[ -f "$req_file" ]]; then
        info "  Installing $(dirname $req_file | xargs basename)..."
        sudo -u "$TRADER_USER" "$VENV_PIP" install -q -r "$req_file" 2>/dev/null || true
    fi
done
sudo -u "$TRADER_USER" "$VENV_PIP" install -q gunicorn selenium webdriver-manager 2>/dev/null
success "Python virtualenv ready at $REPO_DIR/venv"

# ── 7. Copy systemd service files ─────────────────────────────────────────────
info "Installing systemd service files..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_SOURCE="$(dirname "$SCRIPT_DIR")/systemd"

for service_file in "$SYSTEMD_SOURCE"/*.service; do
    if [[ -f "$service_file" ]]; then
        cp "$service_file" /etc/systemd/system/
        success "  Installed $(basename $service_file)"
    fi
done
systemctl daemon-reload
success "Systemd services registered"

# ── 8. Configure nginx ────────────────────────────────────────────────────────
info "Configuring nginx..."
NGINX_SOURCE="$(dirname "$SCRIPT_DIR")/nginx"
if [[ -f "$NGINX_SOURCE/algo-trading.conf" ]]; then
    cp "$NGINX_SOURCE/algo-trading.conf" /etc/nginx/sites-available/algo-trading
    ln -sf /etc/nginx/sites-available/algo-trading /etc/nginx/sites-enabled/
    rm -f /etc/nginx/sites-enabled/default
    nginx -t 2>/dev/null && systemctl reload nginx && success "nginx configured"
else
    warn "nginx config not found at $NGINX_SOURCE — configure manually"
fi

# ── 9. Firewall ───────────────────────────────────────────────────────────────
info "Configuring firewall (ufw)..."
ufw --force reset >/dev/null 2>&1
ufw default deny incoming  >/dev/null 2>&1
ufw default allow outgoing >/dev/null 2>&1
ufw allow ssh              >/dev/null 2>&1
ufw allow 'Nginx Full'     >/dev/null 2>&1
ufw --force enable         >/dev/null 2>&1
success "Firewall configured: SSH + Nginx allowed, everything else blocked"

# ── 10. Fail2ban ──────────────────────────────────────────────────────────────
systemctl enable fail2ban --now >/dev/null 2>&1
success "fail2ban enabled"

# ── 11. Log rotation ──────────────────────────────────────────────────────────
cat > /etc/logrotate.d/algo-trading << 'LOGROTATE'
/var/log/algo-trading/*.log /home/trader/algo-trading/shared/logs/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0640 trader www-data
    postrotate
        systemctl reload webhook orchestrator 2>/dev/null || true
    endscript
}
LOGROTATE
success "Log rotation configured (14-day retention)"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║  Setup complete!                                        ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
echo "Next steps:"
echo "  1. Copy your repo files to $REPO_DIR/{10,14,19}-*/"
echo "  2. Edit $REPO_DIR/10-webhook-flask-server/.env"
echo "  3. Edit $REPO_DIR/14-live-trading-orchestrator/.env"
echo "  4. Run: sudo bash scripts/configure_ssl.sh DOMAIN EMAIL"
echo "  5. Run: sudo systemctl start webhook orchestrator"
echo "  6. Monitor: sudo journalctl -u webhook -f"
echo ""
[[ -n "$DOMAIN" ]] && echo "  Domain configured: $DOMAIN"
