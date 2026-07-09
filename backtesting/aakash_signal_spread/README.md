# Aakash 3m Signal → Delta Credit Spread

Intraday NIFTY strategy: a Pine v6 indicator generates BUY/SELL signals on
3-minute candles; each signal is traded as a delta-targeted credit spread on the
nearest weekly expiry (BUY → Bull Put, SELL → Bear Call; sell ~0.45Δ, buy ~0.20Δ).

**Start here:** [`SESSION_NOTES.md`](SESSION_NOTES.md) — full context, results,
caveats, decisions. Then [`TODO.md`](TODO.md) — what to fix before the full run.

## Files
- `aakash_indicator.pine` — original TradingView source (ground truth).
- `aakash_signal_3m_replay.py` — faithful Python port of the signal engine.
- `aakash_options_spread_test.py` — signals → delta credit spreads (single day).
- `expiry_day_0dte_backtest.py` — separate earlier 0DTE study (reference only).
- `results/` — CSV outputs.

## Run
```bash
# from this folder
DAY=2026-06-12 uv run python aakash_signal_3m_replay.py        # validate signals
DAY=2026-05-22 uv run python aakash_options_spread_test.py     # signal → spreads
```
Data source: `db/historify.duckdb` (NIFTY 1m spot + NFO option 1m).
Option data coverage: ~Oct 2024 → 2026-05-26 (no June 2026 yet).

## Status
Pipeline validated on May 2026 (+₹50,282/lot frictionless, 60% win). **Not
production-ready** — slippage unmodeled (decisive), single favorable month only.
See TODO before trusting results.
