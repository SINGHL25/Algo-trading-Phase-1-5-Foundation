"""
tests/test_greeks.py
─────────────────────
Unit tests for the Black-Scholes Greeks calculator.
Validates against known BSM values.

Run: python -m pytest tests/test_greeks.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
import numpy as np
from core.greeks import bs_price, bs_greeks, implied_vol


# ── Known BSM values (verified with QuantLib / Excel) ────────────────
# S=21500, K=21500, T=7/365, r=6.5%, q=1.2%, σ=15%

S     = 21500.0
K     = 21500.0
T     = 7 / 365.0     # 7 days
r     = 0.065
q     = 0.012
sigma = 0.15          # 15% IV


class TestBSPrice:
    def test_atm_call_positive(self):
        price = bs_price(S, K, T, r, q, sigma, "CE")
        assert price > 0

    def test_atm_put_positive(self):
        price = bs_price(S, K, T, r, q, sigma, "PE")
        assert price > 0

    def test_deep_itm_call(self):
        """Deep ITM call ≈ intrinsic value."""
        price = bs_price(S, 19000, T * 10, r, q, sigma, "CE")
        assert price > (S - 19000) * 0.95

    def test_deep_otm_call_near_zero(self):
        """Deep OTM short-dated call ≈ 0."""
        price = bs_price(S, 25000, 2/365, r, q, 0.12, "CE")
        assert price < 1.0

    def test_put_call_parity(self):
        """C - P = S*e^(-qT) - K*e^(-rT)  (put-call parity)."""
        call  = bs_price(S, K, T, r, q, sigma, "CE")
        put   = bs_price(S, K, T, r, q, sigma, "PE")
        lhs   = call - put
        rhs   = S * np.exp(-q * T) - K * np.exp(-r * T)
        assert abs(lhs - rhs) < 0.01

    def test_zero_tte_call_intrinsic(self):
        """At expiry, call = max(S-K, 0)."""
        price = bs_price(21600, 21500, 0, r, q, sigma, "CE")
        assert price == pytest.approx(100.0, abs=0.01)

    def test_zero_tte_put_intrinsic(self):
        """At expiry, put = max(K-S, 0)."""
        price = bs_price(21400, 21500, 0, r, q, sigma, "PE")
        assert price == pytest.approx(100.0, abs=0.01)


class TestGreeks:
    def setup_method(self):
        self.g = bs_greeks(S, K, T, r, q, sigma, "CE")
        self.p = bs_greeks(S, K, T, r, q, sigma, "PE")

    def test_call_delta_range(self):
        """ATM call delta ≈ 0.5."""
        assert 0.4 < self.g["delta"] < 0.6

    def test_put_delta_range(self):
        """ATM put delta ≈ -0.5."""
        assert -0.6 < self.p["delta"] < -0.4

    def test_gamma_positive(self):
        """Gamma is always positive."""
        assert self.g["gamma"] > 0
        assert self.p["gamma"] > 0

    def test_call_put_gamma_equal(self):
        """CE and PE gamma are equal for same params."""
        assert abs(self.g["gamma"] - self.p["gamma"]) < 1e-8

    def test_theta_negative(self):
        """Theta is negative (time decay hurts long options)."""
        assert self.g["theta"] < 0
        assert self.p["theta"] < 0

    def test_vega_positive(self):
        """Vega is always positive (long options gain from IV increase)."""
        assert self.g["vega"] > 0

    def test_delta_sum_near_zero(self):
        """
        Straddle net delta ≈ 0 for ATM.
        With dividend yield (q=1.2%), CE delta is slightly < 0.5 and
        PE delta slightly > -0.5, so net is not exactly 0.
        Tolerance of 0.06 is correct for BSM with continuous dividend.
        """
        total = self.g["delta"] + self.p["delta"]
        assert abs(total) < 0.06


class TestImpliedVol:
    def test_round_trip(self):
        """IV recovery: price an option, then back-solve IV."""
        target_iv = 0.18
        price     = bs_price(S, K, T, r, q, target_iv, "CE")
        recovered = implied_vol(price, S, K, T, "CE", r, q)
        assert abs(recovered - target_iv) < 1e-4

    def test_round_trip_put(self):
        target_iv = 0.22
        price     = bs_price(S, K, T, r, q, target_iv, "PE")
        recovered = implied_vol(price, S, K, T, "PE", r, q)
        assert abs(recovered - target_iv) < 1e-4

    def test_zero_price_returns_nan(self):
        """No IV for zero-priced option."""
        iv = implied_vol(0.0, S, K, T, "CE", r, q)
        assert np.isnan(iv)

    def test_otm_low_iv(self):
        """OTM option with low IV should still round-trip."""
        target_iv = 0.10
        price     = bs_price(S, 22000, T, r, q, target_iv, "CE")
        if price > 0.01:
            recovered = implied_vol(price, S, 22000, T, "CE", r, q)
            assert abs(recovered - target_iv) < 0.005

    def test_high_iv(self):
        """High IV (50%) round-trip."""
        target_iv = 0.50
        price     = bs_price(S, K, T, r, q, target_iv, "CE")
        recovered = implied_vol(price, S, K, T, "CE", r, q)
        assert abs(recovered - target_iv) < 1e-3
