Magnetic Zones → NIFTY option-selling backtest — clone the existing backtesting/cpr_option_selling/cpr_backtest.py engine (same data path, lot-size logic, FAITHFUL-vs-REALISTIC-vs-naked reporting) and swap in:

- Magnetic-Zones level math (Fib bands around prior-period midpoint) replacing CPR pivots.
- Weekly zone = strangle wings (sell CE near weekly R2, PE near weekly S2), daily zone = leg-in timing — each wing fills the day spot first pushes into its daily R-/S-band, with an OPEN fallback to complete the strangle.
- A TIMING=DAILY vs OPEN A/B to prove whether the daily-confluence timing actually adds edge over a blind weekly strangle.
- Hedged sweep + naked side-by-side, matching the CPR report format for direct comparison.

New files: backtesting/magnetic_zones_option_selling/{magnetic_backtest.py, README.md, results/} — standalone, read-only DuckDB, no app/broker/schema changes.

I flagged the real risk up front: selling at the outer R2/S2 sits further OTM than CPR's strikes, so expired-option data gaps will cause more skips (mitigated by a WING_LEVEL=R1 fallback and visible skip accounting), and high win-rate premium selling is historically net-negative once costs/tails are applied — the backtest is there to quantify that honestly.

⏺ User approved Claude's plan
Plan saved to: ~/.claude/plans/greedy-wondering-pebble.md · /plan to edit
Magnetic Zones → Option-Selling Backtest (NIFTY)

Context

The user has a TradingView Pine v6 indicator, Magnetic Zones [Open], that draws
symmetric Fibonacci support/resistance zones around a prior period's midpoint:

center = (prevHigh + prevLow) / 2 ,  range = prevHigh - prevLow
R2 = center + 0.786*range   R1 = center + 0.236*range
S1 = center - 0.236*range   S2 = center - 0.786*range

The thesis is mean-reversion ("magnetic" pull back toward center). The user wants to
turn this into an option-selling system on NIFTY and validate it with a backtest
before risking capital.

Chosen design (from clarifying questions):
- Timeframe: Daily + Weekly confluence — the weekly zone defines the strangle
wings, the daily zone times entries (leg-in).
- Logic: Range strangle / IC — sell CE near weekly R2 + PE near weekly S2,
collecting theta if price stays inside the weekly zone.
- Risk: Run hedged (spread/IC) AND naked side-by-side, like the CPR report.
- Deliverable: Backtest first (FAITHFUL vs REALISTIC-with-costs).

This is structurally identical to the existing
backtesting/cpr_option_selling/cpr_backtest.py (zone-from-prior-period →
option-sell decision → grid-snapped strikes → priced from db/historify.duckdb).
We clone that engine's scaffolding and swap the level math + entry logic.

Data sources (already present, no new ingestion)

- db/historify.duckdb, table market_data (1m bars):
  - NIFTY spot: symbol='NIFTY', exchange='NSE_INDEX'.
  - Expired NFO options: symbol='NIFTY{DDMONYY}{STRIKE}{CE|PE}', exchange='NFO'
(loaded by the dhan expired-data loader broker/dhan/api/expired_data.py).
- Coverage caveat (carry over from CPR README): expired option data has gaps —
CPR skipped ~42/125 weekly expiries for missing strikes. Selling at the outer
R2/S2 (0.786×range OTM) sits further OTM than CPR's strikes, so expect more
"no_premium" skips. Mitigation built into the plan: a WING_LEVEL knob
(R2 default, R1 = closer/more data) and per-bucket skip accounting so coverage
bias is visible, not hidden.

Reusable pieces to copy from cpr_backtest.py

Copy verbatim (or import): nifty_lot_size, ceil_grid, floor_grid,
parse_expiry_code/expiry_code, MONTH_MAP, _ts, _get_close, _spot,
load_spot_daily, load_expiries, weekly_bars, settle_value, summarize,
and the FAITHFUL-vs-REALISTIC + hedged/naked + hedge-width-sweep structure of
run_for_width / main.

New / changed logic

1. Level math — replace cpr_levels with magnetic_levels

def magnetic_levels(prevH, prevL, fib_inner=0.236, fib_outer=0.786):
    center = (prevH + prevL) / 2
    rng = prevH - prevL
    return dict(center=center, rng=rng,
                R2=center + fib_outer*rng, R1=center + fib_inner*rng,
                S1=center - fib_inner*rng, S2=center - fib_outer*rng)
