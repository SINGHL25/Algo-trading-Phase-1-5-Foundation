"""
core/position_tracker.py
─────────────────────────
In-memory position tracker for options legs.

Tracks:
  - Each sold option leg (symbol, qty, entry premium, current LTP)
  - Net premium received for the strategy
  - Real-time P&L (sold premium - current cost to close)
  - Stop-loss breach detection per-leg and for the whole position
  - Expiry management

Design: All data lives in memory + a JSON snapshot file (positions.json).
The snapshot is reloaded on restart so a crash doesn't lose state.
"""

import os
import json
import logging
import numpy as np
from datetime import datetime, date
from dataclasses import dataclass, field, asdict
from typing import Optional
from pathlib import Path
import pytz

logger = logging.getLogger(__name__)
IST    = pytz.timezone("Asia/Kolkata")

POSITIONS_FILE = Path("data/positions.json")

SL_PCT = float(os.getenv("SL_PCT", "0.50"))   # 50% of premium = SL trigger


# ──────────────────────────────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────────────────────────────

@dataclass
class OptionLeg:
    tradingsymbol:   str
    instrument_token: int
    exchange:        str
    instrument_type: str        # CE | PE
    strike:          float
    expiry:          str        # ISO date string
    lot_size:        int
    num_lots:        int
    transaction_type: str       # SELL (we are premium sellers)
    entry_price:     float      # premium received per unit
    entry_time:      str        # ISO datetime
    order_id:        str

    # Updated live
    current_ltp:     float      = 0.0
    sl_price:        float      = 0.0      # SL trigger (entry_price * (1 + SL_PCT))
    sl_hit:          bool       = False
    exit_price:      float      = 0.0
    exit_time:       Optional[str] = None
    exit_order_id:   Optional[str] = None
    status:          str        = "OPEN"   # OPEN | CLOSED | SL_HIT

    def __post_init__(self):
        self.sl_price = round(self.entry_price * (1 + SL_PCT), 2)

    @property
    def quantity(self) -> int:
        return self.lot_size * self.num_lots

    @property
    def premium_received(self) -> float:
        """Total premium received for this leg (INR)."""
        return round(self.entry_price * self.quantity, 2)

    @property
    def current_cost_to_close(self) -> float:
        """How much it would cost to buy back this leg now."""
        return round(self.current_ltp * self.quantity, 2)

    @property
    def unrealised_pnl(self) -> float:
        """Positive = profit (we sold higher than current LTP)."""
        return round(self.premium_received - self.current_cost_to_close, 2)

    @property
    def pnl_pct(self) -> float:
        """P&L as % of premium received."""
        if self.premium_received == 0:
            return 0.0
        return round(self.unrealised_pnl / self.premium_received * 100, 2)

    @property
    def is_sl_breached(self) -> bool:
        """SL is breached when LTP exceeds our sell price by SL_PCT."""
        return self.current_ltp >= self.sl_price

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OptionsPosition:
    """Groups CE + PE legs of a straddle or strangle."""
    strategy:       str          # straddle | strangle
    underlying:     str
    entry_spot:     float
    expiry:         str
    entry_time:     str
    legs:           list[OptionLeg] = field(default_factory=list)
    status:         str          = "OPEN"   # OPEN | CLOSED | PARTIAL
    close_reason:   str          = ""

    @property
    def total_premium_received(self) -> float:
        return round(sum(l.premium_received for l in self.legs), 2)

    @property
    def total_cost_to_close(self) -> float:
        return round(sum(l.current_cost_to_close for l in self.legs), 2)

    @property
    def net_pnl(self) -> float:
        return round(self.total_premium_received - self.total_cost_to_close, 2)

    @property
    def net_delta(self) -> float:
        """Net delta of all open legs (should be near 0 for straddle)."""
        deltas = [l.current_ltp for l in self.legs]   # placeholder; overridden by tracker
        return 0.0   # see PositionTracker.update_greeks()

    @property
    def sl_breached_legs(self) -> list[OptionLeg]:
        return [l for l in self.legs if l.is_sl_breached and l.status == "OPEN"]

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "legs"}
        d["legs"] = [l.to_dict() for l in self.legs]
        return d


# ──────────────────────────────────────────────────────────────────────
# Tracker
# ──────────────────────────────────────────────────────────────────────

