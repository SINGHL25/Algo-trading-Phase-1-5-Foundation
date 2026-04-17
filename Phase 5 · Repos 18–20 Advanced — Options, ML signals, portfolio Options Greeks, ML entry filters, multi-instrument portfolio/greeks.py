"""
core/greeks.py
──────────────
Black-Scholes Greeks calculator for NIFTY options.

Functions:
  bs_price()       — theoretical option price
  bs_greeks()      — returns dict of delta/gamma/theta/vega/rho
  implied_vol()    — Newton-Raphson IV solver
  compute_greeks_row() — pandas apply helper for the option chain DataFrame

Notes:
  - Uses European BSM model (good approximation for NIFTY index options)
  - Risk-free rate from RBI repo rate (set in env or default 6.5%)
  - Dividend yield assumed 1.2% for NIFTY (adjust as needed)
  - Theta is expressed as INR per day (divide by 365 internally)
"""

import os
import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq
import pandas as pd
import logging

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────
RISK_FREE_RATE = float(os.getenv("RISK_FREE_RATE", "0.065"))   # 6.5%
DIVIDEND_YIELD = float(os.getenv("DIVIDEND_YIELD", "0.012"))   # 1.2%
IV_LOWER_BOUND = 0.001
IV_UPPER_BOUND = 5.0    # 500%


# ──────────────────────────────────────────────────────────────────────
# Core Black-Scholes formulas
# ──────────────────────────────────────────────────────────────────────

def _d1(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    return (np.log(S / K) + (r - q + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))


def _d2(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    return _d1(S, K, T, r, q, sigma) - sigma * np.sqrt(T)


def bs_price(
    S: float,
    K: float,
    T: float,
    r: float,
    q: float,
    sigma: float,
    option_type: str,
) -> float:
    """
    Black-Scholes theoretical price.

    Args:
        S           : spot price
        K           : strike price
        T           : time to expiry in years
        r           : risk-free rate (e.g. 0.065)
        q           : dividend yield (e.g. 0.012)
        sigma       : implied volatility (e.g. 0.18 for 18%)
        option_type : 'CE' or 'PE'
    """
    if T <= 0 or sigma <= 0:
        # Intrinsic value at expiry
        if option_type == "CE":
            return max(S - K, 0.0)
        return max(K - S, 0.0)

    d1 = _d1(S, K, T, r, q, sigma)
    d2 = _d2(S, K, T, r, q, sigma)

    if option_type == "CE":
        return (S * np.exp(-q * T) * norm.cdf(d1)
                - K * np.exp(-r * T) * norm.cdf(d2))
    else:
        return (K * np.exp(-r * T) * norm.cdf(-d2)
                - S * np.exp(-q * T) * norm.cdf(-d1))


def bs_greeks(
    S: float,
    K: float,
    T: float,
    r: float,
    q: float,
    sigma: float,
    option_type: str,
) -> dict:
    """
    Return a dict of Greeks:
      delta   — sensitivity to spot (CE: 0→1, PE: -1→0)
      gamma   — sensitivity of delta to spot (always positive)
      theta   — time decay per calendar day (negative = value lost)
      vega    — sensitivity to 1% change in IV
      rho     — sensitivity to 1% change in rate
    """
    if T <= 1e-6 or sigma <= 1e-6:
        return {"delta": np.nan, "gamma": np.nan, "theta": np.nan,
                "vega": np.nan, "rho": np.nan}

    d1 = _d1(S, K, T, r, q, sigma)
    d2 = _d2(S, K, T, r, q, sigma)

    nd1  = norm.cdf(d1)
    nd1_ = norm.pdf(d1)   # standard normal PDF

    # Delta
    if option_type == "CE":
        delta = np.exp(-q * T) * nd1
    else:
        delta = np.exp(-q * T) * (nd1 - 1)

    # Gamma (same for CE and PE)
    gamma = (np.exp(-q * T) * nd1_) / (S * sigma * np.sqrt(T))

    # Theta (per calendar day; divide BS formula by 365)
    if option_type == "CE":
        theta = (
            -(S * np.exp(-q * T) * nd1_ * sigma) / (2 * np.sqrt(T))
            - r * K * np.exp(-r * T) * norm.cdf(d2)
            + q * S * np.exp(-q * T) * nd1
        ) / 365
    else:
        theta = (
            -(S * np.exp(-q * T) * nd1_ * sigma) / (2 * np.sqrt(T))
            + r * K * np.exp(-r * T) * norm.cdf(-d2)
            - q * S * np.exp(-q * T) * norm.cdf(-d1)
        ) / 365

    # Vega (per 1% change in vol → divide by 100)
    vega = S * np.exp(-q * T) * nd1_ * np.sqrt(T) / 100

    # Rho (per 1% change in rate → divide by 100)
    if option_type == "CE":
        rho = K * T * np.exp(-r * T) * norm.cdf(d2) / 100
    else:
        rho = -K * T * np.exp(-r * T) * norm.cdf(-d2) / 100

    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta, 4),
        "vega":  round(vega,  4),
        "rho":   round(rho,   4),
    }


