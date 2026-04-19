# 16-vps-deployment-guide

**Repo 16 of 20** in the [Algo Trading GitHub Series](../README.md)

Complete production deployment guide for the TradingView → Zerodha algo trading stack on a Linux VPS. Covers AWS and DigitalOcean setup, systemd services, nginx reverse proxy, TLS via Let's Encrypt, Cloudflare Tunnel as an ngrok alternative, automated daily Kite token refresh with Selenium + TOTP, health monitoring, and encrypted backups.

---

## Architecture on the VPS

```
Internet
    │
    ▼ HTTPS :443
┌─────────────────────────────────────────────────────────────────┐
│  nginx (reverse proxy + TLS termination)                        │
│                                                                 │
│  /webhook   → 127.0.0.1:5000  (repo 10 — webhook server)       │
│  /trade     → 127.0.0.1:5001  (repo 14 — orchestrator)         │
│  /health    → 127.0.0.1:5001  (repo 14 — orchestrator)         │
│  /signal    → 127.0.0.1:5002  (repo 19 — ML filter, optional)  │
└─────────────────────────────────────────────────────────────────┘
    │              │                    │
    ▼              ▼                    ▼
  gunicorn      gunicorn            gunicorn
  2w / 4t       1w / 4t             2w / 2t

  systemd service    systemd service   systemd service
  webhook.service    orchestrator.service  ml-filter.service

  token-refresh.timer  → token-refresh.service  (daily 08:50 IST)
  Selenium + TOTP → Kite login → access_token → .env + API update
```

---

## Quick Start

### Option A: AWS EC2

```bash
# 1. Launch instance
#    AMI:           Ubuntu Server 22.04 LTS
#    Instance type: t3.small (2 vCPU, 2GB RAM) — sufficient for all services
#    Storage:       20GB gp3 SSD
#    Security group: Allow inbound SSH (22), HTTP (80), HTTPS (443)

# 2. Connect
ssh -i your-key.pem ubuntu@YOUR_EC2_IP

# 3. Clone this repo + run setup
git clone https://github.com/yourusername/algo-trading-series.git
cd algo-trading-series/16-vps-deployment-guide
sudo bash scripts/setup_vps.sh --domain yourdomain.com --email you@email.com
```

### Option B: DigitalOcean Droplet

```bash
# 1. Create Droplet
#    Image:  Ubuntu 22.04 LTS
#    Size:   Basic / 2GB / $12/month
#    Region: Mumbai (BOM1) — lowest latency to NSE/Zerodha
#    Add your SSH key during creation

# 2. Connect
ssh root@YOUR_DROPLET_IP

# 3. Run setup
git clone https://github.com/yourusername/algo-trading-series.git
cd algo-trading-series/16-vps-deployment-guide
sudo bash scripts/setup_vps.sh --domain yourdomain.com --email you@email.com
```

### Option C: No domain / ngrok alternative (Cloudflare Tunnel)

If you don't have a domain or want to avoid managing DNS/certificates:

```bash
# Free Cloudflare account + any domain (even a free .workers.dev subdomain)
sudo bash scripts/setup_cloudflare_tunnel.sh \
    --domain algo.yourname.workers.dev \
    --tunnel-name algo-trading

# Result: https://algo.yourname.workers.dev/webhook → ready for TradingView
# No certbot, no port forwarding, works even behind NAT
```

---

## Files in This Repo

```
16-vps-deployment-guide/
├── scripts/
│   ├── setup_vps.sh              ← Full VPS setup (run once)
│   ├── configure_ssl.sh          ← Let's Encrypt + nginx HTTPS
│   ├── setup_cloudflare_tunnel.sh← Cloudflare Tunnel (ngrok alternative)
│   └── backup.sh                 ← Encrypted daily backups
│
├── systemd/
│   ├── webhook.service           ← repo 10 Flask server
│   ├── orchestrator.service      ← repo 14 orchestrator (1 worker!)
│   ├── ml-filter.service         ← repo 19 ML filter (optional)
│   ├── token-refresh.service     ← one-shot daily token refresh
│   └── token-refresh.timer       ← fires at 08:50 IST weekdays
│
├── nginx/
│   └── algo-trading.conf         ← nginx reverse proxy config
│
├── token_refresh/
│   └── refresh_token.py          ← Selenium + TOTP automated login
│
└── monitoring/
    └── health_check.py           ← service health + Telegram alerts
```

---

## Step-by-Step Deployment

### 1. Initial Setup

```bash
sudo bash scripts/setup_vps.sh
```

This installs Python, Chrome, nginx, certbot, ufw firewall, fail2ban, log rotation, and creates the `trader` system user.

### 2. Deploy your repos

