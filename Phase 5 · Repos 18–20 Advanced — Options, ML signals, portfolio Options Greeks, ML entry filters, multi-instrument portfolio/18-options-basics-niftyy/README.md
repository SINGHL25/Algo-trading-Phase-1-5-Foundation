# 18-options-basics-nifty

**Repo 18 of 20** in the [Algo Trading GitHub Series](../README.md)

NIFTY options premium selling bot — Short Straddle & Strangle strategies with live Greeks tracking, ATR-based stop-losses, and expiry management. Connects to Zerodha via Kite Connect.

---

## Architecture

```
TradingView (optional signal)
        │
        ▼
  main.py  ─── schedule loop
        │
        ├── strategies/straddle.py   ← sell ATM CE + PE
        ├── strategies/strangle.py   ← sell OTM CE + PE
        ├── strategies/expiry_manager.py
        │
        ├── core/option_chain.py     ← fetch live NFO chain
        ├── core/greeks.py           ← Black-Scholes IV/Greeks
        ├── core/order_executor.py   ← Kite place/cancel/poll
        ├── core/position_tracker.py ← in-memory + JSON state
        │
        └── utils/
            ├── greeks_monitor.py    ← 60s poll loop
            ├── telegram_alerts.py   ← push notifications
            └── generate_token.py    ← daily token refresh
```

---

## What This Repo Covers

| Feature | File | Notes |
|---|---|---|
| Option chain fetch | `core/option_chain.py` | Full NFO dump, cached per session |
| ATM / OTM strike selection | `core/option_chain.py` | `get_atm_strike()`, `get_otm_strikes()` |
| Black-Scholes pricing | `core/greeks.py` | European BSM, dividend yield included |
| Implied Volatility | `core/greeks.py` | Brent's method solver |
| Delta / Gamma / Theta / Vega | `core/greeks.py` | Per-leg and net position |
| Short straddle | `strategies/straddle.py` | Sell ATM CE + PE |
| Short strangle | `strategies/strangle.py` | Sell OTM CE + PE, configurable width |
| Stop-loss per leg | `core/position_tracker.py` | 50% of premium received (configurable) |
| Expiry management | `strategies/expiry_manager.py` | Auto-close by 14:30, worthless-expire logic |
| Live Greeks monitor | `utils/greeks_monitor.py` | IV spike + delta drift alerts |
| Telegram notifications | `utils/telegram_alerts.py` | Entry, SL, EOD summary |
| Daily token refresh | `utils/generate_token.py` | Manual or automated |

---

## Prerequisites

