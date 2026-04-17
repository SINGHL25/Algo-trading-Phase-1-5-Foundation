"""
core/option_chain.py
────────────────────
Fetch the live NIFTY option chain from Kite Connect.

Key responsibilities:
  - Download the full NFO instrument dump once per session (cached)
  - Filter to a specific underlying + expiry
  - Return a structured DataFrame with CE/PE rows, strikes, LTP, OI, volume
  - Identify ATM strike from spot price

Usage:
    chain = OptionChain()
    df    = chain.get_chain(expiry="2024-01-25")
    atm   = chain.get_atm_strike(spot=21500)
"""

import os
import logging
import pandas as pd
import numpy as np
from datetime import datetime, date
from functools import lru_cache
from typing import Optional
import pytz

from core.kite_client import get_kite

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# Strike rounding base (NIFTY strikes are multiples of 50)
STRIKE_STEP = 50


class OptionChain:
    def __init__(self, underlying: str = None, exchange: str = "NFO"):
        self.underlying = underlying or os.getenv("UNDERLYING", "NIFTY 50")
        self.exchange    = exchange
        self.kite        = get_kite()
        self._instruments_cache: Optional[pd.DataFrame] = None

    # ──────────────────────────────────────────────────────────────────
    # Instruments
    # ──────────────────────────────────────────────────────────────────

    def _load_instruments(self) -> pd.DataFrame:
        """Download NFO instrument dump (heavy — cached per session)."""
        if self._instruments_cache is not None:
            return self._instruments_cache

        logger.info("Downloading NFO instrument dump from Kite…")
        raw = self.kite.instruments(self.exchange)
        df  = pd.DataFrame(raw)

        # Normalise column types
        df["expiry"]       = pd.to_datetime(df["expiry"])
        df["strike"]       = pd.to_numeric(df["strike"], errors="coerce")
        df["lot_size"]     = pd.to_numeric(df["lot_size"], errors="coerce")

        self._instruments_cache = df
        logger.info(f"Loaded {len(df)} NFO instruments.")
        return df

    def get_expiries(self) -> list[date]:
        """Return all available weekly/monthly expiries for the underlying."""
        df = self._load_instruments()
        mask = (
            df["name"].str.upper().str.contains(self._nifty_name()) &
            (df["instrument_type"].isin(["CE", "PE"]))
        )
        expiries = sorted(df[mask]["expiry"].dt.date.unique().tolist())
        return expiries

    def get_nearest_expiry(self, skip_today: bool = True) -> date:
        """Return the nearest (usually weekly) expiry."""
        today    = date.today()
        expiries = self.get_expiries()
        upcoming = [e for e in expiries if e >= today]
        if skip_today:
            upcoming = [e for e in upcoming if e != today]
        if not upcoming:
            raise ValueError("No upcoming expiries found.")
        return upcoming[0]

    # ──────────────────────────────────────────────────────────────────
    # Option chain
    # ──────────────────────────────────────────────────────────────────

    def get_chain(
        self,
        expiry: Optional[date] = None,
        num_strikes: int = 20,
        spot: Optional[float] = None,
    ) -> pd.DataFrame:
        """
        Build a clean option chain DataFrame for a given expiry.

        Columns returned:
          strike, instrument_type (CE/PE), tradingsymbol, instrument_token,
          lot_size, expiry, ltp, bid, ask, oi, volume, iv, delta, gamma,
          theta, vega, intrinsic, time_value
        """
        if expiry is None:
            expiry = self.get_nearest_expiry()

        df   = self._load_instruments()
        mask = (
            df["name"].str.upper().str.contains(self._nifty_name()) &
            (df["instrument_type"].isin(["CE", "PE"])) &
            (df["expiry"].dt.date == expiry)
        )
        chain_instruments = df[mask].copy()

        if chain_instruments.empty:
            raise ValueError(f"No options found for expiry {expiry}. Check instrument name.")

        # Get ATM region to filter strikes
        if spot is None:
            spot = self.get_spot_price()

        atm    = self._round_to_strike(spot)
        radius = num_strikes * STRIKE_STEP
        chain_instruments = chain_instruments[
            (chain_instruments["strike"] >= atm - radius) &
            (chain_instruments["strike"] <= atm + radius)
        ].copy()

        # Fetch live quotes for all tokens in one API call
        tokens = chain_instruments["instrument_token"].astype(str).tolist()
        quotes = self._fetch_quotes(tokens)

        # Merge quote data into chain
        chain_instruments = chain_instruments.reset_index(drop=True)
        ltp_list, oi_list, vol_list, bid_list, ask_list = [], [], [], [], []

        for _, row in chain_instruments.iterrows():
            q = quotes.get(f"NFO:{row['tradingsymbol']}", {})
            ltp_list.append(q.get("last_price", 0.0))
            oi_list.append(q.get("oi", 0))
            vol_list.append(q.get("volume", 0))
            depth = q.get("depth", {})
            bid_list.append(depth.get("buy",  [{}])[0].get("price", 0.0))
            ask_list.append(depth.get("sell", [{}])[0].get("price", 0.0))

        chain_instruments["ltp"]    = ltp_list
        chain_instruments["oi"]     = oi_list
        chain_instruments["volume"] = vol_list
        chain_instruments["bid"]    = bid_list
        chain_instruments["ask"]    = ask_list
        chain_instruments["spot"]   = spot
        chain_instruments["atm"]    = atm

        # Compute time to expiry (in years) for Greeks
        now      = datetime.now(IST)
        exp_dt   = datetime.combine(expiry, datetime.min.time()).replace(tzinfo=IST)
        dte_days = max((exp_dt - now).total_seconds() / 86400, 0.001)
        chain_instruments["dte_days"] = dte_days
        chain_instruments["dte_years"] = dte_days / 365.0

        # Compute IV + Greeks
        from core.greeks import compute_greeks_row
        greeks_data = chain_instruments.apply(compute_greeks_row, axis=1)
        chain_instruments = pd.concat([chain_instruments, greeks_data], axis=1)

        return chain_instruments.sort_values(["strike", "instrument_type"]).reset_index(drop=True)

    # ──────────────────────────────────────────────────────────────────
    # Spot price
    # ──────────────────────────────────────────────────────────────────

    def get_spot_price(self) -> float:
        """Fetch current NIFTY 50 spot price from NSE."""
        quote = self.kite.quote([f"NSE:{self.underlying}"])
        spot  = quote[f"NSE:{self.underlying}"]["last_price"]
        logger.debug(f"Spot price: {spot}")
        return float(spot)

    # ──────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────

    def get_atm_strike(self, spot: Optional[float] = None) -> int:
        if spot is None:
            spot = self.get_spot_price()
        return self._round_to_strike(spot)

    def get_otm_strikes(self, spot: float, num_otm: int = 2) -> dict:
        """Return CE OTM and PE OTM strikes num_otm steps away from ATM."""
        atm = self._round_to_strike(spot)
        return {
            "atm":      atm,
            "ce_otm":   atm + num_otm * STRIKE_STEP,   # OTM call = above spot
            "pe_otm":   atm - num_otm * STRIKE_STEP,   # OTM put  = below spot
        }

    @staticmethod
    def _round_to_strike(price: float) -> int:
        return int(round(price / STRIKE_STEP) * STRIKE_STEP)

    def _nifty_name(self) -> str:
        """Returns the 'name' field used in NFO instruments for NIFTY."""
        if "BANKNIFTY" in self.underlying.upper() or "BANK" in self.underlying.upper():
            return "BANKNIFTY"
        return "NIFTY"

    def _fetch_quotes(self, tokens: list[str]) -> dict:
        """Fetch quotes in batches of 200 (Kite API limit)."""
        all_quotes = {}
        batch_size = 200
        instruments = self._instruments_cache

        for i in range(0, len(tokens), batch_size):
            batch_tokens = tokens[i : i + batch_size]
            # Convert tokens → tradingsymbols for quote key
            syms = [
                f"NFO:{r['tradingsymbol']}"
                for _, r in instruments[
                    instruments["instrument_token"].astype(str).isin(batch_tokens)
                ].iterrows()
            ]
            if not syms:
                continue
            try:
                q = self.kite.quote(syms)
                all_quotes.update(q)
            except Exception as e:
                logger.warning(f"Quote fetch error for batch: {e}")
        return all_quotes
