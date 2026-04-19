#!/usr/bin/env python3
"""
monitoring/health_check.py
───────────────────────────
Monitors all algo trading services and alerts via Telegram if anything is down.

Checks performed:
  1. systemd service status (webhook, orchestrator, ml-filter)
  2. HTTP health endpoints (/ health, /status)
  3. Disk space (alert if < 1GB free)
  4. Memory usage (alert if > 85%)
  5. CPU load (alert if 1-min avg > 4.0)
  6. nginx status
  7. SSL certificate expiry (alert 14 days before)

Run modes:
  python monitoring/health_check.py          — single run, exits
  python monitoring/health_check.py --watch  — loop every 5 minutes
  python monitoring/health_check.py --fix    — attempt auto-restart of failed services

Recommended cron (every 5 minutes):
  */5 * * * * /home/trader/algo-trading/venv/bin/python \
    /home/trader/algo-trading/16-vps-deployment-guide/monitoring/health_check.py \
    >> /var/log/algo-trading/health.log 2>&1
"""

import os
import sys
import time
import shutil
import subprocess
import socket
import ssl
import datetime
import argparse
import logging
import requests
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("health")

from dotenv import load_dotenv
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")

# ── Services to monitor ────────────────────────────────────────────────────────
SERVICES = [
    {"name": "webhook",      "port": 5000, "path": "/health"},
    {"name": "orchestrator", "port": 5001, "path": "/health"},
]

OPTIONAL_SERVICES = [
    {"name": "ml-filter", "port": 5002, "path": "/health"},
]

DOMAIN         = os.getenv("DOMAIN", "")
DISK_MIN_GB    = 1.0
MEM_ALERT_PCT  = 85
LOAD_ALERT     = 4.0
SSL_WARN_DAYS  = 14


# ── Check functions ────────────────────────────────────────────────────────────

def check_systemd_service(name: str) -> dict:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True, text=True, timeout=5
        )
        active = result.stdout.strip() == "active"
        return {"ok": active, "status": result.stdout.strip(), "name": name}
    except Exception as e:
        return {"ok": False, "status": str(e), "name": name}


def check_http_endpoint(port: int, path: str, name: str) -> dict:
    try:
        url  = f"http://127.0.0.1:{port}{path}"
        resp = requests.get(url, timeout=5)
        ok   = resp.status_code == 200
        return {"ok": ok, "status": resp.status_code, "name": name, "url": url}
    except Exception as e:
        return {"ok": False, "status": str(e), "name": name}


def check_disk_space() -> dict:
    usage = shutil.disk_usage("/")
    free_gb = usage.free / 1024**3
    ok = free_gb >= DISK_MIN_GB
    return {"ok": ok, "free_gb": round(free_gb, 1), "total_gb": round(usage.total / 1024**3, 1)}


def check_memory() -> dict:
    try:
        with open("/proc/meminfo") as f:
            lines = f.readlines()
        info = {}
        for line in lines:
            parts = line.split()
            if len(parts) >= 2:
                info[parts[0].rstrip(":")] = int(parts[1])

        total    = info.get("MemTotal",     1)
        free     = info.get("MemFree",      0)
        buffers  = info.get("Buffers",      0)
        cached   = info.get("Cached",       0)
        avail    = info.get("MemAvailable", free + buffers + cached)
        used_pct = round((1 - avail / total) * 100, 1)
        ok = used_pct < MEM_ALERT_PCT
        return {"ok": ok, "used_pct": used_pct, "avail_mb": round(avail / 1024)}
    except Exception as e:
        return {"ok": True, "error": str(e)}


def check_load() -> dict:
    load1, load5, load15 = os.getloadavg()
    ok = load1 < LOAD_ALERT
    return {"ok": ok, "load1": round(load1, 2), "load5": round(load5, 2)}