# ──────────────────────────────────────────────────────────────────────
# Implied Volatility solver
# ──────────────────────────────────────────────────────────────────────

def implied_vol(
    market_price: float,
    S: float,
    K: float,
    T: float,
    option_type: str,
    r: float = RISK_FREE_RATE,
    q: float = DIVIDEND_YIELD,
) -> float:
    """
    Solve for IV using Brent's method (robust, no initial guess needed).

    Returns IV as a decimal (e.g. 0.18 = 18%).
    Returns np.nan if solution not found (deep ITM/OTM with zero market price).
    """
    if market_price <= 0 or T <= 0:
        return np.nan

    # Intrinsic value check — can't have IV if price < intrinsic
    if option_type == "CE":
        intrinsic = max(S - K, 0.0)
    else:
        intrinsic = max(K - S, 0.0)

    if market_price < intrinsic - 0.5:
        return np.nan

    def objective(sigma):
        return bs_price(S, K, T, r, q, sigma, option_type) - market_price

    try:
        iv = brentq(objective, IV_LOWER_BOUND, IV_UPPER_BOUND, xtol=1e-6, maxiter=200)
        return round(iv, 6)
    except (ValueError, RuntimeError):
        return np.nan


# ──────────────────────────────────────────────────────────────────────
# Pandas row helper
# ──────────────────────────────────────────────────────────────────────

def compute_greeks_row(row: pd.Series) -> pd.Series:
    """
    Called via df.apply(compute_greeks_row, axis=1).
    Expects row to have: ltp, spot, strike, dte_years, instrument_type.
    Returns Series with iv, delta, gamma, theta, vega, intrinsic, time_value.
    """
    try:
        S    = float(row["spot"])
        K    = float(row["strike"])
        T    = float(row["dte_years"])
        ltp  = float(row["ltp"])
        otype = row["instrument_type"]   # 'CE' or 'PE'

        iv = implied_vol(ltp, S, K, T, otype)
        if np.isnan(iv) or iv <= 0:
            greeks = {"delta": np.nan, "gamma": np.nan,
                      "theta": np.nan, "vega": np.nan, "rho": np.nan}
        else:
            greeks = bs_greeks(S, K, T, RISK_FREE_RATE, DIVIDEND_YIELD, iv, otype)

        if otype == "CE":
            intrinsic = max(S - K, 0.0)
        else:
            intrinsic = max(K - S, 0.0)

        time_value = max(ltp - intrinsic, 0.0)

        return pd.Series({
            "iv":          round(iv * 100, 2) if not np.isnan(iv) else np.nan,  # as %
            "delta":       greeks["delta"],
            "gamma":       greeks["gamma"],
            "theta":       greeks["theta"],
            "vega":        greeks["vega"],
            "intrinsic":   round(intrinsic, 2),
            "time_value":  round(time_value, 2),
        })

    except Exception as e:
        logger.debug(f"Greeks compute error on row {row.get('tradingsymbol', '?')}: {e}")
        return pd.Series({
            "iv": np.nan, "delta": np.nan, "gamma": np.nan,
            "theta": np.nan, "vega": np.nan,
            "intrinsic": np.nan, "time_value": np.nan,
        })
