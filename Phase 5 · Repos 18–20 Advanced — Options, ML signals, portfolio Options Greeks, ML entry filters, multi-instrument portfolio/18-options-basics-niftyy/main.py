"""
main.py
────────
NIFTY Options Bot — main entry point.

Orchestrates:
  1. Kite Connect auth check
  2. Strategy selection (straddle / strangle) from .env
  3. Entry at configured time
  4. Greeks monitor every 60s
  5. SL check every 30s
  6. Force-exit at 15:00 IST
  7. Expiry management

Run:
    python main.py

Recommended: Run inside a screen / tmux session or as a systemd service.
"""

import os
import sys
import time
import logging
import signal
import schedule
from datetime import datetime
import pytz
from dotenv import load_dotenv

load_dotenv()

# ── Logging setup ─────────────────────────────────────────────────────
import pathlib
pathlib.Path("logs").mkdir(exist_ok=True)

logging.basicConfig(
    level    = logging.INFO,
    format   = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt  = "%Y-%m-%d %H:%M:%S",
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")
IST    = pytz.timezone("Asia/Kolkata")

# ── Imports ──────────────────────────────────────────────────────────
from core.kite_client         import get_kite
from core.position_tracker    import PositionTracker
from core.order_executor      import OrderExecutor
from utils.greeks_monitor     import GreeksMonitor
from utils.telegram_alerts    import send_alert
from strategies.expiry_manager import ExpiryManager, schedule_expiry_jobs

STRATEGY        = os.getenv("STRATEGY",       "straddle").lower()
ENTRY_TIME      = os.getenv("ENTRY_TIME",     "09:30")
EXIT_TIME       = os.getenv("EXIT_TIME",      "15:00")
UNDERLYING      = os.getenv("UNDERLYING",     "NIFTY 50")

# ── Graceful shutdown ────────────────────────────────────────────────
_shutdown = False

def _handle_signal(sig, frame):
    global _shutdown
    logger.info(f"Signal {sig} received — initiating graceful shutdown…")
    _shutdown = True

signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ──────────────────────────────────────────────────────────────────────
# Strategy factory
# ──────────────────────────────────────────────────────────────────────

def get_strategy():
    if STRATEGY == "straddle":
        from strategies.straddle import StraddleStrategy
        return StraddleStrategy()
    elif STRATEGY == "strangle":
        from strategies.strangle import StrangleStrategy
        return StrangleStrategy()
    else:
        raise ValueError(f"Unknown STRATEGY='{STRATEGY}'. Use 'straddle' or 'strangle'.")


# ──────────────────────────────────────────────────────────────────────
# Scheduled jobs
# ──────────────────────────────────────────────────────────────────────

def job_entry():
    """Run strategy entry at configured time."""
    logger.info(f"=== Entry job triggered ({STRATEGY}) ===")
    strategy.enter()


def job_check_sl():
    """Check stop-losses every 30 seconds during market hours."""
    exec_ = OrderExecutor()
    if not exec_.is_market_open():
        return
    strategy.check_stop_losses()


def job_force_exit():
    """Force-exit all positions at EXIT_TIME."""
    logger.info("=== Force-exit job triggered ===")
    tracker = PositionTracker()
    for pos in tracker.open_positions:
        logger.info(f"Force-closing position: {pos.strategy} {pos.expiry}")
        strategy.exit_position(pos, reason="FORCE_EXIT_EOD")


def job_greeks_update():
    """Update Greeks every 60s."""
    exec_ = OrderExecutor()
    if not exec_.is_market_open():
        return
    greeks_monitor.update()


def job_heartbeat():
    """Send daily heartbeat + position summary at 10:00 AM."""
    tracker = PositionTracker()
    send_alert(f"💚 Bot alive — {datetime.now(IST).strftime('%Y-%m-%d %H:%M')}\n\n"
               + tracker.summary())


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────

def main():
    global strategy, greeks_monitor

    logger.info("=" * 60)
    logger.info(f"  NIFTY Options Bot  |  Strategy: {STRATEGY.upper()}")
    logger.info(f"  Underlying: {UNDERLYING}")
    logger.info(f"  Entry: {ENTRY_TIME}  |  Exit: {EXIT_TIME}")
    logger.info("=" * 60)

    # Verify Kite connection
    try:
        kite  = get_kite()
        profile = kite.profile()
        logger.info(f"Kite connected: {profile['user_name']} ({profile['user_id']})")
    except Exception as e:
        logger.critical(f"Kite connection failed: {e}")
        logger.critical("Run: python utils/generate_token.py  to refresh your token.")
        sys.exit(1)

    # Init components
    strategy        = get_strategy()
    greeks_monitor  = GreeksMonitor()
    expiry_manager  = ExpiryManager()
    tracker         = PositionTracker()

    # Register scheduled jobs
    schedule.every().day.at(ENTRY_TIME).do(job_entry)
    schedule.every(30).seconds.do(job_check_sl)
    schedule.every(60).seconds.do(job_greeks_update)
    schedule.every().day.at(EXIT_TIME).do(job_force_exit)
    schedule.every().day.at("10:00").do(job_heartbeat)
    schedule_expiry_jobs(expiry_manager)

    logger.info("All jobs scheduled. Bot is running.")
    send_alert(
        f"🚀 *Options Bot Started*\n"
        f"Strategy: {STRATEGY.upper()}\n"
        f"Underlying: {UNDERLYING}\n"
        f"Entry: {ENTRY_TIME} | Exit: {EXIT_TIME}"
    )

    # Main loop
    while not _shutdown:
        schedule.run_pending()
        time.sleep(1)

    # Cleanup on shutdown
    logger.info("Shutting down…")
    open_pos = tracker.open_positions
    if open_pos:
        logger.warning(f"{len(open_pos)} open position(s) on shutdown — NOT auto-closing.")
        logger.warning("Run manually: python -c \"from main import *; job_force_exit()\"")
        send_alert(
            f"⚠️ Bot shutdown with {len(open_pos)} open positions!\n"
            f"Please check and close manually.\n\n"
            + tracker.summary()
        )
    else:
        send_alert("🔴 Bot shutdown. No open positions.")

    logger.info("Bot stopped.")


if __name__ == "__main__":
    main()
