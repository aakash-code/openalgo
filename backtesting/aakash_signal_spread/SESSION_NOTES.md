# Session Notes — Aakash 3m Signal → Delta Credit Spread Strategy

**Date of session:** 2026-06-13
**Author/owner:** Aakash (user) + Claude
**Status:** Pipeline built & validated on May 2026. NOT yet production-tested.
Slippage + full-history run still pending (see TODO.md).

---

## 1. What this strategy is

An **intraday, directional, defined-risk options strategy** for NIFTY:

1. A TradingView Pine v6 indicator ("Intraday BUY/SELL & AUTO SL by Aakash")
   generates BUY/SELL signals on **3-minute** NIFTY candles.
2. Each signal is traded as a **delta-targeted credit spread** on the nearest
   weekly expiry:
   - **BUY signal  → Bull Put Spread**  (sell ~0.45Δ PE, buy ~0.20Δ PE)
   - **SELL signal → Bear Call Spread** (sell ~0.45Δ CE, buy ~0.20Δ CE)
3. Defined risk = small margin (~₹13–15k/spread vs ~₹1.3L naked). The buy leg
   caps the tail; the sell leg harvests premium in the signal's direction.
4. Positions flip on the indicator's own SL logic (the next opposite signal
   closes the current spread and opens the reverse). Last position squared off
   at session end (15:24).

The user's core motivation: "as a human I can't tell when the market will
trend vs chop, so automate the entry." The signal engine makes that decision.

---

## 2. The signal engine (the part the user confirmed is correct)

We ported the Pine indicator bar-by-bar to Python. The **canonical version we
trade is "filter OFF, full state machine"** — the user explicitly confirmed
this 5-signal/day shape is right.

**Signal rules (pure OHLC, 100% faithful):**
- `buy1` : green now, red prev, close > high[1]
- `sell1`: red now, green prev, close < low[1]
- `buy2` : green now, green prev, red prev2, close > high[2]   (double)
- `sell2`: red now, red prev, green prev2, close < low[2]       (double)

**Stateful behaviour (also ported):**
- Active-line block: no new same-side signal while a line is unbroken
  (uniqueSignal=false path).
- Line break = SL hit → deactivate + forced opposite signal on that bar.
- `slModeOnly` latch: after the first SL hit in the day, normal single/double
  signals are disabled — only forced-opposite (SL-flip) signals continue.
- Zone suppression (enhancedMode) between entry and SL.

**Trend filter is INTENTIONALLY OFF** in the traded version. Reason: the Pine
filter uses `ta.vwap`, and **NIFTY index volume = 0 in our DuckDB**, so a true
volume-weighted VWAP is impossible here. We validated raw patterns (exact) and
chose the filter-off state machine as the engine. (See TODO: real VWAP needs
futures volume if we ever want the filter back.)

Reference original Pine source is saved alongside as `aakash_indicator.pine`.

---

## 3. Files

| File | Purpose |
| --- | --- |
| `aakash_signal_3m_replay.py` | Faithful bar-by-bar port of the indicator. Layer-1 = raw pattern candidates; Layer-2 = full state machine. Reusable `load_3m()` + `replay()`. |
| `aakash_options_spread_test.py` | Drives the signal engine → builds delta-targeted credit spreads. Black-Scholes IV/delta solved from each option's own 1m price. Single-day run. |
| `expiry_day_0dte_backtest.py` | Separate earlier exploration: 0DTE short-premium 2×2 (iron fly/naked × filter/no-filter). Kept for reference; NOT the main strategy. |
| `aakash_indicator.pine` | The original TradingView Pine v6 source (ground truth). |
| `results/` | All CSV outputs (signals + spread trade logs). |

Run examples:
```bash
# Signals for one day (validation, both layers)
DAY=2026-06-12 uv run python aakash_signal_3m_replay.py

# Signal → spread test for one day
DAY=2026-05-22 uv run python aakash_options_spread_test.py
```
Env knobs (spread test): `DAY`, `LOTS`, `SHORT_DELTA`, `LONG_DELTA`, `RATE`.

---

## 4. Results so far (per 1 lot, NET of charges, NO slippage)

### June 12, 2026 — signal validation only (no option data for June in DB)
- Layer-1 raw patterns: 30 candidates (user validated against chart = correct).
- Filter-off state machine: 5 signals (09:39 SELL → flips). User: "this is right."
- Option legs NOT testable on June 12 (no June 2026 option data; weekly not yet
  expired so not downloadable as "expired").

### May 2026 — full month, 17 trading days (signal → spread, frictionless)
| Metric | Value |
| --- | --- |
| **Net P&L** | **+₹50,282 / lot** |
| Spreads | 104 (~6/day) |
| Win rate | 59.6% (62/104) |
| Up days / Down days | 15 / 2 |
| Worst day | −₹886 |
| Max drawdown (daily) | −₹886 |
| Best day | +₹6,952 (May 19, 0DTE) |

