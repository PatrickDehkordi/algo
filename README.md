# rh-market-maker

An Avellaneda-Stoikov market-making algorithm for Robinhood, executed through
the official **Robinhood Agentic MCP** endpoint. No username/password required —
authentication is handled entirely via OAuth through Claude Code.

---

## How it works

```
┌─────────────────────────────────────────────────────────┐
│                     market_maker.py                     │
│                                                         │
│  VolEstimator (yfinance)                                │
│    ├─ Garman-Klass  on 5-min bars   (intraday reactive) │
│    └─ Yang-Zhang    on daily bars   (overnight gaps)    │
│         ↓ σ                                             │
│  VolOfVolDetector                                       │
│    └─ CV of rolling σ → spread multiplier              │
│         ↓ spread_mult                                   │
│  Avellaneda-Stoikov                                     │
│    ├─ reservation_price = mid − q·γ·σ²·T               │
│    └─ half_spread = f(γ, σ, T, κ) × spread_mult        │
│         ↓ bid / ask                                     │
│  FillRateTracker                                        │
│    └─ nudges κ toward target fill rate per side         │
│         ↓ orders                                        │
│  rh_client.py  →  Robinhood Agentic MCP endpoint        │
│                   (Agentic account only)                │
└─────────────────────────────────────────────────────────┘
```

### Key parameters

| Symbol | Role | Default |
|--------|------|---------|
| γ (gamma) | Inventory risk aversion — higher = more skew away from net position | 0.1 |
| σ (sigma) | Annualized vol — estimated live, fallback only | 0.15 |
| κ (kappa) | Order arrival rate — higher = tighter spread | 1.5 (auto-tuned) |
| T | Time horizon per cycle | 1/390 (≈1 min) |

---

## Prerequisites

- **Python 3.10+**
- **Claude Code** with the Robinhood MCP server registered and authenticated
  at user scope. The algo reads the OAuth token Claude Code stores in
  `~/.claude/.credentials.json` — no separate login needed.
- A Robinhood account with an **Agentic sub-account** (`agentic_allowed=true`).
  This is a separate sandboxed account created during the MCP auth flow;
  your main brokerage account is untouched.

### Register the MCP server (one-time)

```bash
claude mcp add robinhood-trading --scope user --transport http \
  https://agent.robinhood.com/mcp/trading
```

Then authenticate inside Claude Code:

```
/mcp  →  select robinhood-trading  →  Authenticate  →  complete OAuth in browser
```

---

## Installation

```bash
git clone https://github.com/your-username/rh-market-maker.git
cd rh-market-maker

pip install -r requirements.txt
```

---

## Running

### Quick start (safe defaults — small size, tight loss limit)

```bash
python market_maker.py --quote-size 0.1 --max-inventory 1.0 --max-loss 10
```

### Default settings

```bash
python market_maker.py
```

### Full options

```
python market_maker.py [OPTIONS]

Core
  --symbol TEXT              Stock to quote (default: SPY)
  --gamma FLOAT              Inventory risk aversion (default: 0.1)
  --kappa FLOAT              Initial order arrival rate (default: 1.5)
  --max-inventory FLOAT      Max shares long or short (default: 5.0)
  --quote-size FLOAT         Shares per side per cycle (default: 1.0)
  --poll-interval INT        Seconds between requote cycles (default: 30)
  --max-loss FLOAT           Kill switch: halt if PnL < -N dollars (default: 50)

Volatility estimator
  --vol-method [gk|yz|blend] Estimator: Garman-Klass / Yang-Zhang / blend (default: blend)
  --vol-gk-lookback INT      GK: number of 5-min bars (default: 78 = 1 day)
  --vol-yz-lookback INT      YZ: number of daily bars (default: 30 ≈ 6 weeks)
  --vol-refresh-secs INT     Re-estimate interval in seconds (default: 600)

Vol-of-vol detector
  --vov-window INT           Rolling window of σ estimates (default: 6)
  --vov-threshold FLOAT      CV above this fires WARNING (default: 0.20)
  --vov-scale FLOAT          spread_mult = 1 + scale × CV (default: 3.0)
  --vov-max-mult FLOAT       Cap on spread multiplier (default: 2.0)

Fill-rate tracker (kappa auto-tuner)
  --frt-window INT           Rolling window in cycles (default: 20)
  --frt-target FLOAT         Target fill rate per side (default: 0.30)
  --frt-kappa-min FLOAT      Minimum kappa (default: 0.3)
  --frt-kappa-max FLOAT      Maximum kappa (default: 15.0)
  --frt-adj-rate FLOAT       Proportional step per cycle (default: 0.05)
  --frt-imbalance-warn FLOAT Warn if |bid_rate − ask_rate| > this (default: 0.25)
```

---

## Sample log output

```
10:01:00  INFO     === Market maker: SPY  account=435199179 ===
10:01:02  INFO     σ (GK 5min):  0.1284 (12.8% ann)  n=78
10:01:03  INFO     σ (YZ daily): 0.1351 (13.5% ann)  n=29d
10:01:03  INFO     σ (blend) = (0.1284 + 0.1351) / 2 = 0.1318
10:01:03  INFO     Initial σ=0.1318  half-spread=$0.2330
10:01:04  INFO     Placed buy 1.0@594.77 → a1b2c3d4-...
10:01:04  INFO     Placed sell 1.0@595.23 → e5f6g7h8-...
10:01:34  INFO     Mid=595.01  σ=0.1318  CV=0.031  mult=1.09x  half=0.2540  κ=1.500  inv=0.00  PnL=+0.00
10:01:34  INFO     FRT warming up  bid=0.00  ask=0.00  n=(1,1)
10:02:04  INFO     ASK FILLED  -1.0@595.23 | inv=-1.00
10:02:04  INFO     Mid=595.10  σ=0.1318  CV=0.031  mult=1.09x  half=0.2540  κ=1.500  inv=-1.00  PnL=+0.13
```

---

## Important caveats

- **No maker rebates**: Robinhood does not pay maker rebates, so the algo
  earns the spread only when both sides fill. In practice the spread captured
  is reduced by market impact and adverse selection.
- **Agentic account only**: orders are routed to the dedicated Agentic
  sub-account. Your main brokerage account is never touched.
- **PDT rules do not apply** to the Agentic cash account (no margin).
- **Kill switch**: the algo halts and cancels all open orders if realized +
  unrealized PnL drops below `--max-loss`. Start small.

---

## File overview

| File | Purpose |
|------|---------|
| `market_maker.py` | Main algo: A-S math, vol pipeline, fill tracker, main loop |
| `rh_client.py` | HTTP client for the Robinhood Agentic MCP endpoint; handles OAuth token loading and refresh |
| `requirements.txt` | Python dependencies |
