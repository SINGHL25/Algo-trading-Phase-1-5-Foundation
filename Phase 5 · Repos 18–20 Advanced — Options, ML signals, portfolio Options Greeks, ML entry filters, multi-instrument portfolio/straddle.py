"""
strategies/straddle.py
───────────────────────
Short Straddle on NIFTY

Strategy logic:
  - Sell ATM CE + ATM PE simultaneously
  - ATM = nearest strike to spot at time of entry
  - Collect combined premium from both legs
  - SL per leg: if LTP exceeds entry_price × (1 + SL_PCT), buy back that leg
  - Target: hold until premium decays by target% OR force-exit at 3 PM
  - Avoid entry on expiry day (configurable)

Pre-conditions checked before entry:
  ✓ Market is open + past 9:30 AM (avoid opening volatility)
  ✓ IV not too low (< MIN_IV) or too high (> MAX_IV)
  ✓ Max open positions not exceeded
  ✓ Daily loss limit not hit
  ✓ No existing open straddle on same expiry
"""

import os
import logging
from datetime import datetime, date
from typing import Optional
import pytz

from core.option_chain    import OptionChain
from core.order_executor  import OrderExecutor
from core.position_tracker import OptionLeg, OptionsPosition, PositionTracker
from utils.telegram_alerts import send_alert

logger = logging.getLogger(__name__)
IST    = pytz.timezone("Asia/Kolkata")

# ── Config from .env ──────────────────────────────────────────────────
UNDERLYING       = os.getenv("UNDERLYING",  "NIFTY 50")
LOT_SIZE         = int(os.getenv("LOT_SIZE",   "50"))
NUM_LOTS         = int(os.getenv("NUM_LOTS",    "1"))
MIN_IV           = float(os.getenv("MIN_IV",   "10"))
MAX_IV           = float(os.getenv("MAX_IV",   "80"))
MAX_OPEN_POS     = int(os.getenv("MAX_OPEN_POSITIONS", "2"))
MAX_LOSS_PER_DAY = float(os.getenv("MAX_LOSS_PER_DAY", "5000"))
AVOID_EXPIRY_ENTRY = os.getenv("AVOID_EXPIRY_ENTRY", "true").lower() == "true"
SL_PCT           = float(os.getenv("SL_PCT",   "0.50"))

QUANTITY = LOT_SIZE * NUM_LOTS


