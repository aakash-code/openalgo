# EMA-Crossover Basket — TraderFinder Sector Scope

Dual-mode (`backtest` / `live`) single-file strategy for OpenAlgo. Trades a
**basket** of stocks whose universe comes from your **TraderFinder Sector Scope**
filter. EMA crossover entries, end-of-candle MARKET execution, **ATR trailing stop**.

## Run

```bash
# Backtest the whole basket (per-symbol legs, aggregated report)
uv run python strategies/ema_basket_traderfinder/strategy.py --mode backtest

# Live (sandbox vs real is decided by OpenAlgo's UI analyzer toggle)
uv run python strategies/ema_basket_traderfinder/strategy.py --mode live
```

Backtest output (per-symbol + basket summary) is written to
`backtests/ema_basket_traderfinder/basket_summary.csv`.

## TradeFinder Sector Scope universe

The universe reuses the project's **existing authenticated fetcher** —
`strategies/scan_today_signals.py :: fetch_sector_scope()` — which handles the
JWT + TOTP auth against `tradefinder.in/api_be` and returns
`{SECTOR: {symbol: {ltp, change_pct, r_factor}}}`. No new API wiring needed.

**Auth:** Sector Scope needs a valid JWT. Same as the other scanners:
write a fresh token into `strategies/tf_jwt.txt` (or set `TF_JWT_TOKEN`).
An expired token returns `UNAUTHORISED`, and the strategy then uses the static
fallback below (so a screener outage never hard-blocks trading).

**Optional filters** (defaults take every symbol, matching the base scanner):

| Env var | Default | Meaning |
| --- | --- | --- |
| `TF_MIN_R_FACTOR` | none | keep only stocks with `r_factor >=` this (e.g. `0` = strong/long-biased only) |
| `TF_SECTORS` | all | comma whitelist of sectors, e.g. `BANK,IT` |
| `TF_TOP_N` | all | keep only the top N symbols ranked by `r_factor` |
| `TF_SYMBOLS` | RELIANCE,TCS,INFY,HDFCBANK,ICICIBANK | static **fallback** list when the fetcher is unavailable |

Because Sector Scope's `r_factor` encodes relative strength, `TF_MIN_R_FACTOR=0`
+ `TF_TOP_N=10` gives you "the 10 strongest stocks across the scanned sectors,"
which pairs naturally with long-only EMA-crossover momentum entries.

## Key tunables (all env-overridable)

| Var | Default | Meaning |
| --- | --- | --- |
| `FAST_EMA` / `SLOW_EMA` | 9 / 21 | crossover periods |
| `ATR_PERIOD` | 14 | ATR lookback |
| `ATR_SL_MULT` | 2.0 | initial hard stop = entry − 2×ATR |
| `ATR_TRAIL_MULT` | 2.5 | trailing distance = 2.5×ATR from the peak |
| `RISK_PER_TRADE` | 0.005 | 0.5% of cash risked per trade |
| `PER_SYMBOL_CAP_PCT` | 0.18 | ≤18% of cash per stock |
| `MAX_OPEN_POSITIONS` | 5 | max concurrent stocks |
| `INTERVAL` | 15m | bar timeframe |
| `PRODUCT` | MIS | MIS intraday / CNC delivery |

## How the ATR trailing stop works

- **Live:** every bar close, the trailing distance is recomputed from the latest
  ATR (`ATR_TRAIL_MULT × ATR / price`) and stored on the open position. The LTP
  WebSocket feed updates a per-symbol high-watermark on each tick; when price falls
  `trail` below the peak (or hits the initial `entry − ATR_SL_MULT×ATR` hard stop),
  a worker thread flattens via `placesmartorder(position_size=0)`. The WS callback
  never blocks on the broker.
- **Backtest:** VectorBT runs the same idea with `sl_stop = ATR_TRAIL_MULT×ATR/price`
  (a per-bar array) and `sl_trail=True`. Note the live leg adds a *tighter* initial
  hard stop (`ATR_SL_MULT`) that the backtest approximates with the trailing stop
  from entry — so live is marginally more conservative on fresh entries.

## Caveats

- Long-only (no shorts). Exits = opposite EMA crossover **or** ATR stop **or**
  optional `TIME_EXIT_MIN`.
- On restart, open broker positions are **not** auto-reconciled into memory (this
  file keeps state in-process). For restart-safe state across crashes, say so and
  I'll add the SQLite `state.db` layer (the framework's `core/state.py` pattern).
- Not yet bundled/validated for `/python` self-hosted upload — run `/algo-host` (or
  ask) to produce the upload guide.
