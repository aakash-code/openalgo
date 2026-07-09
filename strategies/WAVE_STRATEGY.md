# Wave Strategy — NIFTY Futures Scalping

**Source**: [github.com/Raahi-Bhushan/trading-algo/strategy/wave.py](https://github.com/Raahi-Bhushan/trading-algo/blob/main/strategy/wave.py)  
**Instrument**: NIFTY near-month futures (NFO)  
**Exchange**: NFO  
**Style**: Mean-reversion scalping with dynamic gap scaling  

---

## Strategy Concept

Wave is a **market-maker style futures scalper**. It simultaneously places a LIMIT BUY below the current price and a LIMIT SELL above the current price, capturing the spread as the market oscillates. When the position becomes one-sided (e.g., many more buys than sells), the gap multiplier increases dramatically — making further buys harder and harder — acting as a natural position limit without a hard stop.

```
Always offers:
  BUY  at  min(current_price, prev_wave_buy_price)  −  buy_gap
  SELL at  max(current_price, prev_wave_sell_price) +  sell_gap
```

When both sides fill, the round-trip profit is `(sell_price − buy_price) × qty`.

---

## How It Works — Step by Step

### 1. Initialisation
At session start:
- Subscribe to NIFTY futures websocket
- Record current price as initial reference
- `prev_wave_buy_price = None`, `prev_wave_sell_price = None`

### 2. Order Placement (each tick / each cycle)
```
current_price  = live futures price
scaled_buy_gap, scaled_sell_gap = get_scaled_gaps(net_position)

buy_price  = min(current_price − scaled_buy_gap,  prev_buy_price  − scaled_buy_gap)
sell_price = max(current_price + scaled_sell_gap, prev_sell_price + scaled_sell_gap)
```
After a `cool_off_time` (10s between cycles), both a LIMIT BUY and LIMIT SELL are placed.

### 3. Gap Scaling (the "wave" mechanic)

The multiplier table below governs how gaps expand when position is imbalanced:

| Net Lots | Buy Gap Multiplier | Sell Gap Multiplier |
|----------|--------------------|---------------------|
| 0 (balanced) | 1.0× | 1.0× |
| +1 (1 net long) | 1.3× | 1.0× |
| +2 | 1.7× | 1.0× |
| +3 | 2.5× | 1.0× |
| +4 | 3.0× | 1.0× |
| +5 to +7 | 10.0× | 1.0× |
| +8 to +10 | 15.0× | 1.0× |
| −1 (1 net short) | 1.0× | 1.3× |
| −2 | 1.0× | 1.7× |
| (and so on…) | | |

**Example**: If net position is +3 lots long and buy_gap = 25, the effective buy gap becomes `25 × 2.5 = 62.5 pts`. The next buy only fires if the market drops 62.5 pts from the last reference — a strong self-limiting mechanism.

### 4. Delta Restrictions (Portfolio Greeks)
Before placing orders, the strategy computes portfolio delta using Black-Scholes (via `mibian`):

```
if portfolio_delta < min_nifty_delta:  → restrict SELL orders and PE buys
if portfolio_delta > max_nifty_delta:  → restrict BUY orders and CE buys
```

In the backtest, this is simplified to a max net lots cap (`max_net_lots = ±10`).

### 5. End-of-Day Square-Off
All remaining open positions are squared off at market close (15:29). EOD exits may be at a loss if the position moved against the strategy.

### 6. Order Matching Logic
```
Bar high  ≥ sell_price  → SELL filled at sell_price
Bar low   ≤ buy_price   → BUY  filled at buy_price
Matched pair (1 buy + 1 sell): P&L = (sell_px − buy_px) × lot_qty
Unmatched at EOD: closed at close price
```

---

## Configuration (wave.yml)

```yaml
default:
  # Instrument
  symbol_name:   "NIFTY25SEPFUT"    # update to current near-month
  exchange:      "NFO"

  # Gap parameters (points)
  buy_gap:       25    # buy limit = current_price − 25
  sell_gap:      25    # sell limit = current_price + 25

  # Position sizing
  buy_quantity:  75    # 1 lot = 75 units (NIFTY lot post Nov-2024 change = 25; adjust!)
  sell_quantity: 75
  lot_size:      75

  # Timing
  cool_off_time: 10    # seconds between order cycles

  # Product / order type
  product_type:  "NRML"
  order_type:    "LIMIT"
  variety:       "REGULAR"
  tag:           "WaveScraper"

  # Greeks / delta management
  min_nifty_delta:      -100
  max_nifty_delta:       100
  min_bank_nifty_delta: -100
  max_bank_nifty_delta:  100
  interest_rate:         10.0   # %
  todays_volatility:     20.0   # %
  delta_calculation_days: 10    # only positions within 10 DTE considered for delta

  # Margin parameters (for display only)
  margin_spread:         100.0
  margin_single_pe_ce:   100.0
  margin_both_pe_ce:     100.0
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

# Start (reads config from strategy/configs/wave.yml)
uv run python strategy/wave.py

# Override symbol from command line (if supported)
SYMBOL=NFO:NIFTY25DECFUT uv run python strategy/wave.py
```

### Environment Variables Required (`.env`)
```
BROKER_NAME=zerodha          # or fyers
BROKER_API_KEY=...
BROKER_API_SECRET=...
```

### Monthly Maintenance Checklist
- [ ] Update `symbol_name` in wave.yml at the start of each month (roll to new expiry)
- [ ] Confirm NIFTY futures lot size (25 since Nov 2024 — config shows 75, verify!)
- [ ] Ensure sufficient NRML margin (~₹1.5L per lot for NIFTY futures)
- [ ] Monitor net position during volatile sessions (gap scaling prevents runaway but doesn't stop it completely)

### Important Notes
- The strategy runs **intraday only** (all positions squared off at close)
- Works best in **range-bound / oscillating markets**
- Trending days (large directional moves) generate EOD losses
- The `cool_off_time = 10s` means ~360 order cycles per 1-hour session

---

## Backtest Results

**Backtest Period**: 26 Jul 2024 → 28 Aug 2025 (13 months)  
**Initial Capital**: ₹20,00,000  
**Data Source**: Historify DuckDB (NFO near-month NIFTY futures 1m OHLC)  
**Note**: Futures data in DB covers Jul 2024 – Aug 2025; results beyond Aug 2025 not available.

### Summary

| Metric | Value |
|--------|-------|
| Total Trades | **594** |
| Buy Trades | 313 |
| Sell Trades | 281 |
| Total P&L | **+₹22,31,576** |
| Return on Capital | **+111.6%** |
| Win Rate | **72.6%** |
| Avg P&L / Trade | ₹3,757 |
| Best Trade | +₹37,388 |
| Worst Trade | −₹29,629 |
| Max Drawdown | **−₹49,912** (~2.5% of capital) |

### Monthly Breakdown

| Month | P&L | Trades | Trend |
|-------|-----|--------|-------|
| Jul 2024 | +₹27,293 | 6 | Partial month (started Jul 26) |
| Aug 2024 | +₹41,093 | 31 | Volatile, mostly range-bound |
| Sep 2024 | +₹92,269 | 32 | Strong oscillation |
| Oct 2024 | +₹1,95,570 | 59 | Best oscillating month, high volatility |
| Nov 2024 | +₹1,41,184 | 59 | Continued bear trend with reversals |
| Dec 2024 | +₹2,65,864 | 57 | Best single month |
| Jan 2025 | **+₹2,86,057** | 56 | Highest monthly P&L |
| Feb 2025 | +₹2,29,226 | 48 | Continued profits |
| Mar 2025 | +₹1,29,420 | 39 | Trending market, fewer oscillations |
| Apr 2025 | +₹2,31,289 | 57 | Tariff panic created large swings → scalped well |
| May 2025 | +₹2,14,267 | 55 | Steady |
| Jun 2025 | +₹2,10,547 | 45 | Strong |
| Jul 2025 | +₹73,508 | 28 | Quieter month |
| Aug 2025 | +₹93,990 | 22 | Partial (to Aug 28) |

**Every single month was profitable.**

### Trade Breakdown by Exit Type

| Exit Reason | Count | Avg P&L |
|-------------|-------|---------|
| Matched (round-trip) | ~320 | Higher |
| EOD (squared at close) | ~274 | Mixed |

EOD exits are where losses occur — when the market trends strongly in one direction and a position cannot be matched before close.

### Notable Trades
- **Best**: +₹37,388 — EOD carry on a large gap-up day where buy filled at bottom and closed near top
- **Worst**: −₹29,629 — EOD carry on a sharp trending day where position moved fully against
- **Most consistent**: October and January had 59 and 56 trades respectively with very high hit rates

### Key Observations

1. **Zero losing months** over 13 months — remarkable consistency.
2. **Tiny max drawdown** (₹49,912 = 2.5% of capital) despite 111% return — superior risk/reward.
3. **The multiplier scale works**: on extreme trending days, gap scaling prevents runaway accumulation.
4. **EOD losses are the primary risk** — the strategy is not trend-following; strong one-directional days hurt.
5. **High trade frequency**: 594 trades over 13 months = ~45 trades/month = ~2 trades/day.

### Risk Warnings
- **Trending market risk**: a sustained 200+ point move in one direction within a session can accumulate multiple losing lots before EOD square-off
- **Liquidity/slippage**: LIMIT orders may not fill at exact prices; in fast markets the price can gap past the level
- **Roll risk**: near expiry, futures basis can widen unpredictably — change symbol before last 3 days of contract
- **Margin**: at 10 net lots, required margin = ~₹15L for NIFTY futures (NRML)

---

## Comparison: Wave vs Survivor

| | Wave | Survivor |
|--|------|---------|
| Instrument | Futures | Weekly Options |
| Style | Scalping | Premium selling |
| Period | 13 months | 18 months |
| Total P&L | **+₹22.3L** | +₹6.9L |
| Return | **+111.6%** | +34.6% |
| Win Rate | 72.6% | **82.7%** |
| Max Drawdown | **−₹49,912** | −₹5,07,056 |
| Worst month | None | −₹3.83L (Apr 2026) |
| Trade frequency | High (~45/month) | Low (~5/month) |
| Capital required | ~₹5–15L (margin) | ~₹2–3L (premium received) |
| Risk | Trending days | No stop-loss on options |

**Bottom line**: Wave is a better risk-adjusted performer in this backtest. Survivor has higher per-trade win rate but the catastrophic April 2026 loss shows the danger of naked option selling without a stop.

---

## Recommended Improvements

| Improvement | Impact |
|-------------|--------|
| Add VIX filter (pause when VIX > 25) | Avoids very high-volatility trending days |
| Increase buy_gap to 35 on trending days (ATR filter) | Fewer losing EOD positions |
| Implement proper Greeks delta calculation live | Better restriction enforcement |
| Add max daily loss circuit breaker | Caps worst-case day |
| Reduce lot_size to 25 (current NIFTY lot) | Config currently shows 75 — verify before going live |