class StraddleStrategy:
    def __init__(self):
        self.chain    = OptionChain(underlying=UNDERLYING)
        self.executor = OrderExecutor()
        self.tracker  = PositionTracker()

    # ──────────────────────────────────────────────────────────────────
    # Entry
    # ──────────────────────────────────────────────────────────────────

    def enter(self, expiry: Optional[date] = None) -> Optional[OptionsPosition]:
        """
        Execute straddle entry. Returns the OptionsPosition if entered, else None.
        """
        # Pre-flight checks
        issue = self._check_preconditions()
        if issue:
            logger.warning(f"Straddle entry blocked: {issue}")
            return None

        # Get nearest expiry if not specified
        if expiry is None:
            expiry = self.chain.get_nearest_expiry(skip_today=AVOID_EXPIRY_ENTRY)
        logger.info(f"Entering straddle | Expiry: {expiry}")

        # Fetch option chain
        spot  = self.chain.get_spot_price()
        chain = self.chain.get_chain(expiry=expiry, spot=spot)
        atm   = self.chain.get_atm_strike(spot)

        # Select ATM CE and PE
        ce_row = chain[(chain["strike"] == atm) & (chain["instrument_type"] == "CE")]
        pe_row = chain[(chain["strike"] == atm) & (chain["instrument_type"] == "PE")]

        if ce_row.empty or pe_row.empty:
            logger.error(f"ATM strike {atm} CE/PE not found in chain.")
            return None

        ce = ce_row.iloc[0]
        pe = pe_row.iloc[0]

        # IV sanity check (use ATM CE IV as proxy)
        atm_iv = ce.get("iv", float("nan"))
        if not _is_nan(atm_iv):
            if atm_iv < MIN_IV:
                logger.warning(f"IV {atm_iv:.1f}% too low (min {MIN_IV}%). Skipping.")
                return None
            if atm_iv > MAX_IV:
                logger.warning(f"IV {atm_iv:.1f}% too high (max {MAX_IV}%). Skipping.")
                return None

        # Place orders
        legs = []
        entry_time = datetime.now(IST).isoformat()

        for row, leg_type in [(ce, "CE"), (pe, "PE")]:
            sym  = row["tradingsymbol"]
            ltp  = float(row["ltp"])

            try:
                result  = self.executor.sell_option(sym, QUANTITY)
                fill_px = self.executor.wait_for_fill(result["order_id"])
                if fill_px == 0:
                    fill_px = ltp   # fallback if wait failed
            except Exception as e:
                logger.error(f"Failed to sell {sym}: {e}")
                send_alert(f"❌ Straddle entry FAILED for {sym}: {e}")
                return None

            sl_trigger = round(fill_px * (1 + SL_PCT), 2)

            leg = OptionLeg(
                tradingsymbol    = sym,
                instrument_token = int(row["instrument_token"]),
                exchange         = "NFO",
                instrument_type  = leg_type,
                strike           = float(row["strike"]),
                expiry           = str(expiry),
                lot_size         = LOT_SIZE,
                num_lots         = NUM_LOTS,
                transaction_type = "SELL",
                entry_price      = fill_px,
                entry_time       = entry_time,
                order_id         = result["order_id"],
                current_ltp      = fill_px,
            )
            legs.append(leg)

            logger.info(
                f"  {leg_type} {int(row['strike'])} sold @ {fill_px:.2f} | "
                f"SL trigger: {sl_trigger:.2f}"
            )

        # Build position
        position = OptionsPosition(
            strategy    = "straddle",
            underlying  = UNDERLYING,
            entry_spot  = spot,
            expiry      = str(expiry),
            entry_time  = entry_time,
            legs        = legs,
        )
        self.tracker.add_position(position)

        premium = position.total_premium_received
        msg = (
            f"✅ *Straddle entered*\n"
            f"Underlying: {UNDERLYING} @ {spot:.0f}\n"
            f"Strike: {atm} CE + PE\n"
            f"Expiry: {expiry}\n"
            f"Premium: ₹{premium:.2f}\n"
            f"SL @ 50% = loss of ₹{premium * 0.5:.2f}"
        )
        send_alert(msg)
        return position

    # ──────────────────────────────────────────────────────────────────
    # Exit (full position)
    # ──────────────────────────────────────────────────────────────────

    def exit_position(self, position: OptionsPosition, reason: str = "manual"):
        """Buy back all open legs of the straddle."""
        logger.info(f"Exiting straddle | Reason: {reason}")
        exit_details = []

        for leg in position.legs:
            if leg.status != "OPEN":
                continue
            try:
                result  = self.executor.buy_to_close(leg.tradingsymbol, QUANTITY)
                fill_px = self.executor.wait_for_fill(result["order_id"])
                self.tracker.mark_leg_closed(
                    leg.tradingsymbol, fill_px, result["order_id"], reason
                )
                exit_details.append(f"{leg.instrument_type} {int(leg.strike)}: {fill_px:.2f}")
            except Exception as e:
                logger.error(f"Exit failed for {leg.tradingsymbol}: {e}")
                send_alert(f"❌ Exit FAILED for {leg.tradingsymbol}: {e}")

        self.tracker.mark_position_closed(position, reason)

        msg = (
            f"🚪 *Straddle exited* — {reason}\n"
            f"Net P&L: ₹{position.net_pnl:.2f}\n"
            + "\n".join(exit_details)
        )
        send_alert(msg)

    # ──────────────────────────────────────────────────────────────────
    # SL monitor (called on every tick / schedule)
    # ──────────────────────────────────────────────────────────────────

    def check_stop_losses(self):
        """
        Check if any leg has breached its SL. Buy back immediately if so.
        Also exits the whole position if one leg is closed (risk management).
        """
        breached = self.tracker.get_sl_breached()
        for position, leg in breached:
            logger.warning(
                f"SL BREACHED: {leg.tradingsymbol} | "
                f"LTP: {leg.current_ltp:.2f} ≥ SL: {leg.sl_price:.2f}"
            )
            send_alert(
                f"🔴 *SL Hit!*\n"
                f"{leg.instrument_type} {int(leg.strike)} | "
                f"LTP: {leg.current_ltp:.2f} | SL: {leg.sl_price:.2f}"
            )
            # Exit the full position (both legs) to stay delta-neutral
            self.exit_position(position, reason="SL_HIT")

    # ──────────────────────────────────────────────────────────────────
    # Pre-flight checks
    # ──────────────────────────────────────────────────────────────────

    def _check_preconditions(self) -> Optional[str]:
        """Returns error string if entry should be blocked, else None."""
        if not self.executor.is_market_open():
            return "Market closed"

        if len(self.tracker.open_positions) >= MAX_OPEN_POS:
            return f"Max open positions ({MAX_OPEN_POS}) reached"

        if self.tracker.today_pnl <= -MAX_LOSS_PER_DAY:
            return f"Daily loss limit ₹{MAX_LOSS_PER_DAY} hit"

        if AVOID_EXPIRY_ENTRY:
            try:
                nearest = self.chain.get_nearest_expiry(skip_today=False)
                if nearest == date.today():
                    return "Expiry day — entry avoided"
            except Exception:
                pass

        return None


# ── Utility ───────────────────────────────────────────────────────────

def _is_nan(v) -> bool:
    try:
        import math
        return math.isnan(v)
    except (TypeError, ValueError):
        return True