Clean equity curve, almost straight up.

---

## 5. Honest caveats (READ before believing the number)

1. **NO SLIPPAGE modeled.** Fills are at 1m close. 104 spreads = 416 option
   fills, many OTM/0DTE legs with real bid-ask. Sensitivity:
   - 0.5 pt/leg/side → −₹13.5k → net **+₹36.8k**
   - 1.0 pt/leg/side → −₹27k → net **+₹23.2k**
   - 2.0 pt/leg/side → −₹54k → net **−₹3.8k**
   Slippage is decisive. This is the #1 thing to fix before trusting results.
2. **May 2026 was a favorable, trending month** (15 up / 2 down is not normal).
   The P&L is driven by end-of-day held positions riding trends. A choppy month
   (cf. single day May 22 = −₹61, lots of flips) would churn costs and bleed.
3. **One month ≠ edge.** Need the full Oct-2024 → May-2026 history.
4. **VWAP/trend filter disabled** (volume=0 limitation). If the live TradingView
   chart uses the filter, live signals will differ from this engine.
5. **Data coverage:** option 1m data in DB spans ~Oct 2024 → 2026-05-26. No June
   2026 options.

---

## 5b. UPDATE (2026-06-13 cont.) — slippage model + multi-day backtest

**Data coverage corrected:** NIFTY option 1m data actually spans **2021-04-29 →
2026-06-09** (~5 years, ~19,600 contracts), NOT just Oct-2024+. User downloaded
through the June 9 expiry. (June 12 still needs the June 16 expiry, not yet
expired.) BANKNIFTY/BANKEX download jobs ran but data did NOT land — would need
re-download if wanted.

**New engine:** `aakash_spread_backtest.py` — multi-day, charges-only (NET of
brokerage + STT + txn + SEBI + GST + stamp), + monthly table + equity/drawdown.
Primary backtest tool. NOTE: slippage was added then **removed at the user's
request** (2026-06-13) — fills are now at the 1m mid/close. Numbers below are
frictionless; a 1pt/leg slippage run earlier showed this is highly fill-
sensitive (2026 YTD was +₹198k charges-only vs +₹45k at 1pt slippage).

**2026 YTD result (Jan 1–Jun 9, charges-only, per 1 lot):**
- Net **+₹198,157** | 106 days (89 up / 17 down) | 589 spreads | win 47.9%
- Max DD **−₹4,008** | best day +7,369 | worst −2,245
- Monthly: Jan +31,966 · Feb +23,297 · Mar +40,026 · Apr +41,656 · May +51,439 · Jun(7d) +9,773

**Profile = trend-follower** (defined-risk spread caps losses, winners run).
Every 2026 month positive on charges-only fills.

**Full "5-year" run (2021→2026, charges-only) — DONE, key finding:**
The strategy only TRADES from **2024-08-23 → 2026-06-09 (23 months)**. Everything
before Aug-2024 = 0 spreads because (a) the expiry/lot metadata
(`expired_fno_contracts`) only covers Aug-2024+, and (b) 2021-2023 NIFTY option
bars are too sparse (~370-540 contracts/yr = monthly near-ATM only, not weekly
chains). **No pre-2024 regime test is possible with current data.**

Real 23-month results (charges-only, 1 lot):
- Net **+₹779,473** | 443 traded days (79% up) | 2,340 spreads
- Trade win 46% | **payoff 3.15×** (avg win +1,146 / avg loss −364)
- **23/23 months positive** (worst Feb-2025 = +₹5 breakeven) | max DD −₹24,671
- Avg +₹33,890/mo → annualized ~₹406,681/lot
- By year: 2024(5mo) +₹53,983 · 2025(12mo) +₹527,333 · 2026(5mo) +₹198,157

Caveats: (1) 23 months only, all in a trending/favorable NIFTY regime — no
choppy/bear out-of-sample. (2) Charges-only/frictionless — 1pt slippage cut the
2026 slice ~77% (+198k→+45k), so realistic fills would cut ₹779k a lot.
Files: `results/full_run_2021_2026.log`, `results/backtest_*_2021-04-29_2026-06-09.csv`.

## 5c. Capital / margin model + BUY-FIRST execution rule (2026-06-13)

`aakash_spread_backtest.py` now prints a CAPITAL & MARGIN block. Env knobs:
`CAPITAL` (def 300000), `MARGIN_PER_LOT` (def 40000 hedged), `NAKED_PER_LOT`
(def 130000). Assumes one spread open at a time (strategy flips), buy-first.

**5 lots / ₹3L / last 6 months (Dec-2025→Jun-2026):** margin ₹2.0L (67% util,
max 7 lots fundable), return-on-capital ~433% (frictionless), max DD −₹11,410
(4%). Verdict: 5 lots FITS with buffer. BUT full-period 5-lot DD ≈ −₹1.2L (40%
of ₹3L) — last 6mo was benign.

