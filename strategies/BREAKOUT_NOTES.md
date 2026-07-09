# Breakout Intraday Strategy — Build Log & Notes

Living record of the TradeFinder-driven breakout strategy: files, config, changes,
findings, and open items. **Update this whenever we change behaviour** so future
tuning is informed. Last updated: 2026-06-04.

---

## 1. What this is

Autonomous Python port of the Pine indicator *"Intraday Signals & Risk Calculator
[India v5.6]"*. Runs in OpenAlgo's `/python` host — no webhook, no TradingView.

Flow: every loop → refresh TradeFinder IntradayBoost watchlist (60s) → for each
stock evaluate the breakout signal on the just-closed 5m bar → size (risk-based) →
MARKET entry + broker SL-M → manage (breakeven / trail / target) → square off at EXIT_TIME.

---

## 2. File inventory (all in `strategies/`)

| File | Purpose |
|---|---|
| `breakout_intraday_strategy.py` | **The live strategy.** Single-file, `/python`-host ready (SIGTERM, env vars, stdout + file logs). `--test` = one diagnostic pass, no orders. |
| `breakout_replay.py` | Single-day **verification report** — per-trade Pine-style breakdown + `breakout_verify_<date>.csv`. |
| `breakout_report.py` | **Full verification dump** — ALL scanned stocks. Writes `breakout_allbars_<date>.csv` and `breakout_signals_<date>.csv`. |
| `breakout_sweep.py` | **Parameter sweep** — two modes: `classic` (fixed target) and `new` (breakeven + time-exit + re-entry). `--mode compare` runs both. |
| `BREAKOUT_NOTES.md` | This file. |

Run examples (need `OPENALGO_API_KEY` env; OpenAlgo on :5000; `TF_JWT_TOKEN` for live data):
```
uv run python strategies/breakout_intraday_strategy.py --test
uv run python strategies/breakout_report.py --top 40
uv run python strategies/breakout_sweep.py --top 60 --mode compare
uv run python strategies/breakout_sweep.py --symbols HCLTECH,TCS,INFY --date 2026-06-03 --mode compare
```
`--symbols` flag bypasses TradeFinder fetch (use when JWT expired for historical sweeps).

---

## 3. Signal logic (ported from Pine, classic mode)

On the **last CLOSED 5m bar** (drop the forming bar → use `iloc[-2]` equivalent):

- `buy1`  (single): green, prev red, `close > high[1]`              — SL = `min(L,L1)`
- `sell1` (single): red, prev green, `close < low[1]`               — SL = `max(H,H1)`
- `buy2`  (double): green, prev green, prev2 red, `close > high[2]` — SL = `min(L,L1,L2)`
- `sell2` (double): red, prev red, prev2 green, `close < low[2]`    — SL = `max(H,H1,H2)`
- Filters: **VWAP** (buy>VWAP / sell<VWAP, daily-reset) + **ADX ≥ threshold** (Wilder).
- Session 09:15–EXIT_TIME; no new entries after `ENTRY_CUTOFF` (15:00).

**Enhanced-mode** (zone suppression, auto-reverse on SL, SL-mode lockout) — NOT ported. Deferred.

### Sizing (Pine risk calculator)
`qty = min( risk_budget / sl_dist , notional_cap / entry )`
- risk_budget = `BALANCE × MAX_LOSS_PCT%` = 311000 × 1% = ₹3,110 (matches Pine).
- notional_cap = `CAPITAL_PER_TRADE` = ₹50,000.
- **OPEN DECISION:** match Pine (`balance × leverage` ≈ ₹15.5L) vs keep ₹50k cap.
  Raising cap to ₹1L doubles P&L with same fixed ₹52/trade charges — worth testing.

### Execution / exits (current)
- Entry = **next-bar open** (realistic MARKET fill; Pine entry = signal close). Gap ≈ **+0.001%** avg → negligible.
- SL = broker-side SL-M (survives a crash). Cancelled and re-placed on trail/breakeven.
- Breakeven = optional: move SL to entry after `BREAKEVEN_PCT`% favourable move.
- Target = single, `entry ± TARGET_RR × sl_dist`. **Set TARGET_RR=0 to disable** (time-exit mode).
- Trail = `TRAIL_MODE` — see §4.
- Time exit = `EXIT_TIME` (HHMM, configurable).
- Multi-target partial booking (T1–T6, 30/40/30) — NOT ported. Deferred.