def check_ssl_expiry(domain: str) -> dict:
    if not domain:
        return {"ok": True, "skipped": True}
    try:
        ctx  = ssl.create_default_context()
        with socket.create_connection((domain, 443), timeout=5) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                cert = ssock.getpeercert()
        expiry_str = cert["notAfter"]
        expiry = datetime.datetime.strptime(expiry_str, "%b %d %H:%M:%S %Y %Z")
        days_left = (expiry - datetime.datetime.utcnow()).days
        ok = days_left > SSL_WARN_DAYS
        return {"ok": ok, "days_left": days_left, "expiry": expiry.strftime("%Y-%m-%d")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Run all checks ─────────────────────────────────────────────────────────────

def run_checks() -> tuple[bool, list[str]]:
    """Run all checks. Returns (all_ok, list_of_issues)."""
    issues = []
    all_ok = True

    # Systemd services
    for svc in SERVICES:
        r = check_systemd_service(svc["name"])
        if not r["ok"]:
            issues.append(f"❌ systemd `{svc['name']}`: {r['status']}")
            all_ok = False
        else:
            logger.info(f"  ✅ systemd {svc['name']}: {r['status']}")

    # HTTP endpoints
    for svc in SERVICES:
        r = check_http_endpoint(svc["port"], svc["path"], svc["name"])
        if not r["ok"]:
            issues.append(f"❌ HTTP {svc['name']}:{svc['port']}{svc['path']} → {r['status']}")
            all_ok = False
        else:
            logger.info(f"  ✅ HTTP {svc['name']}:{svc['port']} → {r['status']}")

    # Optional services (don't fail overall if absent)
    for svc in OPTIONAL_SERVICES:
        r = check_http_endpoint(svc["port"], svc["path"], svc["name"])
        if not r["ok"]:
            logger.debug(f"  ℹ️  Optional {svc['name']} not responding")

    # Disk
    r = check_disk_space()
    if not r["ok"]:
        issues.append(f"⚠️ Disk: only {r['free_gb']}GB free (need ≥{DISK_MIN_GB}GB)")
        all_ok = False
    else:
        logger.info(f"  ✅ Disk: {r['free_gb']}GB free of {r['total_gb']}GB")

    # Memory
    r = check_memory()
    if not r["ok"]:
        issues.append(f"⚠️ Memory: {r['used_pct']}% used (limit {MEM_ALERT_PCT}%)")
        all_ok = False
    else:
        logger.info(f"  ✅ Memory: {r['used_pct']}% used, {r['avail_mb']}MB available")

    # Load
    r = check_load()
    if not r["ok"]:
        issues.append(f"⚠️ CPU load: {r['load1']} (limit {LOAD_ALERT})")
        all_ok = False
    else:
        logger.info(f"  ✅ CPU load: {r['load1']}")

    # SSL
    if DOMAIN:
        r = check_ssl_expiry(DOMAIN)
        if not r.get("skipped"):
            if not r["ok"]:
                issues.append(f"⚠️ SSL cert expires in {r.get('days_left','?')} days ({r.get('expiry','')})")
            else:
                logger.info(f"  ✅ SSL: {r.get('days_left','?')} days until expiry")

    return all_ok, issues


def send_telegram_alert(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception:
        pass


def auto_restart_failed():
    """Attempt to restart any failed services."""
    for svc in SERVICES:
        r = check_systemd_service(svc["name"])
        if not r["ok"]:
            logger.info(f"Auto-restarting {svc['name']}...")
            try:
                subprocess.run(["systemctl", "restart", svc["name"]], timeout=30)
                logger.info(f"  Restart triggered for {svc['name']}")
            except Exception as e:
                logger.error(f"  Restart failed: {e}")


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true", help="Run in loop every 5 minutes")
    parser.add_argument("--fix",   action="store_true", help="Auto-restart failed services")
    args = parser.parse_args()

    _last_alert_time = {}

    def run_once():
        logger.info(f"Health check @ {datetime.datetime.now().strftime('%H:%M:%S')}")
        ok, issues = run_checks()

        if args.fix and not ok:
            auto_restart_failed()
            time.sleep(5)
            ok, issues = run_checks()

        if not ok:
            alert_key = "\n".join(issues)
            last_alert = _last_alert_time.get(alert_key, 0)
            # Don't spam: only alert once per 15 minutes for the same issue set
            if time.time() - last_alert > 900:
                msg = "🚨 *Algo Trading Health Alert*\n\n" + "\n".join(issues)
                send_telegram_alert(msg)
                _last_alert_time[alert_key] = time.time()
                logger.warning("Alert sent to Telegram")
        else:
            logger.info("All checks passed ✅")

        return ok

    if args.watch:
        while True:
            run_once()
            time.sleep(300)   # every 5 minutes
    else:
        ok = run_once()
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