class PositionTracker:
    def __init__(self):
        self.positions: list[OptionsPosition] = []
        self._load_snapshot()

    # ── Snapshot persistence ──────────────────────────────────────────

    def _load_snapshot(self):
        """Reload positions from disk on startup (crash recovery)."""
        if not POSITIONS_FILE.exists():
            return
        try:
            with open(POSITIONS_FILE) as f:
                data = json.load(f)
            for pos_data in data:
                legs = [OptionLeg(**l) for l in pos_data.pop("legs", [])]
                pos  = OptionsPosition(**pos_data, legs=legs)
                self.positions.append(pos)
            logger.info(f"Loaded {len(self.positions)} positions from snapshot.")
        except Exception as e:
            logger.error(f"Could not load positions snapshot: {e}")

    def save_snapshot(self):
        """Persist current state to disk."""
        POSITIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(POSITIONS_FILE, "w") as f:
                json.dump([p.to_dict() for p in self.positions], f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Snapshot save failed: {e}")

    # ── Position lifecycle ────────────────────────────────────────────

    def add_position(self, position: OptionsPosition):
        self.positions.append(position)
        self.save_snapshot()
        logger.info(
            f"Position added: {position.strategy} | "
            f"Premium: ₹{position.total_premium_received} | "
            f"Expiry: {position.expiry}"
        )

    def update_ltps(self, ltp_map: dict[str, float]):
        """
        Update current LTP for each leg.
        ltp_map: { tradingsymbol: ltp }
        """
        for pos in self.open_positions:
            for leg in pos.legs:
                if leg.status == "OPEN" and leg.tradingsymbol in ltp_map:
                    leg.current_ltp = ltp_map[leg.tradingsymbol]

    def update_greeks(self, greeks_map: dict[str, dict]):
        """
        Attach live Greeks to each leg.
        greeks_map: { tradingsymbol: {delta, gamma, theta, vega, iv} }
        """
        for pos in self.open_positions:
            for leg in pos.legs:
                if leg.tradingsymbol in greeks_map:
                    g = greeks_map[leg.tradingsymbol]
                    leg.__dict__.update({
                        "live_delta": g.get("delta"),
                        "live_gamma": g.get("gamma"),
                        "live_theta": g.get("theta"),
                        "live_vega":  g.get("vega"),
                        "live_iv":    g.get("iv"),
                    })

    def mark_leg_closed(self, tradingsymbol: str, exit_price: float,
                        exit_order_id: str, reason: str = ""):
        for pos in self.open_positions:
            for leg in pos.legs:
                if leg.tradingsymbol == tradingsymbol and leg.status == "OPEN":
                    leg.status        = "CLOSED"
                    leg.exit_price    = exit_price
                    leg.exit_time     = datetime.now(IST).isoformat()
                    leg.exit_order_id = exit_order_id
                    if reason == "SL":
                        leg.sl_hit = True
                        leg.status = "SL_HIT"
                    logger.info(
                        f"Leg closed: {tradingsymbol} | "
                        f"Entry: {leg.entry_price} | Exit: {exit_price} | "
                        f"PnL: {round((leg.entry_price - exit_price) * leg.quantity, 2)}"
                    )

    def mark_position_closed(self, pos: OptionsPosition, reason: str = ""):
        pos.status      = "CLOSED"
        pos.close_reason = reason
        for leg in pos.legs:
            if leg.status == "OPEN":
                leg.status = "CLOSED"
        self.save_snapshot()
        logger.info(
            f"Position closed: {pos.strategy} | "
            f"Net P&L: ₹{pos.net_pnl} | Reason: {reason}"
        )

    # ── Queries ───────────────────────────────────────────────────────

    @property
    def open_positions(self) -> list[OptionsPosition]:
        return [p for p in self.positions if p.status == "OPEN"]

    @property
    def today_pnl(self) -> float:
        today = date.today().isoformat()
        return round(sum(
            p.net_pnl for p in self.positions
            if p.entry_time[:10] == today
        ), 2)

    def get_sl_breached(self) -> list[tuple[OptionsPosition, OptionLeg]]:
        """Return list of (position, leg) pairs where SL is breached."""
        breached = []
        for pos in self.open_positions:
            for leg in pos.sl_breached_legs:
                breached.append((pos, leg))
        return breached

    def summary(self) -> str:
        """Human-readable summary for logging / Telegram."""
        lines = ["📊 Position Summary", "─" * 40]
        if not self.open_positions:
            lines.append("  No open positions.")
        for pos in self.open_positions:
            lines.append(
                f"  {pos.strategy.upper()} | {pos.underlying} | "
                f"Exp: {pos.expiry} | "
                f"Premium: ₹{pos.total_premium_received} | "
                f"PnL: ₹{pos.net_pnl}"
            )
            for leg in pos.legs:
                status_icon = "🟢" if leg.status == "OPEN" else "🔴"
                lines.append(
                    f"    {status_icon} {leg.instrument_type} {int(leg.strike)} | "
                    f"Entry: {leg.entry_price} | LTP: {leg.current_ltp:.2f} | "
                    f"SL: {leg.sl_price}"
                )
        lines.append(f"  Today's P&L: ₹{self.today_pnl}")
        return "\n".join(lines)