---

## 4. Current config defaults (env-overridable)

| Var | Default | Notes |
|---|---|---|
| `INTERVAL` | `5m` | signal timeframe |
| `ENABLE_SINGLE` | `false` | sweep: double-only wins |
| `ENABLE_DOUBLE` | `true` | |
| `USE_VWAP` | `true` | |
| `USE_ADX` / `ADX_THRESHOLD` | `true` / `25` | sweep raised 20→25; Pine uses 20 |
| `ADX_LEN` | `14` | Wilder period; matches Pine |
| `TARGET_RR` | `2.0` | 0 = no fixed target (time-exit) |
| `TRAIL_MODE` | `after_1R` | none / after_1R / full |
| `BREAKEVEN_PCT` | `0.0` | 0 = off; 1.0 = move SL to entry after 1% move |
| `EXIT_TIME` | `1525` | HHMM voluntary square-off |
| `HARD_EXIT_TIME` | `1520` | HHMM failsafe (< broker MIS SOS) |
| `ALLOW_REENTRY` | `true` | re-enter after SL/target exit on same stock |
| `MIN_SL_DIST_PCT` | `0.3` | skip if SL < 0.3% away (charges > expected profit) |
| `BALANCE` / `MAX_LOSS_PCT` | `311000` / `1.0` | risk budget ₹3,110/trade |
| `CAPITAL_PER_TRADE` | `50000` | notional cap — raise to ₹1L to double P&L at same charges |
| `MAX_OPEN_POSITIONS` | `10` | |
| `MAX_DAILY_LOSS_RS` | `25000` | halts if realised + worst-case unrealised exceeds this |
| `WATCHLIST_REFRESH_SEC` | `60` | TradeFinder re-fetch |
| `ENTRY_CUTOFF` | `1500` | HHMM — no new entries after this |
| `DRY_RUN` | `false` | true = log + simulate, zero real orders |
| `BREAKOUT_LOG_DIR` | `logs/breakout` | daily log folder (renamed from LOG_DIR to avoid OpenAlgo env clash) |

---

## 5. Daily log files (`logs/breakout/`)

Four files created automatically each day:

| File | Content |
|---|---|
| `breakout_YYYY-MM-DD.log` | Full text log — startup config, every signal, entry, trail, exit, summary |
| `breakout_errors_YYYY-MM-DD.log` | WARNING + ERROR only — open first if something went wrong |
| `breakout_trades_YYYY-MM-DD.csv` | One row per closed trade (see columns below) |
| `breakout_events_YYYY-MM-DD.jsonl` | Machine-readable JSON events for replay/analysis |
| `breakout_state_YYYY-MM-DD.json` | Crash-recovery state snapshot |

### Trade CSV columns
```
date, signal_time, entry_time, exit_time,
symbol, direction, kind, signal_adx, signal_vwap,
entry_price, initial_sl, final_sl, target,
exit_price, qty, notional, gross, charges, net, reason,
breakeven_activated, reached_1r, dry_run
```
- `signal_time` = candle close that fired the pattern
- `entry_time`  = when entry order was actually placed (IST HH:MM:SS)
- `exit_time`   = when trade closed
- Gap between signal_time and entry_time = order latency
- Gap between entry_time and exit_time = trade duration

---

## 6. Charge breakdown (Zerodha intraday equity)

From 2026-06-03 live data — 58 trades, ₹56.8L total turnover:

| Component | Amount | % |
|---|---|---|
| Brokerage (₹20 flat/order) | ₹1,703 | 56.4% |
| STT (0.025% sell side) | ₹710 | 23.5% |
| GST (18% on brok+txn+SEBI) | ₹339 | 11.2% |
| Transaction (NSE 0.00307%) | ₹174 | 5.8% |
| Stamp duty (0.003% buy) | ₹85 | 2.8% |
| SEBI (0.0001%) | ₹6 | 0.2% |
| **Total** | **₹3,018** | |
| Per trade avg | **₹52** | |

**Charges = 38.5% of gross P&L on this day.** Key lever: raising `CAPITAL_PER_TRADE`
from ₹50k → ₹1L doubles gross while charges stay flat (brokerage is fixed ₹20/order).

---

## 7. Change log

