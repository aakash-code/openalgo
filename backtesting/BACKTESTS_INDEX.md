# OpenAlgo — Backtest Inventory & Index

_Single source of truth for every backtest in this repo. Generated during cleanup._

## Summary count
- **Survivor (options selling):** 8+ script variants, 5 saved result sets
- **Wave (futures grid):** 1 script (`strategies/backtest_wave.py`) — not yet run on our data
- **NIFTY options-selling suite (older, Supertrend/credit-spread):** ~6 scripts
- **Other:** VCP, intraday scanner, credit spread, vectorbt example

---

## 1. Survivor — the accurate, current engine ✅
**`backtesting/survivor/`** — date-aware lot sizing (25→75→65 per SEBI), net of est. costs.
- 2-yr (Jul 2024→May 2026): **Net ₹1.09 Cr**, 13.9%/yr on capital, win 83%, max DD −₹13.2L, peak margin ₹4.10 Cr.
- 5-yr (Jun 2021→2026): Net ₹1.09 Cr, Sharpe 0.89, skew −5.74 (−₹43L Jun-2024 event).
- Deliverables: `Investor_Performance_Report.pdf`, `survivor_*.csv/json/html`, `report_*.csv`. See `survivor/README.md`.

## 2. Survivor — PRIOR runs (root-level, **flat lot 75 — inflated**)
| Script | Result folder | Period | Saved net P&L | Notes |
|---|---|---|---|---|
| `survivor_backtest_ultimate.py` | `survivor_ultimate_results/` | Oct 2024–Apr 2026 (~18mo) | **₹1.27 Cr** | flat lot 75, SL 10×. ⚠️ summary ₹1.27 Cr vs trade-sum ₹0.99 Cr (inconsistent). NOT ₹1.58 Cr. |
| `survivor_backtest_v2.py` | `survivor_backtest_results/` | Oct 2024–Apr 2026 | ₹1.02 Cr | flat lot 75 |
| `survivor_backtest_optimized.py` | `survivor_optimized_results/` | Oct 2024–Apr 2026 | **−₹76 L** | "optimized" config blew up |
| `survivor_backtest_historify.py` | (duckdb) | — | — | reads `db/historify.duckdb` |
| `SURVIVOR_PRO_PACKAGE/1_NAKED_ORIGINAL/` | `.../results/` | Oct 2024–Apr 2026 | ₹65 L | naked |
| `SURVIVOR_PRO_PACKAGE/2_HEDGED_PRO/` | `.../results/` | Oct 2024–Apr 2026 | ₹60 L | credit-spread hedge, ½ the margin |
| `strategies/backtest_survivor.py`, `_v2.py`, `_live.py` | — | — | — | older/live variants |

> **All prior Survivor runs used a FLAT lot size of 75**, which over-sizes the
> Oct–Nov 2024 window (real lot 25) and 2026 (real lot 65), inflating P&L. The
> engine in §1 fixes this. Treat §2 numbers as overstated.

## 3. Wave — futures grid scalper
- `strategies/backtest_wave.py` (+ repo `backtesting/trading-algo/strategy/wave.py`). Tick-level; our data is 1-min → indicative only. Not yet run.

## 4. NIFTY options-selling suite (older)
**`backtesting/nifty_options_selling/`** — Supertrend / SMA-EMA / credit-spread option-selling backtests + optimizers + `check_data_gaps.py`. Reads `db/historify.duckdb`.
Also `backtesting/nifty_credit_spread_backtest.py`.

## 5. Other backtests
| Script | Strategy |
|---|---|
| `vcp_backtest.py` | Volatility Contraction Pattern |
| `intraday_scanner_backtest.py` | intraday setup scanner |
| `examples/python/backtesting_vectorbt.py` | VectorBT example |

---

## Cleanup recommendation
Root-level scripts (`survivor_backtest_*.py`, `generate_*_report.py`,
`forensic_loss_analysis.py`, `vcp_backtest.py`, `intraday_*`) and loose result
folders (`survivor_*_results/`) clutter the repo root. Suggested: move them under
`backtesting/legacy_survivor/` and `backtesting/misc/` (paths in those scripts are
relative, so a move needs a path check first). `survivor_state/` is **live trading
state** — leave it. Not yet moved — awaiting confirmation.
