"""
strategies/strangle.py
───────────────────────
Short Strangle on NIFTY

Strategy logic:
  - Sell OTM CE + OTM PE (N strikes away from ATM)
  - Lower premium than straddle but wider profit zone
  - SL per leg: LTP > entry_price × (1 + SL_PCT)
  - OTM distance controlled by STRANGLE_OTM_STRIKES (default: 2)

Example (NIFTY @ 21500, STRIKE_STEP=50, OTM=2):
  ATM  = 21500
  CE   = 21600  (2 strikes above ATM)
  PE   = 21400  (2 strikes below ATM)

The wider OTM selection reduces vega risk but earns less premium.
Adjust STRANGLE_OTM_STRIKES in .env based on IV environment:
  - High IV (>20%): 3–4 strikes OTM (wider wing)
  - Low IV (<15%):  1–2 strikes OTM (closer to ATM)
"""

import os
import logging
from datetime import datetime, date
from typing import Optional
import pytz

from core.option_chain     import OptionChain
from core.order_executor   import OrderExecutor
from core.position_tracker import OptionLeg, OptionsPosition, PositionTracker
from utils.telegram_alerts import send_alert

logger = logging.getLogger(__name__)
IST    = pytz.timezone("Asia/Kolkata")

UNDERLYING        = os.getenv("UNDERLYING",  "NIFTY 50")
LOT_SIZE          = int(os.getenv("LOT_SIZE",   "50"))
NUM_LOTS          = int(os.getenv("NUM_LOTS",    "1"))
MIN_IV            = float(os.getenv("MIN_IV",   "10"))
MAX_IV            = float(os.getenv("MAX_IV",   "80"))
MAX_OPEN_POS      = int(os.getenv("MAX_OPEN_POSITIONS", "2"))
MAX_LOSS_PER_DAY  = float(os.getenv("MAX_LOSS_PER_DAY", "5000"))
OTM_STRIKES       = int(os.getenv("STRANGLE_OTM_STRIKES", "2"))
AVOID_EXPIRY_ENTRY = os.getenv("AVOID_EXPIRY_ENTRY", "true").lower() == "true"
SL_PCT            = float(os.getenv("SL_PCT",   "0.50"))

QUANTITY = LOT_SIZE * NUM_LOTS


class StrangleStrategy:
    def __init__(self):
        self.chain    = OptionChain(underlying=UNDERLYING)
        self.executor = OrderExecutor()
        self.tracker  = PositionTracker()

    # ──────────────────────────────────────────────────────────────────
    # Entry
    # ──────────────────────────────────────────────────────────────────

    def enter(self, expiry: Optional[date] = None) -> Optional[OptionsPosition]:
        issue = self._check_preconditions()
        if issue:
            logger.warning(f"Strangle entry blocked: {issue}")
            return None

        if expiry is None:
            expiry = self.chain.get_nearest_expiry(skip_today=AVOID_EXPIRY_ENTRY)
        logger.info(f"Entering strangle | OTM={OTM_STRIKES} | Expiry: {expiry}")

        spot   = self.chain.get_spot_price()
        chain  = self.chain.get_chain(expiry=expiry, spot=spot)
        strikes = self.chain.get_otm_strikes(spot, num_otm=OTM_STRIKES)

        ce_strike = strikes["ce_otm"]
        pe_strike = strikes["pe_otm"]

        ce_row = chain[(chain["strike"] == ce_strike) & (chain["instrument_type"] == "CE")]
        pe_row = chain[(chain["strike"] == pe_strike) & (chain["instrument_type"] == "PE")]

        if ce_row.empty or pe_row.empty:
            logger.error(
                f"OTM strikes CE={ce_strike} / PE={pe_strike} not found in chain."
            )
            return None

        ce = ce_row.iloc[0]
        pe = pe_row.iloc[0]

        # IV gate on OTM leg average
        ce_iv = ce.get("iv", float("nan"))
        if not _is_nan(ce_iv):
            if ce_iv < MIN_IV:
                logger.warning(f"IV {ce_iv:.1f}% too low (min {MIN_IV}%). Skipping.")
                return None
            if ce_iv > MAX_IV:
                logger.warning(f"IV {ce_iv:.1f}% too high (max {MAX_IV}%). Skipping.")
                return None

        legs       = []
        entry_time = datetime.now(IST).isoformat()

        for row, leg_type in [(ce, "CE"), (pe, "PE")]:
            sym = row["tradingsymbol"]
            ltp = float(row["ltp"])

            try:
                result  = self.executor.sell_option(sym, QUANTITY)
                fill_px = self.executor.wait_for_fill(result["order_id"])
                if fill_px == 0:
                    fill_px = ltp
            except Exception as e:
                logger.error(f"Strangle entry failed for {sym}: {e}")
                send_alert(f"❌ Strangle entry FAILED for {sym}: {e}")
                return None

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
                f"SL: {leg.sl_price:.2f}"
            )

        position = OptionsPosition(
            strategy   = "strangle",
            underlying = UNDERLYING,
            entry_spot = spot,
            expiry     = str(expiry),
            entry_time = entry_time,
            legs       = legs,
        )
        self.tracker.add_position(position)

        premium = position.total_premium_received
        msg = (
            f"✅ *Strangle entered*\n"
            f"Underlying: {UNDERLYING} @ {spot:.0f}\n"
            f"CE: {ce_strike} | PE: {pe_strike} ({OTM_STRIKES} strikes OTM)\n"
            f"Expiry: {expiry}\n"
            f"Premium: ₹{premium:.2f}"
        )
        send_alert(msg)
        return position

    # ──────────────────────────────────────────────────────────────────
    # Exit
    # ──────────────────────────────────────────────────────────────────

    def exit_position(self, position: OptionsPosition, reason: str = "manual"):
        logger.info(f"Exiting strangle | Reason: {reason}")
        for leg in position.legs:
            if leg.status != "OPEN":
                continue
            try:
                result  = self.executor.buy_to_close(leg.tradingsymbol, QUANTITY)
                fill_px = self.executor.wait_for_fill(result["order_id"])
                self.tracker.mark_leg_closed(
                    leg.tradingsymbol, fill_px, result["order_id"], reason
                )
            except Exception as e:
                logger.error(f"Strangle exit failed for {leg.tradingsymbol}: {e}")
                send_alert(f"❌ Exit FAILED for {leg.tradingsymbol}: {e}")

        self.tracker.mark_position_closed(position, reason)
        send_alert(
            f"🚪 *Strangle exited* — {reason}\n"
            f"Net P&L: ₹{position.net_pnl:.2f}"
        )

    def check_stop_losses(self):
        breached = self.tracker.get_sl_breached()
        for position, leg in breached:
            logger.warning(f"SL BREACHED: {leg.tradingsymbol} @ {leg.current_ltp:.2f}")
            send_alert(
                f"🔴 *SL Hit — Strangle*\n"
                f"{leg.instrument_type} {int(leg.strike)} | "
                f"LTP: {leg.current_ltp:.2f} | SL: {leg.sl_price:.2f}"
            )
            self.exit_position(position, reason="SL_HIT")

    # ──────────────────────────────────────────────────────────────────
    # Pre-flight
    # ──────────────────────────────────────────────────────────────────

    def _check_preconditions(self) -> Optional[str]:
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


def _is_nan(v) -> bool:
    try:
        import math
        return math.isnan(v)
    except (TypeError, ValueError):
        return True
