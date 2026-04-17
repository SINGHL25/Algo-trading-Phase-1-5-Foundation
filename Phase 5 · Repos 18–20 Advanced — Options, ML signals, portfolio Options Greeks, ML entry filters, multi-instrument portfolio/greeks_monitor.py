"""
utils/greeks_monitor.py
────────────────────────
Continuously polls live quotes and recomputes Greeks for all open positions.

Runs on a schedule (default: every 60s during market hours).
Updates the PositionTracker with fresh LTPs and Greeks.
Sends Telegram alert if:
  - IV spikes > 5% in a single update
  - Net delta drifts > MAX_DELTA_NET
  - Any leg is within 20% of SL trigger

Usage (from main loop):
    monitor = GreeksMonitor()
    schedule.every(60).seconds.do(monitor.update)
"""

import os
import logging
import numpy as np
import pandas as pd
from datetime import datetime, date
from typing import Optional
import pytz

from core.kite_client      import get_kite
from core.greeks           import implied_vol, bs_greeks, RISK_FREE_RATE, DIVIDEND_YIELD
from core.position_tracker import PositionTracker, OptionLeg
from utils.telegram_alerts import send_alert

logger = logging.getLogger(__name__)
IST    = pytz.timezone("Asia/Kolkata")

MAX_DELTA_NET   = float(os.getenv("MAX_DELTA_NET",  "0.15"))
IV_SPIKE_ALERT  = 5.0    # alert if IV changes by this % in one update
SL_WARN_PCT     = 0.80   # alert when LTP is 80% of SL trigger


class GreeksMonitor:
    def __init__(self):
        self.kite    = get_kite()
        self.tracker = PositionTracker()
        self._prev_iv: dict[str, float] = {}   # symbol → last known IV

    def update(self):
        """Fetch live quotes, update LTPs and Greeks for all open positions."""
        open_positions = self.tracker.open_positions
        if not open_positions:
            return

        # Collect all unique symbols + their metadata
        leg_meta: dict[str, dict] = {}
        for pos in open_positions:
            for leg in pos.legs:
                if leg.status == "OPEN":
                    leg_meta[leg.tradingsymbol] = {
                        "leg":    leg,
                        "strike": leg.strike,
                        "otype":  leg.instrument_type,
                        "expiry": leg.expiry,
                    }

        if not leg_meta:
            return

        # Fetch quotes in one batch
        symbols = [f"NFO:{sym}" for sym in leg_meta]
        try:
            quotes = self.kite.quote(symbols)
        except Exception as e:
            logger.error(f"Quote fetch failed: {e}")
            return

        # Compute Greeks and update tracker
        ltp_map    = {}
        greeks_map = {}

        for sym, meta in leg_meta.items():
            q   = quotes.get(f"NFO:{sym}", {})
            ltp = float(q.get("last_price", 0.0))
            if ltp == 0:
                continue

            ltp_map[sym] = ltp
            leg          = meta["leg"]
            strike       = meta["strike"]
            otype        = meta["otype"]
            expiry       = date.fromisoformat(meta["expiry"])

            # Time to expiry
            now     = datetime.now(IST)
            exp_dt  = datetime.combine(expiry, datetime.min.time()).replace(tzinfo=IST)
            dte_yrs = max((exp_dt - now).total_seconds() / 86400, 0.001) / 365.0

            # Spot (use current LTP approximation; ideally fetch NSE spot)
            spot = self._get_spot_approx(leg)

            # IV
            iv = implied_vol(ltp, spot, strike, dte_yrs, otype)

            if iv is not None and not np.isnan(iv):
                greeks = bs_greeks(spot, strike, dte_yrs,
                                   RISK_FREE_RATE, DIVIDEND_YIELD, iv, otype)
                greeks["iv"] = round(iv * 100, 2)   # as %
            else:
                greeks = {"iv": np.nan}

            greeks_map[sym] = greeks

            # IV spike detection
            prev_iv = self._prev_iv.get(sym)
            if prev_iv and not np.isnan(greeks.get("iv", np.nan)):
                iv_change = abs(greeks["iv"] - prev_iv)
                if iv_change >= IV_SPIKE_ALERT:
                    send_alert(
                        f"📈 *IV Spike* on {sym}\n"
                        f"IV: {prev_iv:.1f}% → {greeks['iv']:.1f}% "
                        f"(Δ{iv_change:.1f}%)"
                    )
            if not np.isnan(greeks.get("iv", np.nan)):
                self._prev_iv[sym] = greeks["iv"]

            # SL proximity warning
            sl_warn = leg.entry_price * (1 + float(os.getenv("SL_PCT", "0.50")) * SL_WARN_PCT)
            if ltp >= sl_warn and leg.status == "OPEN":
                send_alert(
                    f"⚠️ *SL Proximity Warning*\n"
                    f"{sym} LTP={ltp:.2f} approaching SL={leg.sl_price:.2f}"
                )

        # Push updates to tracker
        self.tracker.update_ltps(ltp_map)
        self.tracker.update_greeks(greeks_map)

        # Net delta check
        for pos in open_positions:
            net_delta = sum(
                greeks_map.get(l.tradingsymbol, {}).get("delta", 0.0) * l.quantity
                for l in pos.legs if l.status == "OPEN"
            )
            if abs(net_delta) > MAX_DELTA_NET:
                send_alert(
                    f"⚠️ *Delta Drift Alert*\n"
                    f"{pos.strategy.upper()} net delta = {net_delta:.3f} "
                    f"(limit: ±{MAX_DELTA_NET})"
                )

        # Log summary
        logger.debug(self._log_summary(ltp_map, greeks_map))

    def _get_spot_approx(self, leg: OptionLeg) -> float:
        """
        Approximate spot from position entry. For Greeks accuracy,
        ideally fetch NSE:NIFTY 50 quote — done here only if needed.
        """
        try:
            q = self.kite.quote(["NSE:NIFTY 50"])
            return float(q["NSE:NIFTY 50"]["last_price"])
        except Exception:
            return float(leg.strike)   # fallback: use strike as proxy

    def _log_summary(self, ltp_map: dict, greeks_map: dict) -> str:
        lines = [f"Greeks update @ {datetime.now(IST).strftime('%H:%M:%S')}"]
        for sym, ltp in ltp_map.items():
            g   = greeks_map.get(sym, {})
            iv  = g.get("iv",    "—")
            d   = g.get("delta", "—")
            th  = g.get("theta", "—")
            lines.append(
                f"  {sym}: LTP={ltp:.2f} | IV={iv}% | "
                f"Δ={d} | Θ={th}/day"
            )
        return "\n".join(lines)
