# Survivor Strategy — Backtest Suite

Backtest of the **Survivor** NIFTY weekly options premium-selling strategy on
5 years of 1-minute data (dhanloader dataset). Driving price feed:
`dhanloader/data/INDEX_SPOT/NIFTY_clean.csv`; option premiums from
`dhanloader/data/NIFTY/chunks/WEEK/1/...` indexed by absolute strike.

## Scripts
| File | Purpose |
| --- | --- |
| `survivor_backtest.py` | Core engine. Carry-overnight to weekly expiry, auto-roll, net of est. costs. Env knobs: `START_DATE`, `END_DATE`, `PE_GAP`, `CE_GAP`, `PE_SYMBOL_GAP`, `CE_SYMBOL_GAP`, `PE_QUANTITY`, `CE_QUANTITY`, `MIN_PRICE_TO_SELL`, `STOP_LOSS_MULT`, `DAILY_LOSS_CAP` |
| `survivor_analytics.py` | Institutional tear sheet (Sharpe, drawdown, skew, monthly table, capital) |
| `survivor_sweep.py` | Parameter sweep (strike/gap/min-premium) → capital efficiency |
| `survivor_risk_compare.py` | Stop-loss / kill-switch overlay comparison (full 5yr incl. Jun-2024 crash) |
| `investor_report.py` | Investor-ready **PDF** (no strategy logic exposed). Run with `uv run --with matplotlib python investor_report.py` |

## Run
```bash
# Full 5 years
uv run python backtesting/survivor/survivor_backtest.py
# Last 2 years
START_DATE=2024-07-01 uv run python backtesting/survivor/survivor_backtest.py
# With risk overlay
DAILY_LOSS_CAP=400000 uv run python backtesting/survivor/survivor_backtest.py
```

## Outputs
`survivor_trades.csv`, `survivor_equity.csv`, `survivor_summary.json`,
`survivor_equity.html`, `Investor_Performance_Report.pdf`, and `report_*.csv`
(day / weekly-expiry / month / year P&L).

## Key results (2-yr Jul-2024→2026, base config, 1 lot, net of est. costs)
Net ≈ Rs 1.09 Cr · 13.9%/yr on capital · win 83% · max DD −Rs 13.2L ·
peak margin Rs 4.1 Cr. Position sizing uses the prevailing NIFTY lot size
(25 → 75 → 65 per SEBI revisions over the period). Full-cycle (5yr) is weaker: Sharpe 0.89, skew −5.74,
−Rs 43L Jun-2024 event. A daily kill-switch (`DAILY_LOSS_CAP`) keeps ~91% of
profit while halving drawdown — see `survivor_risk_compare.csv`.

> Backtested / simulated. Not a live track record. Past performance is not
> indicative of future results. Option selling carries uncapped tail risk.
