# Survivor Pro Strategy Package

This folder contains all files related to the **Survivor Options Strategy** (Hedged/Credit Spread version).

## Contents

- **backtest_engine.py**: The core logic. Processes 1.5 years of 1-minute data in ~1 minute using daily pre-caching. Implements the Hedged (Credit Spread) logic.
- **dashboard_generator.py**: Generates an interactive Plotly HTML dashboard from the backtest results.
- **latest_report.html**: The most recent visual report generated. Open this in any browser.
- **results/**: 
    - `trades.json`: Detailed log of all 6,175 trades.
    - `daily_stats.json`: Daily equity and margin tracking.
    - `expiry_stats.json`: Performance breakdown by weekly expiry.
- **sma_ema_version.py**: An alternative experimental version using SMA 68/90 and EMA 340 filters on a 5-minute chart.

## How to Run

1. **To rerun the backtest:**
   ```bash
   cd SURVIVOR_PRO_PACKAGE
   uv run backtest_engine.py
   ```

2. **To update the dashboard:**
   ```bash
   uv run dashboard_generator.py
   ```

## Performance Summary (Oct 2024 - Apr 2026)
- **Net Profit:** ~Rs 48 Lakhs
- **Peak Margin:** ~Rs 87 Lakhs
- **ROI:** ~55%