```bash
# Copy your code to the VPS
scp -r ../10-webhook-flask-server trader@YOUR_VPS:/home/trader/algo-trading/
scp -r ../14-live-trading-orchestrator trader@YOUR_VPS:/home/trader/algo-trading/
scp -r ../19-ml-signal-filter trader@YOUR_VPS:/home/trader/algo-trading/   # optional

# Or use git
ssh trader@YOUR_VPS "cd /home/trader/algo-trading && git clone YOUR_REPO_URL 10-webhook-flask-server"
```

### 3. Configure credentials

```bash
ssh trader@YOUR_VPS
cd /home/trader/algo-trading

# Copy env templates
cp 10-webhook-flask-server/.env.example  10-webhook-flask-server/.env
cp 14-live-trading-orchestrator/.env.example 14-live-trading-orchestrator/.env

# Edit with your credentials
nano 10-webhook-flask-server/.env
nano 14-live-trading-orchestrator/.env
```

Critical values to set:
```bash
KITE_API_KEY=your_api_key
KITE_API_SECRET=your_api_secret
KITE_ACCESS_TOKEN=your_initial_token   # from manual login on day 1
WEBHOOK_SECRET=your_hmac_secret        # generate: python -c "import secrets; print(secrets.token_hex(32))"
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
DRY_RUN=true                           # keep true until you've paper traded
```

For automated token refresh (highly recommended):
```bash
KITE_USER_ID=your_zerodha_user_id
KITE_PASSWORD=your_zerodha_password    # encrypted by OS file permissions
KITE_TOTP_SECRET=your_totp_base32     # from Kite security settings
```

### 4. SSL (if using a domain)

```bash
# Point your domain's A record to the VPS IP first (DNS propagation: 5-15 min)
sudo bash scripts/configure_ssl.sh yourdomain.com you@email.com

# Verify
curl https://yourdomain.com/health
```

### 5. Install and start services

```bash
# Install systemd services
sudo cp systemd/*.service /etc/systemd/system/
sudo cp systemd/*.timer   /etc/systemd/system/
sudo systemctl daemon-reload

# Enable at boot
sudo systemctl enable orchestrator webhook token-refresh.timer

# Start in order (orchestrator first — webhook forwards to it)
sudo systemctl start orchestrator
sleep 3
sudo systemctl start webhook

# Enable and start ML filter (optional)
# sudo systemctl enable ml-filter && sudo systemctl start ml-filter

# Enable token refresh timer
sudo systemctl enable token-refresh.timer && sudo systemctl start token-refresh.timer
```

### 6. Verify everything is running

```bash
# Check service status
sudo systemctl status webhook orchestrator token-refresh.timer

# Watch live logs
sudo journalctl -u webhook -f
sudo journalctl -u orchestrator -f

# Test endpoints
curl http://localhost:5000/health
curl http://localhost:5001/health
curl https://yourdomain.com/health

# Run health check
python monitoring/health_check.py
```

### 7. Configure TradingView

In your Pine Script alert, set:
- **Webhook URL**: `https://yourdomain.com/webhook`
- **Message** (JSON):
```json
{
  "strategy_id": "ORB_NIFTY_15M",
  "action": "{{strategy.order.action}}",
  "symbol": "{{ticker}}",
  "price": {{close}},
  "quantity": 50
}
```
- **Header**: `X-Signature: {{your_hmac_signature}}`

---

## Daily Token Refresh

### How it works

Kite access tokens expire at midnight IST every day. The `token-refresh.timer` fires at **08:50 IST on weekdays**, 25 minutes before market open.

```
token-refresh.timer  (08:50 IST Mon-Fri)
      │
      ▼
token-refresh.service  (one-shot)
      │
      ▼
token_refresh/refresh_token.py
      │
      ├── Chrome headless → kite.zerodha.com/connect/login
      ├── Enter KITE_USER_ID + KITE_PASSWORD
      ├── Generate TOTP from KITE_TOTP_SECRET → enter 6-digit code
      ├── Extract request_token from redirect URL
      ├── POST to Kite API → get access_token
      ├── Write to .env
      └── POST /admin/token/update → update running orchestrator
                (no restart needed)
```

### TOTP Setup

