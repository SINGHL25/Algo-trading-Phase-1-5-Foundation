#!/usr/bin/env bash
# =============================================================================
# scripts/setup_cloudflare_tunnel.sh
# Sets up a Cloudflare Tunnel as an ngrok alternative.
#
# Why Cloudflare Tunnel instead of ngrok:
#   - Free tier is persistent (ngrok URLs change on restart unless paid)
#   - No port forwarding required on your router/firewall
#   - Works behind NAT/CGNAT (common on mobile broadband)
#   - Lower latency (Cloudflare edge vs ngrok US servers)
#   - TLS is handled by Cloudflare — no certbot needed
#
# Prerequisites:
#   1. Free Cloudflare account at https://dash.cloudflare.com
#   2. A domain added to Cloudflare (even a free .workers.dev subdomain works)
#
# What this script does:
#   1. Installs cloudflared
#   2. Logs in (opens browser or prints auth URL)
#   3. Creates a named tunnel
#   4. Routes yourdomain.com/webhook → localhost:5000
#   5. Installs cloudflared as a systemd service
#
# Usage:
#   bash scripts/setup_cloudflare_tunnel.sh --domain yoursite.com --tunnel-name algo-trading
#   bash scripts/setup_cloudflare_tunnel.sh --domain yoursite.workers.dev --tunnel-name algo
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

DOMAIN=""
TUNNEL_NAME="algo-trading"

while [[ $# -gt 0 ]]; do
    case $1 in
        --domain)      DOMAIN="$2";      shift 2 ;;
        --tunnel-name) TUNNEL_NAME="$2"; shift 2 ;;
        *) shift ;;
    esac
done

[[ -z "$DOMAIN" ]] && error "Usage: bash $0 --domain yoursite.com [--tunnel-name algo-trading]"

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Cloudflare Tunnel Setup (ngrok alternative)                ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
info "Domain: $DOMAIN | Tunnel: $TUNNEL_NAME"

# ── Install cloudflared ────────────────────────────────────────────────────────
info "Installing cloudflared..."
if ! command -v cloudflared &>/dev/null; then
    ARCH=$(uname -m)
    case "$ARCH" in
        x86_64) CF_ARCH="amd64" ;;
        aarch64|arm64) CF_ARCH="arm64" ;;
        *) error "Unsupported architecture: $ARCH" ;;
    esac
    wget -q -O /usr/local/bin/cloudflared \
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}"
    chmod +x /usr/local/bin/cloudflared
    success "cloudflared installed: $(cloudflared --version)"
else
    success "cloudflared already installed: $(cloudflared --version)"
fi

# ── Authenticate ───────────────────────────────────────────────────────────────
info "Authenticating with Cloudflare..."
echo ""
echo "  This will open a browser or print an auth URL."
echo "  Log in with your Cloudflare account and authorise the tunnel."
echo ""
cloudflared tunnel login
success "Authenticated with Cloudflare"

# ── Create tunnel ──────────────────────────────────────────────────────────────
info "Creating tunnel: $TUNNEL_NAME..."
EXISTING=$(cloudflared tunnel list 2>/dev/null | grep "$TUNNEL_NAME" || echo "")
if [[ -z "$EXISTING" ]]; then
    cloudflared tunnel create "$TUNNEL_NAME"
    success "Tunnel '$TUNNEL_NAME' created"
else
    success "Tunnel '$TUNNEL_NAME' already exists"
fi

# Get tunnel ID
TUNNEL_ID=$(cloudflared tunnel list 2>/dev/null | grep "$TUNNEL_NAME" | awk '{print $1}')
[[ -z "$TUNNEL_ID" ]] && error "Could not get tunnel ID"
info "Tunnel ID: $TUNNEL_ID"

# ── Create tunnel config ───────────────────────────────────────────────────────
CF_CONFIG_DIR="$HOME/.cloudflared"
mkdir -p "$CF_CONFIG_DIR"

cat > "$CF_CONFIG_DIR/config.yml" << CONFIG
tunnel: ${TUNNEL_ID}
credentials-file: ${CF_CONFIG_DIR}/${TUNNEL_ID}.json

ingress:
  # Webhook server (repo 10)
  - hostname: ${DOMAIN}
    path: /webhook
    service: http://localhost:5000

  # Orchestrator (repo 14)
  - hostname: ${DOMAIN}
    path: /(trade|health|status|positions|config|admin)
    service: http://localhost:5001

  # ML filter (repo 19) — remove if not used
  - hostname: ${DOMAIN}
    path: /signal
    service: http://localhost:5002

  # Catch-all — required by cloudflared
  - service: http_status:404
CONFIG

success "Tunnel config written to $CF_CONFIG_DIR/config.yml"

# ── Create DNS route ───────────────────────────────────────────────────────────
info "Creating DNS route: $DOMAIN → tunnel..."
cloudflared tunnel route dns "$TUNNEL_NAME" "$DOMAIN" || warn "DNS route may already exist"
success "DNS route configured"

# ── Install as systemd service ────────────────────────────────────────────────
info "Installing cloudflared as systemd service..."
cloudflared service install 2>/dev/null || true
systemctl enable cloudflared --now 2>/dev/null || true
success "cloudflared systemd service enabled"

# ── Test ─────────────────────────────────────────────────────────────────────
sleep 3
HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "https://${DOMAIN}/health" 2>/dev/null || echo "000")

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Cloudflare Tunnel active!                                  ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  Tunnel name: $TUNNEL_NAME"
echo "  Tunnel ID:   $TUNNEL_ID"
echo ""
echo "  Public URLs:"
echo "    https://${DOMAIN}/webhook  → localhost:5000  (TradingView alerts)"
echo "    https://${DOMAIN}/health   → localhost:5001  (health check)"
echo ""
echo "  TradingView webhook URL:  https://${DOMAIN}/webhook"
echo ""
echo "  Status: https://${DOMAIN}/health → HTTP $HTTP_STATUS"
echo ""
echo "  Manage:"
echo "    cloudflared tunnel list"
echo "    cloudflared tunnel info $TUNNEL_NAME"
echo "    sudo systemctl status cloudflared"