**⚠️ BUY-FIRST EXECUTION RULE (do-or-die for live):** the SELL ~0.45Δ leg is
naked until its hedge is in → ~₹1.3L/lot margin (₹6.5L at 5 lots > ₹3L →
broker REJECTION / forced square-off). Live `/python` strategy MUST place the
~0.20Δ BUY (hedge) leg FIRST, confirm fill, THEN send the SELL — every entry AND
every SL-flip. This is a hard requirement, not a preference.

## 5d. ⚠️ CRITICAL (2026-06-13): look-ahead bias WAS the entire edge

Fixed a look-ahead bug: the engine computed the signal on the closed 3m candle
(correct) but FILLED the trade at the candle's *start* price (it priced options
at the bar's left-label time, e.g. 09:27, while the signal only confirms at the
bar CLOSE, 09:30). On a breakout candle that means "selling" the option at the
richer pre-move price — pure look-ahead.

`aakash_spread_backtest.py` now fills at the first option tick at-or-AFTER the
candle close (label + 3min). `OLD_FILL=1` reproduces the biased behaviour.

**Impact (2026 YTD, 1 lot, charges-only):**
| Fill | Net | Win | Payoff | Max DD |
| --- | --- | --- | --- | --- |
| OLD (look-ahead) | **+₹198,424** | 48% | 3.03x | −₹3,852 |
| NEW (no look-ahead) | **−₹67,060** | 32% | 1.66x | −₹69,128 |

**The strategy FLIPS from +₹198k to −₹67k once entries are realistic.** Every
prior positive number this session (+₹50k/month, +₹779k 5-yr, +₹13L @5 lots) was
an artifact of look-ahead bias. The breakout-candle entry is exactly the move
that goes against the option premium, so realistic fills erase and reverse the
edge. **As currently designed, the strategy is NOT profitable.**

(Lot sizes now use MODE per expiry + web-verified schedule 25→75→65; full per-leg
audit trail + daily/weekly/monthly/yearly CSVs added.)

**FULL no-look-ahead result (Aug-2024→Jun-2026, 23 mo tradeable, 1 lot, charges-only):**
- Net **−₹101,554** | 2,544 spreads | win **29.7%** | payoff 2.13x | max DD −₹114,166 (38% of ₹3L)
- Yearly: 2024 **−₹41,256** · 2025 **+₹6,754** (breakeven) · 2026 **−₹67,052**
- A few big trend months win (Mar-2025 +43k, Jul-2025 +59k) but frequent losers
  (Feb-2025 −45k, Feb-2026 −26k, etc.) outweigh them.
- Reconciliation OK (diff ₹0). Lot timeline 25→75→65 (one stale-25 anomaly at the
  2025-01-30 monthly expiry from old long-dated contracts; immaterial to the result).
**Conclusion: the strategy as designed is NOT viable with realistic entries.**
Deliverables: results/backtest_{trades_full,daily,weekly,monthly,yearly}_2021-04-29_2026-06-09.csv,
results/full_nolookahead.log.

## 5e. HOLD mode (no flips) — big improvement, still marginal (2026-06-13)

Added `HOLD_MODE=1` (env, + `SL_MULT`): enter on a signal, IGNORE opposite signals
(no flip), exit only on a hard SL (loss >= SL_MULT × credit) or EOD. Captures theta.

**Full 23mo, no-look-ahead, 1 lot, charges-only, SL=2×credit:**
| | Flip (every signal) | HOLD (SL2x/EOD) |
| --- | --- | --- |
| Net | −₹101,554 | **+₹15,060** |
| Win | 29.7% | 47.1% |
| Payoff | 2.13x | 1.16x |
| Max DD | −₹114,166 | −₹71,673 |
| 2024/25/26 | −41k/+7k/−67k | +8k / **+51k** / **−44k** |

Holding swung it +₹116k (flip churn was the killer; theta needs time). BUT +₹15k/23mo
≈ 1%/yr = breakeven, NOT an edge. Inconsistent (2025 great, 2026 bad incl −38k month).
Proves the theta mechanism is real but the raw signal + nearest-weekly spread isn't
selective enough. Next lever to test: DTE filter (trade only 0-1 DTE / expiry days where
theta dominates — June-9 expiry-day hold made +₹2,253) and/or SL sweep.
Results: results/full_holdmode.log, backtest_*_2021-04-29_2026-06-09.csv (HOLD run).

## 6. Decisions made this session

- Trade the **filter-OFF full state machine** (user confirmed).
- **BUY→Bull Put, SELL→Bear Call**, short ~0.45Δ / long ~0.20Δ (user's spec).
- Strikes chosen by **BS delta**, IV implied from each option's own price.
- Exit on next (flip) signal; square off remaining at 15:24.
- Charges modeled (brokerage + STT + txn + SEBI + GST + stamp). Slippage = TODO.

See `TODO.md` for what we agreed to work on before the full backtest.
