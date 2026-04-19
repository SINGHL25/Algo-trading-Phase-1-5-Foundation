#!/usr/bin/env bash
# =============================================================================
# scripts/configure_ssl.sh
# Obtains a Let's Encrypt TLS certificate and configures nginx for HTTPS.
#
# Usage:
#   sudo bash scripts/configure_ssl.sh yourdomain.com you@email.com
#
# Requirements:
#   - Domain DNS A record pointing to this VPS IP (propagation can take 5-15 min)
#   - Port 80 and 443 open in firewall
#   - nginx installed and running
#
# After success:
#   - https://yourdomain.com/webhook  — TradingView webhook endpoint
#   - https://yourdomain.com/health   — health check
#   - Certificate auto-renews via certbot cron
# =============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

DOMAIN="${1:-}"
EMAIL="${2:-}"

[[ -z "$DOMAIN" ]] && error "Usage: sudo bash $0 yourdomain.com you@email.com"
[[ -z "$EMAIL"  ]] && error "Usage: sudo bash $0 yourdomain.com you@email.com"
[[ $EUID -ne 0  ]] && error "Run as root: sudo bash $0"

info "Configuring SSL for: $DOMAIN"

# ── Verify DNS resolves to this machine ───────────────────────────────────────
OWN_IP=$(curl -s https://api.ipify.org 2>/dev/null || curl -s https://ifconfig.me 2>/dev/null || echo "")
DNS_IP=$(dig +short "$DOMAIN" A 2>/dev/null | tail -1 || nslookup "$DOMAIN" 2>/dev/null | grep 'Address:' | tail -1 | awk '{print $2}' || echo "")

if [[ -n "$OWN_IP" && -n "$DNS_IP" && "$OWN_IP" != "$DNS_IP" ]]; then
    warn "DNS mismatch: $DOMAIN resolves to $DNS_IP but this server is $OWN_IP"
    warn "Certbot may fail if DNS hasn't propagated. Continue anyway? (y/N)"
    read -r reply
    [[ "$reply" != "y" && "$reply" != "Y" ]] && error "Aborted."
fi

# ── Write temporary nginx config for ACME challenge ───────────────────────────
info "Writing temporary nginx config for ACME challenge..."
cat > /etc/nginx/sites-available/algo-trading-temp << NGINX_TEMP
server {
    listen 80;
    server_name ${DOMAIN};
    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }
    location / {
        return 301 https://\$host\$request_uri;
    }
}
NGINX_TEMP

ln -sf /etc/nginx/sites-available/algo-trading-temp /etc/nginx/sites-enabled/algo-trading-temp
nginx -t && systemctl reload nginx
success "Temporary nginx config active"

# ── Obtain certificate ────────────────────────────────────────────────────────
info "Obtaining Let's Encrypt certificate for $DOMAIN..."
certbot certonly \
    --webroot \
    --webroot-path /var/www/html \
    --email "$EMAIL" \
    --agree-tos \
    --no-eff-email \
    --domain "$DOMAIN" \
    --non-interactive

success "Certificate obtained: /etc/letsencrypt/live/$DOMAIN/"

# ── Write production nginx config with SSL ────────────────────────────────────
info "Writing production nginx config with SSL..."
cat > /etc/nginx/sites-available/algo-trading << NGINX_SSL
# ── Rate limiting zones ──────────────────────────────────────────────────────
limit_req_zone \$binary_remote_addr zone=webhook:10m rate=30r/m;
limit_req_zone \$binary_remote_addr zone=admin:10m    rate=60r/m;
limit_conn_zone \$binary_remote_addr zone=perip:10m;

# ── HTTP → HTTPS redirect ─────────────────────────────────────────────────────
server {
    listen 80;
    server_name ${DOMAIN};
    return 301 https://\$host\$request_uri;
}

# ── Main HTTPS server ─────────────────────────────────────────────────────────
server {
    listen 443 ssl http2;
    server_name ${DOMAIN};

    # TLS certificates (Let's Encrypt)
    ssl_certificate     /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;

    # Modern TLS settings (A+ on SSL Labs)
    ssl_protocols             TLSv1.2 TLSv1.3;
    ssl_ciphers               ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:DHE-RSA-AES128-GCM-SHA256;
    ssl_prefer_server_ciphers off;
    ssl_session_cache         shared:SSL:10m;
    ssl_session_timeout       1d;
    ssl_session_tickets       off;
    ssl_stapling              on;
    ssl_stapling_verify       on;
    add_header Strict-Transport-Security "max-age=63072000" always;

    # Connection limit per IP
    limit_conn perip 20;

    # Security headers
    add_header X-Frame-Options           DENY;
    add_header X-Content-Type-Options    nosniff;
    add_header X-XSS-Protection          "1; mode=block";
    add_header Referrer-Policy           "no-referrer";
    add_header Content-Security-Policy   "default-src 'none'";

    # Max request body (webhook payloads are small)
    client_max_body_size 1m;

    # Logs
    access_log /var/log/nginx/algo-trading-access.log;
    error_log  /var/log/nginx/algo-trading-error.log;

    # ── /webhook  →  repo 10 Flask server (port 5000) ─────────────────────────
    location /webhook {
        limit_req zone=webhook burst=10 nodelay;

        proxy_pass         http://127.0.0.1:5000;
        proxy_http_version 1.1;
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;

        proxy_read_timeout    30s;
        proxy_connect_timeout 10s;
        proxy_send_timeout    30s;
    }

    # ── /trade, /health, /status  →  repo 14 orchestrator (port 5001) ─────────
    location ~ ^/(trade|health|status|positions|config|admin) {
        limit_req zone=admin burst=20 nodelay;

        proxy_pass         http://127.0.0.1:5001;
        proxy_http_version 1.1;
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;

        proxy_read_timeout    30s;
        proxy_connect_timeout 10s;
    }

    # ── /signal  →  repo 19 ML filter (port 5002) ─────────────────────────────
    location /signal {
        limit_req zone=webhook burst=10 nodelay;

        proxy_pass         http://127.0.0.1:5002;
        proxy_http_version 1.1;
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;

        proxy_read_timeout    60s;
    }

    # ── Block everything else ──────────────────────────────────────────────────
    location / {
        return 404;
    }
}
NGINX_SSL

# Remove temp config
rm -f /etc/nginx/sites-enabled/algo-trading-temp

ln -sf /etc/nginx/sites-available/algo-trading /etc/nginx/sites-enabled/algo-trading
nginx -t && systemctl reload nginx
success "Production nginx config with SSL active"

# ── Auto-renewal cron ─────────────────────────────────────────────────────────
info "Setting up certbot auto-renewal..."
if ! crontab -l 2>/dev/null | grep -q certbot; then
    (crontab -l 2>/dev/null; echo "0 3 * * * certbot renew --quiet --post-hook 'systemctl reload nginx'") | crontab -
fi
success "Auto-renewal cron: daily at 3:00 AM"

# ── Final test ────────────────────────────────────────────────────────────────
echo ""
sleep 2
HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" "https://${DOMAIN}/health" 2>/dev/null || echo "000")
if [[ "$HTTP_STATUS" == "200" ]]; then
    success "HTTPS health check passed: https://${DOMAIN}/health → 200"
else
    warn "Health check returned $HTTP_STATUS — services may not be running yet"
fi

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  SSL configured!                                            ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  Webhook endpoint:  https://${DOMAIN}/webhook"
echo "  Health check:      https://${DOMAIN}/health"
echo "  Status:            https://${DOMAIN}/status"
echo ""
echo "  TradingView alert URL:  https://${DOMAIN}/webhook"