- Weekly wings: from prior completed week's H/L (weekly_bars, prev_key,
exactly as CPR) → wk_lv = magnetic_levels(prevH_wk, prevL_wk).
- Daily trigger zones: for each trading day in the expiry week, from the
prior day's H/L → d_lv = magnetic_levels(prevH_day, prevL_day).
(Mirrors the indicator's request.security("D"/"W", [high[1], low[1]]).)

2. Wing strikes (range strangle / IC)

- Short CE strike = ceil_grid(wk_lv[WING_LEVEL_CE])  (default R2)
- Short PE strike = floor_grid(wk_lv[WING_LEVEL_PE]) (default S2)
- WING_LEVEL env: R2 (default, wider) or R1 (closer, more premium + more data).
- Hedged: protective long wing width strikes beyond each short (sweep
HEDGE_WIDTHS=[50,100,200,300] as in CPR). Naked: no long leg.

3. Entry timing — daily-zone leg-in (the Daily+Weekly confluence)

For each weekly expiry cycle:
1. Determine the trading days of that week strictly before expiry.
2. Walk days in order. On each day compute d_lv from the prior day's H/L, then
scan that day's 1m spot bars:
  - First bar whose price enters the daily R-band (d_lv[R1] ≤ price ≤ d_lv[R2],
i.e. spot rallying into resistance) → sell the CE wing at that bar's option
close (no look-ahead: fill at/after the trigger bar, per the aakash discipline).
  - First bar entering the daily S-band (d_lv[S2] ≤ price ≤ d_lv[S1]) →
sell the PE wing.
  - Each wing fills at most once per cycle.
3. Fallback (FALLBACK env, default OPEN): any wing not triggered by the last
pre-expiry day is sold at that day's 09:20 open so the strangle completes; set
FALLBACK=SKIP to leave it asymmetric. Record which wings were leg-in vs fallback.
4. Comparison toggle (TIMING env): DAILY (leg-in, default) vs OPEN
(sell both wings at week open, ignoring the daily zone) — lets us measure whether
the daily-zone timing actually adds edge vs a blind weekly strangle.

4. Exits, settlement, costs — reuse CPR model unchanged

- FAITHFUL: hold to weekly expiry, settle_value at spot intrinsic, no costs.
- REALISTIC: per-cycle SL (loss ≥ SL_MULT × credit, MTM at 15:15 daily walk),
SLIPPAGE_PTS/leg, COST_RS_PER_LEG brokerage+taxes both sides; lot via
nifty_lot_size(entry_day) (25→75→65).
- Credit = sum of (short premium − long premium) across both wings; the two wings
may fill on different days — accumulate credit as each leg fills.

5. Reporting — reuse summarize, add buckets

- Side-by-side: hedged width sweep (50/100/200/300) FAITHFUL vs REALISTIC, plus a
NAKED block with worst-single-trade tail proxy — identical layout to
cpr_summary.json so results are directly comparable to the CPR study.
- Extra split: by wing-fill timing (leg-in vs fallback) and by TIMING=DAILY vs
OPEN so the confluence value is explicit.
- Skip accounting: no_prev_week, no_daily_prev, no_premium, low_credit,
no_trigger.

Files to create

backtesting/magnetic_zones_option_selling/
  magnetic_backtest.py     # the engine (CPR scaffold + magnetic logic above)
  README.md                # thesis, run cmd, env knobs, results table, caveats
  results/                 # magnetic_summary.json, magnetic_trades_w50.json,
                           #   magnetic_trades_naked.json (written by the run)
No changes to app code, brokers, or the DB schema — this is a standalone analysis
script in the established backtesting/ pattern (read-only DuckDB connection,
closed at end — FD-clean).

Config / env knobs (defaults in brackets)

START [2021-04-29] · END [2026-06-09] · FIB_INNER [0.236] · FIB_OUTER [0.786]
· WING_LEVEL [R2] · TIMING [DAILY] · FALLBACK [OPEN] · HEDGE_WIDTHS [50,100,200,300]
· SL_MULT [2.0] · SLIPPAGE_PTS [1.0] · COST_RS_PER_LEG [30] · MIN_CREDIT_PTS [2.0]
· GRID [50].

Verification

1. Unit sanity (no DB): assert magnetic_levels(100, 0) → center 50, R2 128.6,
R1 73.6, S1 26.4, S2 −28.6; assert grid snapping and that CE strike > spot >
PE strike for a typical week.
2. Smoke run on a small window:
START=2025-01-01 END=2025-03-31 uv run python backtesting/magnetic_zones_option_selling/magnetic_backtest.py
— confirm it loads spot/expiries, produces trades, prints FAITHFUL/REALISTIC/NAKED
blocks and a skip dict, and writes results/*.json.
3. Confluence A/B: run TIMING=DAILY vs TIMING=OPEN over the same window and
compare net/PF/win% to judge whether daily-zone timing helps.
4. Coverage check: inspect the skip dict; if no_premium is large at WING_LEVEL=R2,
re-run with WING_LEVEL=R1 and report both (documents the OTM-data limitation).
5. Full run 2021→2026, capture the results table into README, mirroring the CPR
report format for an apples-to-apples comparison.

Honest expectation (set before building)

The CPR study found ~80% win rate but net-negative once realistic — the classic
premium-selling shape (many small wins, few large losers). A weekly Magnetic-Zones
strangle at R2/S2 is wider (should lift win% further) but the same tail dynamics
apply, and far-OTM data gaps may bias the sample. The backtest's job is to quantify
this honestly — hedged-vs-naked and DAILY-vs-OPEN — not to assume an edge exists.
