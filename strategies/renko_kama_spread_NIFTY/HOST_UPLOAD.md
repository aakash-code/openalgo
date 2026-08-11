# Upload Guide: renko_kama_spread

## Validation

```
Validation: strategies/renko_kama_spread_NIFTY/strategy.py
  [PASS]  SIGTERM handler installed (line 479)
  [PASS]  SIGINT handler installed (line 480)
  [PASS]  stop_event (threading.Event) used for interruptible waits
  [PASS]  HOST_SERVER priority correct (line 88)
  [PASS]  Reads OPENALGO_STRATEGY_EXCHANGE (line 56)
  [PASS]  stdout-only logging, no FileHandler
  [PASS]  if __name__ == "__main__" entry present
  [PASS]  Mode dispatcher honors env MODE, defaults to "live"
  [PASS]  No hardcoded local paths
  [PASS]  STRATEGY_NAME read from env with default
  ALL REQUIRED CHECKS PASSED
```

Options strategies in this pack are execution-only: `--mode backtest` exits immediately
with a message pointing at the separate DuckDB backtest harness
(`backtesting/renko_kama_hedged_spread/renko_kama_spread_backtest.py`) where the signal
was already validated over 90 days of historical NIFTY + option data.

## Before you upload

1. **Turn on Analyzer Mode** at `http://localhost:5000/analyzer` - this is what makes
   today's run a dry run. It intercepts every order server-side before it reaches the
   broker, regardless of anything in the strategy file itself.
2. **Confirm `EXPIRY_DATE`** in the strategy file (currently `04AUG26`, the nearest NIFTY
   weekly as of today). This is never auto-rolled - update it yourself each week.
3. **Confirm the market is open** - the strategy checks `SESSION_END` (15:29 IST) and
   will refuse to do anything once the session has passed for the day.

## On http://localhost:5000/python

### Step 1: Click "Add Strategy"

| Form field | Value |
|---|---|
| Name | renko_kama_spread |
| File | `strategies/renko_kama_spread_NIFTY/strategy.py` |
| Exchange | NSE_INDEX (underlying is NIFTY spot; the options themselves are NFO, resolved automatically per leg) |

### Step 2: Parameters (key=value, one per row)

| Key | Value | Purpose |
|---|---|---|
| MODE | live | required - backtest mode is disabled for this file |
| EXPIRY_DATE | 04AUG26 | nearest NIFTY weekly expiry - **update this every week** |
| EXIT_MODE | eod | `eod` (default, validated) force-closes daily at 15:24. `carry` lets a position ride across days/weekends for extra theta decay (backtested net P&L ~2.5x higher over 90 days, but larger max drawdown and real weekend gap exposure the backtest can't fully price) - still force-closes on/after the position's own contract expiry day. See caveats below before using `carry`. |
| LOTS | 5 | position size (qty = 5 x live lot size, read from the option chain at startup) |
| BRICK_SIZE | 15 | renko brick size in points, as validated in the backtest |
| CHOP_THRESHOLD | 38.2 | only trade when Choppiness Index < this (validated cutoff) |
| LOG_LEVEL | INFO | logging verbosity |

### Step 3: Schedule

| Field | Value |
|---|---|
| Start time | 09:15 IST |
| Stop time | 15:30 IST |
| Days | Mon, Tue, Wed, Thu, Fri |

### Step 4: Click Upload, then Start

Logs stream to `logs/strategies/<id>_<timestamp>.log`. Watch for:
- `Warm-up: N 5m bars` - confirms history fetch succeeded before trading begins
- `New 5m bar ... chop=... trend=...` - one line per completed 5-minute bar
- `Opening PE/CE spread` / `Closing ... spread` - entries and exits
- `Fills: hedge(...) @ ... | short(...) @ ...` - confirms both legs actually filled (or timed out - check for `None`)

To stop, click Stop in the UI - the host sends SIGTERM.
- In **EXIT_MODE=eod** (default): the strategy closes any open position and cancels
  dangling orders before exiting (up to 15s grace period) - same as day 1.
- In **EXIT_MODE=carry**: stopping does NOT close an open position - it leaves it open
  and the position state is saved to `strategies/renko_kama_spread_NIFTY/state.json` so
  the next run (e.g. the host's next scheduled start) resumes tracking it instead of
  losing track of it. Only use Stop in carry mode when you actually want to leave the
  position open unmanaged - if you want to flatten immediately, do it manually via the
  broker/orderbook UI, then delete `state.json` before restarting the strategy.

## EXIT_MODE=carry caveats (read before switching from the default)

- The `/python` host restarts this process daily per your schedule (Step 3 below) - an
  in-memory-only position would vanish on every restart. This file now persists the open
  position to `state.json` on every entry/exit and reloads it on startup specifically so
  carry mode survives that restart cycle. `EXIT_MODE=eod` doesn't need this (it always
  flattens before the host's stop time), so `state.json` should normally stay empty/absent
  in eod mode - if you ever see a warning log about "resuming a position" while running in
  eod mode, that indicates a real problem (the file force-closes it immediately as a
  safety net, but investigate why it happened).
- Backtested over 90 days, carry mode produced ~2.5x the net P&L of eod mode with a
  similar win rate - the mechanism is capturing extra theta/time decay from holding
  through non-trading hours (especially weekends), not a better directional read (see
  `backtesting/renko_kama_hedged_spread/analyze_trades.py` output). It also had a
  somewhat larger max drawdown, and the backtest still only uses each option's last
  traded price across the gap - it does not model a genuine weekend news-shock scenario,
  so real tail risk from holding short options through a weekend is probably understated.
- If you want to reset carry-mode state manually: stop the strategy, manually flatten the
  position via the broker/orderbook if one is open, then `rm strategies/renko_kama_spread_NIFTY/state.json`
  before starting again.

## What this strategy does NOT do

- No stop-loss or target on the spread - exit is trend-flip or square-off only (daily at
  15:24 IST in eod mode, or at the position's own contract expiry in carry mode),
  matching exactly what was backtested for each mode. Do not add SL logic without
  re-validating in the backtest harness first (untested risk logic could change behavior
  in ways that haven't been measured).
- No auto-roll of `EXPIRY_DATE` - you must update it manually each week before the old
  expiry lapses, in both modes.

## Notes

- Live vs sandbox is decided entirely by OpenAlgo's Analyzer Mode toggle, not by
  anything in this file - the strategy just places normal orders and trusts the
  server-side toggle to intercept them when in analyzer mode.
- Re-upload anytime; the host preserves `STRATEGY_ID`.
- If you want a different position size mid-week, change `LOTS` in the parameters and
  restart the strategy - no code change needed.
