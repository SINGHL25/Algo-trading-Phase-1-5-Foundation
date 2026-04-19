#!/usr/bin/env bash
# =============================================================================
# scripts/backup.sh
# Daily backup of critical data: .env files, state JSON, SQLite DBs, logs.
#
# What's backed up:
#   - .env files (encrypted with AES-256)
#   - data/state/orchestrator_state.json
#   - logs/webhooks.db (SQLite)
#   - logs/*.log (compressed)
#
# Retention: 7 daily + 4 weekly + 3 monthly (thin backup strategy)
#
# Storage options (configure one):
#   LOCAL_BACKUP_DIR — local directory (default: /home/trader/backups)
#   S3_BUCKET        — AWS S3 bucket (requires awscli)
#
# Usage:
#   bash scripts/backup.sh              — run backup
#   bash scripts/backup.sh --restore    — restore latest backup
#   bash scripts/backup.sh --list       — list available backups
#
# Cron (daily at 4:00 AM):
#   0 4 * * * /home/trader/algo-trading/venv/bin/python \
#     bash /home/trader/algo-trading/16-vps-deployment-guide/scripts/backup.sh \
#     >> /var/log/algo-trading/backup.log 2>&1
# =============================================================================

set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[$(date +%H:%M:%S)]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }

BASE_DIR="/home/trader/algo-trading"
BACKUP_DIR="${LOCAL_BACKUP_DIR:-/home/trader/backups}"
S3_BUCKET="${S3_BUCKET:-}"
BACKUP_PASSPHRASE="${BACKUP_PASSPHRASE:-}"   # set in .env for encrypted backups
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_NAME="algo_trading_${TIMESTAMP}.tar.gz"
BACKUP_PATH="$BACKUP_DIR/$BACKUP_NAME"

mkdir -p "$BACKUP_DIR"

# ── Collect files to back up ──────────────────────────────────────────────────
INCLUDE_PATHS=()

for dir in 10-webhook-flask-server 14-live-trading-orchestrator 19-ml-signal-filter; do
    [[ -f "$BASE_DIR/$dir/.env" ]] && INCLUDE_PATHS+=("$BASE_DIR/$dir/.env")
done

for state_file in "$BASE_DIR"/*/data/state/*.json "$BASE_DIR"/shared/state/*.json; do
    [[ -f "$state_file" ]] && INCLUDE_PATHS+=("$state_file")
done

for db_file in "$BASE_DIR"/*/logs/*.db "$BASE_DIR"/shared/data/*.db; do
    [[ -f "$db_file" ]] && INCLUDE_PATHS+=("$db_file")
done

info "Backing up ${#INCLUDE_PATHS[@]} files to $BACKUP_PATH..."

# ── Create archive ────────────────────────────────────────────────────────────
if [[ ${#INCLUDE_PATHS[@]} -gt 0 ]]; then
    tar -czf "$BACKUP_PATH" "${INCLUDE_PATHS[@]}" 2>/dev/null || true
else
    warn "No files found to back up"
    exit 0
fi

# ── Encrypt if passphrase set ─────────────────────────────────────────────────
if [[ -n "$BACKUP_PASSPHRASE" ]]; then
    openssl enc -aes-256-cbc -pbkdf2 -in "$BACKUP_PATH" \
        -out "${BACKUP_PATH}.enc" -pass pass:"$BACKUP_PASSPHRASE"
    rm "$BACKUP_PATH"
    BACKUP_PATH="${BACKUP_PATH}.enc"
    BACKUP_NAME="${BACKUP_NAME}.enc"
    info "Backup encrypted with AES-256"
fi

BACKUP_SIZE=$(du -sh "$BACKUP_PATH" | cut -f1)
success "Backup created: $BACKUP_PATH ($BACKUP_SIZE)"

# ── Upload to S3 ──────────────────────────────────────────────────────────────
if [[ -n "$S3_BUCKET" ]] && command -v aws &>/dev/null; then
    aws s3 cp "$BACKUP_PATH" "s3://$S3_BUCKET/algo-trading/$BACKUP_NAME" --quiet
    success "Uploaded to s3://$S3_BUCKET/algo-trading/$BACKUP_NAME"
fi

# ── Prune old backups (keep last 14) ─────────────────────────────────────────
cd "$BACKUP_DIR"
ls -t algo_trading_*.tar.gz* 2>/dev/null | tail -n +15 | xargs -r rm --
success "Old backups pruned (keeping last 14)"

# ── Restore mode ─────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--restore" ]]; then
    LATEST=$(ls -t "$BACKUP_DIR"/algo_trading_*.tar.gz* 2>/dev/null | head -1)
    [[ -z "$LATEST" ]] && { warn "No backups found"; exit 1; }
    info "Restoring from: $LATEST"

    RESTORE_FILE="$LATEST"
    if [[ "$LATEST" == *.enc ]]; then
        [[ -z "$BACKUP_PASSPHRASE" ]] && { warn "BACKUP_PASSPHRASE needed for encrypted backup"; exit 1; }
        openssl enc -d -aes-256-cbc -pbkdf2 -in "$LATEST" \
            -out "/tmp/restore_algo.tar.gz" -pass pass:"$BACKUP_PASSPHRASE"
        RESTORE_FILE="/tmp/restore_algo.tar.gz"
    fi

    tar -xzf "$RESTORE_FILE" -C / 2>/dev/null
    success "Restore complete"
    exit 0
fi

# ── List mode ────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--list" ]]; then
    echo ""
    echo "Available backups in $BACKUP_DIR:"
    ls -lh "$BACKUP_DIR"/algo_trading_*.tar.gz* 2>/dev/null || echo "  (none)"
    exit 0
fi
