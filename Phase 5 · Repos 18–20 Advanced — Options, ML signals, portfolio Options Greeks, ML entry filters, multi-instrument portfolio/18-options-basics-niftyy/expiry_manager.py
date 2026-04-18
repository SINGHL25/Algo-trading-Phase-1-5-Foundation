"""
strategies/expiry_manager.py
─────────────────────────────
Handles expiry-day position management for NIFTY options.

Rules applied on expiry day:
  1. Do NOT open new positions (AVOID_EXPIRY_ENTRY=true)
  2. Auto-close all open positions by FORCE_EXIT_TIME (default 14:30 on expiry)
  3. If option expires worthless (LTP ≤ 1.0), let it expire — save brokerage
  4. Alert via Telegram at:
       - Start of expiry day
       - 30 min before force-exit
       - On each forced close
       - Daily P&L summary at EOD

Expiry detection:
  - Checks if any open position has expiry == today
  - Also checks if next expiry is today (for newly loaded positions)

NIFTY expiry schedule (as of 2024):
  - Weekly: every Thursday
  - Monthly: last Thursday
  BankNIFTY: Wednesday
  FinNIFTY: Tuesday
"""

import os
import logging
from datetime import datetime, date, time as dtime, timedelta
from typing import Optional
import pytz
import schedule
import time

from core.position_tracker import PositionTracker, OptionsPosition
from core.order_executor   import OrderExecutor
from utils.telegram_alerts import send_alert

logger = logging.getLogger(__name__)
IST    = pytz.timezone("Asia/Kolkata")

FORCE_EXIT_ON_EXPIRY   = dtime(14, 30)   # earlier cutoff on expiry day
WORTHLESS_THRESHOLD    = 1.0             # INR — if LTP ≤ this, let expire
EXPIRY_WARNING_MINUTES = 30


class ExpiryManager:
    def __init__(self):
        self.tracker  = PositionTracker()
        self.executor = OrderExecutor()

    # ──────────────────────────────────────────────────────────────────
    # Main daily check (run at 9:15 AM)
    # ──────────────────────────────────────────────────────────────────

    def morning_check(self):
        """
        Run at market open. Alerts if any position expires today.
        """
        today           = date.today()
        expiring_today  = self._get_expiring_positions(today)

        if not expiring_today:
            logger.info("Expiry check: no positions expire today.")
            return

        symbols = [p.expiry for p in expiring_today]
        msg = (
            f"⚠️ *Expiry Day Alert*\n"
            f"{len(expiring_today)} position(s) expire TODAY ({today})\n"
            f"Force-exit scheduled at {FORCE_EXIT_ON_EXPIRY.strftime('%H:%M')} IST\n\n"
            + self.tracker.summary()
        )
        send_alert(msg)
        logger.info(f"Expiry day: {len(expiring_today)} positions will be managed.")

    # ──────────────────────────────────────────────────────────────────
    # Force-exit on expiry
    # ──────────────────────────────────────────────────────────────────

    def force_exit_expiring(self, strategy_instance=None):
        """
        Called at FORCE_EXIT_ON_EXPIRY time on expiry day.
        Closes all positions expiring today, unless leg is worthless.
        """
        today          = date.today()
        expiring_today = self._get_expiring_positions(today)

        if not expiring_today:
            return

        logger.info(f"Force-exiting {len(expiring_today)} expiring positions…")

        for position in expiring_today:
            legs_to_exit = []
            legs_to_expire = []

            for leg in position.legs:
                if leg.status != "OPEN":
                    continue
                if leg.current_ltp <= WORTHLESS_THRESHOLD:
                    legs_to_expire.append(leg)
                    logger.info(
                        f"  Letting expire: {leg.tradingsymbol} "
                        f"LTP={leg.current_ltp:.2f} (≤ ₹{WORTHLESS_THRESHOLD})"
                    )
                else:
                    legs_to_exit.append(leg)

            # Close non-worthless legs
            for leg in legs_to_exit:
                try:
                    result  = self.executor.buy_to_close(
                        leg.tradingsymbol, leg.quantity
                    )
                    fill_px = self.executor.wait_for_fill(result["order_id"])
                    self.tracker.mark_leg_closed(
                        leg.tradingsymbol, fill_px,
                        result["order_id"], reason="EXPIRY_FORCE_EXIT"
                    )
                    logger.info(
                        f"  Force-closed: {leg.tradingsymbol} @ {fill_px:.2f}"
                    )
                except Exception as e:
                    logger.error(f"  Force-exit failed for {leg.tradingsymbol}: {e}")
                    send_alert(
                        f"❌ Force-exit FAILED on expiry!\n"
                        f"{leg.tradingsymbol}: {e}\n"
                        f"⚠️ MANUAL ACTION REQUIRED"
                    )

            # Mark worthless legs as closed (they'll expire at 0)
            for leg in legs_to_expire:
                self.tracker.mark_leg_closed(
                    leg.tradingsymbol, 0.0, "EXPIRED_WORTHLESS", reason="EXPIRED"
                )

            # Close the position record
            self.tracker.mark_position_closed(position, reason="EXPIRY")

        # Send EOD summary
        self._send_eod_summary(today)

    # ──────────────────────────────────────────────────────────────────
    # Expiry warning (30 min before force-exit)
    # ──────────────────────────────────────────────────────────────────

    def send_expiry_warning(self):
        today          = date.today()
        expiring_today = self._get_expiring_positions(today)
        if not expiring_today:
            return

        send_alert(
            f"⏰ *Expiry Warning — 30 min to force-exit*\n"
            f"Force-exit at {FORCE_EXIT_ON_EXPIRY.strftime('%H:%M')} IST\n\n"
            + self.tracker.summary()
        )

    # ──────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────

    def _get_expiring_positions(self, on_date: date) -> list[OptionsPosition]:
        return [
            p for p in self.tracker.open_positions
            if p.expiry == str(on_date)
        ]

    def _send_eod_summary(self, trade_date: date):
        """Send end-of-day P&L summary."""
        today_pnl = self.tracker.today_pnl
        positions_today = [
            p for p in self.tracker.positions
            if p.entry_time[:10] == str(trade_date)
        ]
        lines = [
            f"📅 *EOD Summary — {trade_date}*",
            f"Positions: {len(positions_today)}",
            f"Net P&L: ₹{today_pnl:.2f}",
            "",
        ]
        for p in positions_today:
            lines.append(
                f"  {p.strategy.upper()} {p.expiry} | "
                f"Entry premium: ₹{p.total_premium_received:.2f} | "
                f"P&L: ₹{p.net_pnl:.2f}"
            )
        send_alert("\n".join(lines))


# ──────────────────────────────────────────────────────────────────────
# Scheduling helper
# ──────────────────────────────────────────────────────────────────────

def schedule_expiry_jobs(manager: ExpiryManager):
    """Register scheduled jobs for expiry management."""
    schedule.every().day.at("09:20").do(manager.morning_check)

    warning_time = (
        datetime.combine(date.today(), FORCE_EXIT_ON_EXPIRY)
        - timedelta(minutes=EXPIRY_WARNING_MINUTES)
    ).time().strftime("%H:%M")
    schedule.every().day.at(warning_time).do(manager.send_expiry_warning)

    force_exit_str = FORCE_EXIT_ON_EXPIRY.strftime("%H:%M")
    schedule.every().day.at(force_exit_str).do(manager.force_exit_expiring)

    logger.info(
        f"Expiry jobs scheduled: "
        f"morning_check=09:20, "
        f"warning={warning_time}, "
        f"force_exit={force_exit_str}"
    )