1. Log in to [kite.zerodha.com](https://kite.zerodha.com) → Account → Security
2. Enable two-factor authentication → choose **TOTP**
3. When the QR code appears, also click **"Can't scan? Enter key manually"**
4. Copy the base32 secret key (e.g. `JBSWY3DPEHPK3PXP`)
5. Scan the QR in Google Authenticator as normal
6. Add the secret to `.env`: `KITE_TOTP_SECRET=JBSWY3DPEHPK3PXP`

### Test token refresh manually

```bash
cd /home/trader/algo-trading/14-live-trading-orchestrator
/home/trader/algo-trading/venv/bin/python \
    ../16-vps-deployment-guide/token_refresh/refresh_token.py
```

### Check timer status

```bash
systemctl list-timers token-refresh.timer
systemctl status token-refresh.service   # after it last ran
journalctl -u token-refresh -n 50
```

---

## Service Management Cheatsheet

```bash
# Status
sudo systemctl status webhook orchestrator ml-filter

# Logs (live)
sudo journalctl -u webhook     -f
sudo journalctl -u orchestrator -f
sudo journalctl -u ml-filter   -f

# Restart (triggers graceful shutdown for orchestrator)
sudo systemctl restart orchestrator
sudo systemctl restart webhook

# Stop all (graceful — orchestrator force-exits positions)
sudo systemctl stop webhook orchestrator

# Reload config (hot-reload strategies.yaml without restart)
curl -X POST https://yourdomain.com/config/reload

# Emergency halt (stop all trading immediately)
curl -X POST https://yourdomain.com/admin/halt

# View open positions
curl https://yourdomain.com/positions

# Force-exit all positions manually
curl -X POST https://yourdomain.com/admin/force-exit

# Enable/disable DRY_RUN (edit .env then restart)
sed -i 's/DRY_RUN=true/DRY_RUN=false/' .env
sudo systemctl restart orchestrator
```

---

## Health Monitoring

### One-time check
```bash
python monitoring/health_check.py
```

### Continuous watch (runs every 5 min)
```bash
python monitoring/health_check.py --watch
```

### Cron (recommended)
```bash
# Add to crontab:
*/5 * * * * /home/trader/algo-trading/venv/bin/python \
  /home/trader/algo-trading/16-vps-deployment-guide/monitoring/health_check.py \
  >> /var/log/algo-trading/health.log 2>&1
```

Checks performed: systemd service status, HTTP health endpoints, disk space (< 1GB triggers alert), memory usage (> 85%), CPU load (> 4.0), SSL certificate expiry (< 14 days).

---

## Backups

```bash
# Manual backup
bash scripts/backup.sh

# List backups
bash scripts/backup.sh --list

# Restore latest
bash scripts/backup.sh --restore

# Encrypted backups (set in .env)
BACKUP_PASSPHRASE=your_strong_passphrase bash scripts/backup.sh

# S3 upload (requires awscli configured)
S3_BUCKET=your-bucket-name bash scripts/backup.sh
```

Cron (daily at 4 AM):
```bash
0 4 * * * bash /home/trader/algo-trading/16-vps-deployment-guide/scripts/backup.sh >> /var/log/algo-trading/backup.log 2>&1
```

---

## Port Reference

| Port | Service | Accessible from |
|---|---|---|
| 5000 | repo 10 webhook server | localhost only (via nginx) |
| 5001 | repo 14 orchestrator | localhost only (via nginx) |
| 5002 | repo 19 ML filter | localhost only (via nginx) |
| 80   | nginx HTTP → 443 redirect | public |
| 443  | nginx HTTPS | public |
| 22   | SSH | your IP only (configure in security group) |

> All Flask/Gunicorn processes bind to `127.0.0.1` only. Only nginx is exposed publicly.

---

## Troubleshooting

**Service won't start:**
```bash
sudo journalctl -u SERVICE_NAME -n 50 --no-pager
sudo systemctl status SERVICE_NAME
```

**nginx config error:**
```bash
sudo nginx -t
sudo journalctl -u nginx -n 20
```

**Webhook 401 (HMAC failure):**
Check that `WEBHOOK_SECRET` in `.env` matches the secret you use to sign TradingView alerts.

**Token refresh fails:**
```bash
# Run manually with verbose output
PYTHONPATH=/home/trader/algo-trading \
  /home/trader/algo-trading/venv/bin/python \
  token_refresh/refresh_token.py
```
Common causes: wrong TOTP secret, Kite changed 2FA flow, Chrome/ChromeDriver version mismatch.

**Positions not closing at EOD:**
```bash
curl https://yourdomain.com/positions            # check what's open
curl https://yourdomain.com/admin/force-exit -X POST  # manual force close
sudo journalctl -u orchestrator -n 100 | grep "force_exit"
```

---

## Security Checklist

- [ ] SSH key authentication only (password auth disabled)
- [ ] `WEBHOOK_SECRET` set and used for HMAC validation
- [ ] `.env` files are `chmod 600` (readable only by owner)
- [ ] fail2ban enabled
- [ ] ufw firewall: only 22, 80, 443 open
- [ ] `DRY_RUN=true` until paper trading confirmed profitable
- [ ] Kite API app has IP whitelist set to VPS IP
- [ ] `KITE_PASSWORD` in `.env` — rotate regularly
- [ ] Backups encrypted and stored off-server

---

## Previous Repos (Required)

- **[10-webhook-flask-server](../10-webhook-flask-server)** — receives TradingView alerts
- **[14-live-trading-orchestrator](../14-live-trading-orchestrator)** — executes trades

## Next Repo

**[17-performance-analytics](../17-performance-analytics)** — Streamlit dashboard with equity curve, monthly P&L heatmap, per-strategy stats, and trade journal.
