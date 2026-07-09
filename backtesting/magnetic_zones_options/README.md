# Magnetic Zones — Options Selling Backtest

Quant backtest of NIFTY option-selling strategies built on the **Magnetic Zones [Open]**
indicator, using NIFTY spot 1-min + expired NFO option 1-min bars from `db/historify.duckdb`.

## The thesis

Magnetic Zones derives symmetric Fibonacci support/resistance **zones** from the **previous**
Daily/Weekly/Monthly candle's High/Low:

```
center = (prevHigh + prevLow) / 2 ,  rng = prevHigh - prevLow
R2 = center + 0.786*rng    R1 = center + 0.236*rng
S1 = center - 0.236*rng    S2 = center - 0.786*rng
zone half-width = rng * 0.13 / 2     # "price entered the zone" band
```

The zones act as price *magnets* / mean-reversion boundaries. We sell premium at the **outer**
zones (R2 for calls, S2 for puts), expecting price to stay contained.

## The matrix (12 configs)

| Axis | Values |
| --- | --- |
| **Entry** | `range_fade` (sell both sides at period open) · `touch_fade` (sell a side only when spot enters that zone) |
| **Structure** | `naked` (short strangle) · `hedged` (iron condor, buy a wing `HEDGE_WIDTH` pts beyond the short) |
| **Timeframe** | `daily_intraday` (yesterday's zones, enter 09:20, square-off 15:20) · `weekly_overnight` (last week's zones, hold to weekly expiry) · `monthly_overnight` (last month's zones, hold to monthly expiry) |

Each config is reported in two modes (the `cpr_option_selling` convention):

- **FAITHFUL** — hold to expiry (overnight) or square-off (intraday), settle at intrinsic / close.
  No SL/TP, no costs. The strategy's structural edge.
- **REALISTIC** — intra-period **TP** (premium decays `TP_PCT` of credit) / **SL**
  (loss ≥ `SL_MULT`×credit), per-leg slippage and full Indian charges (brokerage/STT/txn/SEBI/GST/stamp).

## Capital & sizing

Fixed **₹1 Cr** capital, **fixed lots** (`LOTS`, default 10). ROI is reported against the ₹1 Cr base.
Historically-correct NIFTY lot sizes (25 → 75 → 65) come from `expired_fno_contracts`.

## Run

```bash
uv run python -m backtesting.magnetic_zones_options.run_all
```

Env overrides:

```
START=2024-04-01  END=2026-06-09   # backtest window (catalog coverage)
LOTS=10  HEDGE_WIDTH=200  TP_PCT=0.5  SL_MULT=2.0  SLIPPAGE_PTS=1.0
ONLY=range_fade,hedged,W-ovn       # comma tokens; run only configs whose name contains all
```

Outputs land in `results/`: `mz_comparison.csv` (sorted leaderboard), `mz_summary.json`
(per-config FAITHFUL+REALISTIC metrics + skip counts), `mz_trades_<config>.csv` (every trade),
`mz_equity_<config>.html` (Plotly equity / drawdown / per-trade P&L).

## Files

- `zones.py` — the indicator math (ported 1:1, unit-tested against `compute_zones(100,0)`).
- `data_loader.py` — read-only DuckDB reader (adapted from `survivor_backtest_historify.py` /
  `cpr_option_selling`): spot D/W/M bars, 1-min spot, as-of option price (no look-ahead), per-expiry
  lot size, expiry resolution.
- `costs.py` — generalised per-leg Indian charge model + slippage.
- `engine.py` — parametrized engine; the "unit" abstraction (one credit position managed as a whole)
  unifies all four entry×structure combos through one exit path.
- `run_all.py` — driver: runs the 12-config matrix, reports, writes results.

## Caveats / assumptions

- **Window**: the strike-resolved option catalog (`expired_fno_contracts`) covers **2024-03-28 →
  2026-06-09 (~2.2 yrs)**. Spot history goes back further but option backtesting is limited to this.
- **No look-ahead**: every option fill is at-or-before the bar; entries fill at/after the entry time.
- **Margin is report-only** — fixed lots drive sizing; ₹1 Cr is the ROI base, not a lot-count constraint.
  (`naked` margin is far higher per lot than `hedged` — compare ROI with that in mind.)
- Uses the **fixed-strike** resolved option data (`market_data`/`NFO`), not the rolling-ATM
  `expired_options_rolling` table.
- The outer zones (R2/S2 at ±0.786×range) sit beyond the prior candle's range, so shorts are far
  OTM: high win rate, small credits. Defined-risk losses on `hedged` are capped near
  `HEDGE_WIDTH − credit` points.
- Research backtest only — no app/runtime code is touched.
```
