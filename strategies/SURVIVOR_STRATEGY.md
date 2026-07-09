# Survivor Strategy — NIFTY Weekly Options Selling

**Source**: [github.com/Raahi-Bhushan/trading-algo/strategy/survivor.py](https://github.com/Raahi-Bhushan/trading-algo/blob/main/strategy/survivor.py)  
**Instrument**: NIFTY weekly options (PE + CE)  
**Exchange**: NFO  
**Style**: Short premium / options selling  

---

## Strategy Concept

The Survivor strategy is a **systematic options selling machine**. It monitors the NIFTY spot index and automatically sells out-of-the-money (OTM) options whenever the market moves a configurable number of points in either direction.

```
NIFTY rises 20+ pts from reference → SELL a PE (200 pts below current spot)
NIFTY falls 20+ pts from reference → SELL a CE (200 pts above current spot)
```

The intuition is simple: premium decay (theta) works in the seller's favour. By selling options that are already 200 points away from the current price, the strategy collects premium that expires worthless the majority of the time. A "reset" mechanism prevents the reference from drifting indefinitely in one direction.

---

## How It Works — Step by Step

### 1. Dual Reference Tracking
Two independent reference values are maintained:
- `pe_ref` — tracks the reference for PE selling (rises after each PE sell)
- `ce_ref` — tracks the reference for CE selling (falls after each CE sell)

Both are initialised to the current NIFTY price at strategy start (or configured start points).

### 2. Trade Trigger
Every 1-minute bar:

```
PE sell trigger:   current_price − pe_ref  ≥ pe_gap (20 pts)
CE sell trigger:   ce_ref − current_price  ≥ ce_gap (20 pts)
```

### 3. Multiplier Scaling
The gap can be crossed by more than one `pe_gap` unit in a single step:
```
multiplier = floor(diff / gap)   [capped at sell_multiplier_threshold = 5]
quantity   = base_quantity × multiplier
```
This means a sudden 60-point move triggers 3× the normal position size.

### 4. Strike Selection
```
PE strike = round_to_50( current_spot − pe_symbol_gap )   → 200 pts OTM put
CE strike = round_to_50( current_spot + ce_symbol_gap )   → 200 pts OTM call
```
If the option at that strike is below `min_price_to_sell` (₹15), the strategy shifts the strike 50 points closer to ATM (up to 8 attempts) until it finds an option with adequate premium.

### 5. Reference Update
After each sell:
```
pe_ref = old_pe_ref + pe_gap × multiplier
ce_ref = old_ce_ref − ce_gap × multiplier
```
This means the reference "follows" the market so the next trigger requires another full `pe_gap` move.

### 6. Reset Logic
To prevent the reference from permanently diverging from spot:
```
PE reset:  (pe_ref − current_price) ≥ pe_reset_gap (30 pts)
           → pe_ref = current_price + pe_gap   [reference pulled back]

CE reset:  (current_price − ce_ref) ≥ ce_reset_gap (30 pts)
           → ce_ref = current_price − ce_gap
```

### 7. Exit
The strategy has **no intraday stop-loss** — it holds positions to weekly expiry. Settlement = last traded price of the option on expiry day (effectively intrinsic value or near-zero for far OTM options).

---

## Configuration (survivor.yml)

```yaml
default:
  # Underlying index for trigger
  index_symbol:   "NSE:NIFTY 50"

  # Option series prefix — update each week!
  symbol_initials: "NIFTY25807"       # e.g. NIFTY25JAN30 for Jan 30 expiry

  # Trigger gaps (points)
  pe_gap:          20    # NIFTY must rise 20 pts to trigger PE sell
  ce_gap:          20    # NIFTY must fall 20 pts to trigger CE sell

  # Strike distance from spot (points)
  pe_symbol_gap:  200    # sell PE 200 pts below spot
  ce_symbol_gap:  200    # sell CE 200 pts above spot

  # Position sizing (multiples of lot size)
  pe_quantity:     75    # 3 lots × 25 = 75 (post Nov-2024 lot size)
  ce_quantity:     75

  # Risk filters
  min_price_to_sell:       15    # skip options below ₹15 premium
  sell_multiplier_threshold: 5   # max 5× scaling on a single trigger

  # Reset thresholds (points)
  pe_reset_gap:    30
  ce_reset_gap:    30

  # Starting reference (0 = use current market price)
  pe_start_point:   0
  ce_start_point:   0

  # Execution
  trans_type:    "SELL"
  exchange:      "NFO"
  order_type:    "MARKET"
  product_type:  "NRML"
  tag:           "Survivor"
```

---

## Execution Instructions

### Prerequisites
```bash
pip install uv
cp .sample.env .env   # add broker credentials
```

### Run Live
```bash
# Install deps
uv sync

# Start the strategy
uv run python strategy/survivor.py \
    --symbol-initials NIFTY25JAN30 \
    --pe-gap 20 --ce-gap 20 \
    --pe-quantity 75 --ce-quantity 75 \
    --min-price-to-sell 15

# With custom start points (useful after mid-session restart)
uv run python strategy/survivor.py \
    --symbol-initials NIFTY25JAN30 \
    --pe-start-point 23500 \
    --ce-start-point 23500
```

### Environment Variables Required (`.env`)
```
BROKER_NAME=zerodha          # or fyers (requires code changes)
BROKER_API_KEY=...
BROKER_API_SECRET=...
BROKER_TOTP_ENABLE=false
```

### Weekly Maintenance Checklist
- [ ] Update `symbol_initials` in config before each week (new expiry prefix)
- [ ] Confirm NIFTY lot size (changed from 50 → 25 in Nov 2024)
- [ ] Verify option premium >15 is available 200 pts OTM
- [ ] Check NRML margin availability (requires ~₹2–3L per strategy run)

---

## Backtest Results

**Backtest Period**: 3 Oct 2024 → 9 May 2026 (84 weekly expiries)  
**Initial Capital**: ₹20,00,000  
**Data Source**: Historify DuckDB (NSE_INDEX NIFTY 1m + NFO weekly options 1m)

### Summary

| Metric | Value |
|--------|-------|
| Total Trades | **98** |
| PE Trades | 20 |
| CE Trades | 78 |
| Total P&L | **+₹6,91,322** |
| Return on Capital | **+34.6%** |
| Win Rate | **82.7%** |
| Avg P&L / Trade | ₹7,054 |
| Best Trade | +₹64,958 |
| Worst Trade | −₹2,69,561 |
| Max Drawdown | −₹5,07,056 |

### Monthly Breakdown

| Month | P&L | Trades | Note |
|-------|-----|--------|------|
| Oct 2024 | **+₹5,80,624** | 32 | Nifty crashed Oct 3–4 → CEs sold into collapse expired worthless |
| Nov 2024 | +₹1,66,916 | 17 | Continued bear trend, clean CE selling |
| Dec 2024 | — | 0 | No triggers (low volatility) |
| Jan 2025 | +₹36,864 | 11 | Jan 30 expiry partially ITM, small loss netted |
| Feb 2025 | +₹1,43,756 | 9 | Clean CE expiry week after week |
| Mar 2025 | +₹52,459 | 4 | Sparse triggers |
| Apr 2025 | +₹1,59,222 | 6 | Tariff panic Apr 7 → CEs sold near crash bottom, expired worthless |
| Jun 2025 | +₹2,884 | 1 | Single PE trade, small profit |
| Oct 2025 | −₹44,790 | 8 | Market bounced, Nov 4 expiry PEs got ITM |
| Nov 2025 | −₹27,206 | 6 | PE selling into downtrend, 3 trades ITM at expiry |
| Jan 2026 | +₹3,799 | 2 | Tiny PE profit |
| Apr 2026 | **−₹3,83,205** | 2 | **Catastrophic** — sold CE at 22,450/22,400 on Apr 2, NIFTY surged to 23,100+ by Apr 7 expiry |

### Notable Trades

**Best Trade**: `NIFTY09APR2522450CE` — sold at ₹173 on Apr 7, 2025 (tariff panic day), expired at ₹0.10. P&L: **+₹64,958** on 375 qty.

**Worst Trade**: `NIFTY07APR2622450CE` — sold at ₹225 on Apr 2, 2026, NIFTY rallied hard to 23,100+, option worth ₹675 at expiry. P&L: **−₹2,69,561** on 600 qty.

### Key Observations

1. **Strong CE bias** (78 of 98 trades) — reflects the 2024–2025 bear trend in NIFTY.
2. **Clustered activity** — most triggers fired in volatile weeks; low-volatility months had zero trades.
3. **No stop-loss is the core risk** — the April 2026 week erased ~55% of the strategy's total profit in 2 trades.
4. **Lot-size caveat** — NIFTY lot size changed from 50 → 25 in Nov 2024; the backtest uses 25 throughout.

### Risk Warnings
- **No stop-loss**: a 300+ point directional move in the week of expiry = full loss on accumulated position
- **Multiplier risk**: a 100-point gap breach on a single bar → 5× position (₹9,375 premium per lot at avg prices)
- **Liquidity**: deep OTM options (200 pts away) can have wide bid-ask spreads; MARKET orders may fill worse

---

## Recommended Improvements

| Improvement | Impact |
|-------------|--------|
| Add intraday stop-loss (3× premium paid) | Caps worst-case loss per trade |
| Use LIMIT orders instead of MARKET | Reduces slippage on OTM options |
| Reduce pe_symbol_gap to 100 on expiry day | Better premium on same-day expires |
| Skip selling within 30 min of market open | Avoids gap-up/gap-down spike fills |
| Add VIX filter (skip when VIX < 12) | Low VIX = low premium, poor risk/reward |