| Requirement | Details |
|---|---|
| Python | 3.11+ |
| Zerodha account | With F&O trading enabled |
| Kite Connect API | ₹500/month — [create app](https://developers.kite.trade/) |
| TradingView | Optional — for signal entry instead of scheduled entry |

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/yourusername/18-options-basics-nifty.git
cd 18-options-basics-nifty
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your API keys, lot size, risk settings
```

Key settings in `.env`:

```bash
KITE_API_KEY=your_key
KITE_API_SECRET=your_secret
STRATEGY=straddle          # or strangle
LOT_SIZE=50                # NIFTY lot size — verify on NSE
NUM_LOTS=1                 # start with 1 lot
SL_PCT=0.50                # SL at 50% of premium received
MAX_LOSS_PER_DAY=5000      # daily loss circuit breaker (INR)
STRANGLE_OTM_STRIKES=2     # OTM distance for strangle
```

### 3. Generate daily access token

```bash
python utils/generate_token.py
# Follow the prompts — takes 30 seconds
```

> ⚠️ Kite access tokens expire at midnight. Regenerate every morning before the bot starts.

### 4. View live option chain (test API connection)

```bash
python utils/display_chain.py
python utils/display_chain.py --expiry 2024-01-25 --strikes 15
```

### 5. Run tests

```bash
python -m pytest tests/test_greeks.py -v
```

### 6. Run the bot

```bash
python main.py
```

---

## Strategy Details

### Short Straddle

Sell ATM Call + ATM Put at the same strike.

```
NIFTY Spot @ 21,500
  Sell 21500 CE @ ₹120
  Sell 21500 PE @ ₹115
  ─────────────────────
  Total premium: ₹235 × 50 = ₹11,750
  Breakeven: 21265 — 21735
  SL (50%): exit if either leg LTP > entry × 1.5
```

**Best conditions:**
- High IV environment (Θ decays faster)
- Weekly expiry (Thursday)
- No major events (RBI policy, Budget, election results)

### Short Strangle

Sell OTM Call + OTM Put (N strikes from ATM).

```
NIFTY Spot @ 21,500 (OTM = 2 strikes)
  Sell 21600 CE @ ₹65
  Sell 21400 PE @ ₹60
  ─────────────────────
  Total premium: ₹125 × 50 = ₹6,250
  Breakeven: 21275 — 21725
  Wider range but lower premium than straddle
```

**When to prefer strangle over straddle:**
- Very high IV (>25%) — straddle too expensive to defend
- Expecting sideways but with some range

---

## Greeks Reference

| Greek | Meaning for Short Options | What to watch |
|---|---|---|
| **Delta** | Position's sensitivity to spot | Net delta should stay near 0. Alert if |Δ| > 0.15 |
| **Gamma** | Rate of delta change | High gamma near expiry = more risk |
| **Theta** | Daily premium decay (your income) | Higher = faster decay = better for seller |
| **Vega** | Sensitivity to IV change | IV spike = loss for option seller |
| **IV** | Implied volatility (market's fear gauge) | Enter when IV > 15%; exit if IV spikes >5% in one tick |

---

## Stop-Loss Logic

```
SL price = entry_price × (1 + SL_PCT)

Example: Sold CE @ ₹120, SL_PCT = 0.50
  SL trigger = ₹120 × 1.50 = ₹180

If CE LTP ≥ ₹180 → buy back BOTH legs (full position exit)
```

**Why exit both legs on one SL hit?**  
When one leg goes against you, the position is no longer delta-neutral. Holding the other leg adds directional risk. The cleaner move is to exit everything and re-evaluate.

---

## Expiry Day Rules

| Time | Action |
|---|---|
| 09:20 IST | Morning alert: expiry day warning |
| 14:00 IST | Warning: 30 min to force-exit |
| 14:30 IST | Force-exit all non-worthless legs |
| Market close | EOD P&L summary sent to Telegram |

**Worthless threshold:** If a leg's LTP ≤ ₹1.00, let it expire at zero — saves ₹40–60 in brokerage per leg.

---

## Risk Warnings

> ⚠️ **Options selling carries unlimited risk. Read before trading:**

1. **Short straddle/strangle has unlimited upside risk** — if NIFTY moves sharply in one direction (e.g. ±3% in a day), losses can exceed premium collected many times over.

2. **Gap risk** — overnight gaps cannot be protected by intraday SL. Avoid holding positions overnight unless you understand the risk.

3. **Event risk** — RBI policy, Union Budget, election results, global shocks can cause extreme moves. Check the economic calendar daily.

4. **SEBI algo trading rules (2025)** — retail traders are required to obtain prior approval for automated strategies. Read the SEBI circular before going live.

5. **Paper trade first** — use `12-paper-trading-engine` (repo 12) for at least 4 weeks before risking real capital.

---

## Project Structure

```
18-options-basics-nifty/
├── main.py                         ← Bot entry point
├── requirements.txt
├── .env.example
│
├── core/
│   ├── kite_client.py              ← Kite Connect singleton
│   ├── option_chain.py             ← NFO chain fetch + ATM/OTM selection
│   ├── greeks.py                   ← Black-Scholes IV, Delta, Gamma, Theta, Vega
│   ├── position_tracker.py         ← In-memory + JSON position state
│   └── order_executor.py           ← Kite order placement + SL-M
│
├── strategies/
│   ├── straddle.py                 ← Short straddle entry/exit/SL
│   ├── strangle.py                 ← Short strangle entry/exit/SL
│   └── expiry_manager.py           ← Expiry-day force-exit + EOD summary
│
├── utils/
│   ├── greeks_monitor.py           ← Live Greeks + IV spike + delta drift alerts
│   ├── telegram_alerts.py          ← Push notifications
│   ├── display_chain.py            ← Terminal option chain viewer
│   └── generate_token.py           ← Daily Kite token refresh
│
├── tests/
│   └── test_greeks.py              ← Black-Scholes unit tests
│
├── data/
│   └── positions.json              ← Auto-generated — live position state
│
└── logs/
    ├── bot.log                     ← Main bot log
    └── orders.jsonl                ← JSONL order audit trail
```

---

## Previous Repo (Required)

This repo depends on:
- **[01-zerodha-kite-setup](../01-zerodha-kite-setup)** — Kite auth + token management
- **[04-kite-order-manager](../04-kite-order-manager)** — Order execution patterns

---

## Next Repo

**[19-ml-signal-filter](../19-ml-signal-filter)** — XGBoost classifier to filter entry signals using OHLCV + indicator features. Reduces false entries before the strategy executes.

---

## Disclaimer

This software is for educational purposes only. It is not financial advice. Options trading involves significant risk of loss. The author is not a SEBI-registered investment advisor. Always paper trade first. Never risk money you cannot afford to lose.