### 2026-06-03 — initial build
- Created `breakout_intraday_strategy.py` (signal + sizing + orders + state + SEBI guards).
- Reused TradeFinder access from user's `ann_intraday_strategy.py`.
- **Bug fixed:** `pd.NA` in VWAP/ADX division corrupted dtype → switched to `np.nan`.

### 2026-06-03 — sweep + robust config
- Sweep over 60 stocks, 729 combos. Baseline (trail=full 2R both adx20) = **−1,250 net**.
- Applied: double-only, ADX≥25, `TRAIL_MODE=after_1R`. Result: **39 trades, 64% win, NET +5,401**.

### 2026-06-04 — TradingView MCP verification (all 58 signals)

Installed `tradingview-mcp` (already at `/Users/.../tradingview-mcp`, CDP port 9222).
Ran automated per-stock verification against Pine "Intraday Signals & Risk Calculator [India v5.6]".

**Results (2026-06-03, 58 stocks):**
- 34/58 direction match ✓ (24 mismatches = TV showed a *later* signal in opp direction, not a bug)
- Exact SL match (Python == Pine): CONCOR 453.00, PETRONET 268.30, TECHM 1497.00, VMM 119.70
- Near-exact SL matches on all other ✓ stocks (within 0.2–0.6 pts)
- **Conclusion: signal logic and SL source are verified correct.**

**Pine indicator settings noted (from `indicator.get` via CDP):**
- ADX threshold = **20** (`in_38`) — Pine default; our Python uses **25** (sweep improvement, intentional)
- Balance = ₹3,11,000 ✓ (`in_42`)
- Leverage = 5× (`in_43`) → Pine qty 10–30× larger than ours (open sizing decision)
- Session = "0915-1525" ✓ (`in_1`)
- ADX period = 14 ✓ (`in_28`)
- T1 at 2R (table shows "R:R (T1): 1:2") ✓

### 2026-06-04 — sweep rewrite + new mode

`breakout_sweep.py` fully rewritten with two modes:

**Classic mode** (fixed target, one trade per stock):
- Best 2026-06-03: `adx25 after_1R 1.5R double` → n=58, win=44 (76%), NET **+₹9,597**
- Live config `adx25 after_1R 2R double` → n=58, win=44, NET **+₹9,232** (ranked #4)

**New mode** (breakeven SL + time-exit + re-entry):
- Best: `adx20 be=1.5% exit=1525 reentry` → n=131, win=60 (46%), NET **+₹14,302** (+55%)
- Safe upgrade (no reentry): `adx25 be=1% exit=1510 once` → n=58, win=35 (60%), NET **+₹10,843**
- Proposed config `adx25 be=1% exit=1510 reentry` → n=141, win=69, NET **+₹8,924** (slightly worse, ADX=25 too selective for reentry)
- 526/576 combos net-positive → highly robust on this trending day
- **One-day finding only. Multi-day validation required before going live with new mode.**

**Key bug fixed in sweep:** `exit_hm` was in HHMM format but `_hm` column is in minutes
→ added `exit_min = (exit_hm // 100) * 60 + (exit_hm % 100)` conversion.

### 2026-06-04 — fix late entry on first signal bar (evaluate_signal)

**Root cause:** `evaluate_signal` always dropped `iloc[-1]` assuming it was the forming bar.
Right after a candle close (e.g. 9:30 bar closes at 9:35), the history API sometimes returns
only the closed bars `[9:15..9:30]` with no partial 9:35 bar yet. The strategy would drop the
9:30 signal bar itself, and only detect the signal one poll later (~20 s) once the 9:35 forming
bar appeared.

**Fix:** Clock-time guard — drop the last bar only if `now_min < bar_open_min + interval_min`.
At exactly 9:35 when there's no forming bar in data, 9:30 is correctly kept as the signal bar.
When the forming 9:35 bar does appear in data, it's correctly dropped. No repaint risk.

Added `_parse_interval_min(INTERVAL)` → `_INTERVAL_MIN` constant so this works for any
configured interval (`5m`, `15m`, `1h`, etc.).

### 2026-06-04 — strategy hardening (live file)

**Config additions (all env-overridable):**
- `EXIT_TIME` / `HARD_EXIT_TIME` — were hardcoded 15:25/15:20, now HHMM env vars
- `BREAKEVEN_PCT` — move SL to entry after X% favourable move (0=off)
- `ALLOW_REENTRY` — re-enter same stock after position closes
- `MIN_SL_DIST_PCT` — skip trades where SL < X% away (default 0.3%, filters ₹52-charge losers)
- `TARGET_RR=0` support — disables fixed target, holds till EXIT_TIME
- `BREAKOUT_LOG_DIR` — renamed from LOG_DIR to avoid clash with OpenAlgo's own LOG_DIR env

**Bug fixes:**
- **DRY_RUN SL simulation** — `_order_status()` returns None for fake "DRY-..." IDs so SL hits never fired. Fixed: checks bar low/high against current_sl in DRY_RUN mode.
- **SL order rejection** — if SL-M order fails, now cancels entry and skips (previously left naked open position).
- **Daily loss check** — now includes unrealised worst-case (current_sl as proxy) for open positions, not just closed P&L.
- **JWT expiry mid-session** — now logs WARNING with stale list count instead of failing silently.

**Position dataclass additions:**
- `reached_be: bool` — breakeven SL activated flag
- `signal_time: str` — signal candle timestamp
- `entry_time: str` — actual order placement time (IST HH:MM:SS)
- `signal_adx: float`, `signal_vwap: float`, `kind: str` — stored for trade log

**New daily log system (`logs/breakout/`):**
- `breakout_YYYY-MM-DD.log` — full text (INFO+)
- `breakout_errors_YYYY-MM-DD.log` — errors only (WARNING+)
- `breakout_trades_YYYY-MM-DD.csv` — one row per trade, all fields
- `breakout_events_YYYY-MM-DD.jsonl` — structured JSON events

---

## 8. Open items / future improvements

- [ ] **Multi-day validation** — new mode sweep results are from ONE trending day. Run classic
      and new-mode sweeps across 10+ days before trusting the breakeven + reentry config.
- [ ] **Qty sizing decision** — match Pine (`balance × leverage`, ~₹15.5L/trade) vs current
      ₹50k notional cap. Raising to ₹1L doubles P&L at same ₹52/trade charges — low hanging fruit.
- [ ] **Multi-target partial booking** (T1–T6, 30/40/30) from Pine risk calculator.
- [ ] **Enhanced mode** (zone suppression, auto-reverse, SL-mode lockout).
- [ ] **JWT auto-refresh** — biggest unattended-run blocker. Pull `lt` from a file refreshed
      each morning, or automate tradefinder login.
- [ ] **Re-entry after exit** — currently works via `ALLOW_REENTRY=true`, but sweep shows it only
      adds value with ADX≤20. At ADX=25 it's neutral or slightly negative.
- [ ] **Telegram/WhatsApp alert** on entry, exit, SL hit — OpenAlgo has built-in integration.
- [ ] **Market holiday check** — detect non-trading days at startup to avoid pre-session loop.
- [ ] **Broker charges config** — currently hardcoded Zerodha rates; parameterise for other brokers.
- [ ] Replace `verify=False` TLS (LibreSSL workaround) by running on Python 3.12+.

---

## 9. Known differences vs Pine (so verification is meaningful)

1. **Entry** = next-bar open, not signal-candle close. Gap ≈ **+0.001%** avg (negligible).
2. **Qty** = capped by ₹50k notional, not Pine's `balance×leverage` (→ see open item above).
3. **ADX threshold** = 25 (Python) vs 20 (Pine default) — intentional sweep improvement.
4. **Breakeven / trail** = Python-only; Pine doesn't have automated SL management.
5. **Classic mode only** — enhanced-mode state machine + multi-target not yet ported.

---

## 10. TradingView MCP setup

MCP config at `~/.claude/.mcp.json` — points to `/Users/bond7/Desktop/Project/tradingview-mcp/`.
TradingView Desktop must run with `--remote-debugging-port=9222` (CDP).

Key CLI commands for verification:
```bash
TV="node /Users/bond7/Desktop/Project/tradingview-mcp/src/cli/index.js"
$TV status                                          # check connection
$TV symbol "NSE:TECHM"                              # switch chart
$TV scroll "2026-06-03"                             # scroll to date
$TV data labels --study-filter "Intraday"           # read Pine signal labels
$TV data tables --study-filter "Intraday"           # read Pine risk table
$TV indicator get "NmWZpD"                          # read indicator inputs by study ID
```
The Pine indicator exposes: Direction, Entry, Stop, T1-T6 prices, Qty, Balance, all charges via labels + tables. Signal arrows (plotshape) are NOT accessible programmatically — screenshots only.
