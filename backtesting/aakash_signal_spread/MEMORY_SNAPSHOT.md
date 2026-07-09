---
name: aakash-signal-spread-strategy
description: "Ongoing strategy work — Aakash 3m BUY/SELL indicator driving delta-targeted NIFTY credit spreads; status, location, and what's pending before full backtest"
metadata: 
  node_type: memory
  type: project
  originSessionId: 2a8e15c7-214d-4317-97e7-4ad31958816f
---

Building/backtesting Aakash's intraday NIFTY strategy: a Pine v6 indicator
("Intraday BUY/SELL & AUTO SL by Aakash") generates BUY/SELL signals on **3-min**
NIFTY candles; each signal → a **delta-targeted credit spread** on the nearest
weekly expiry. BUY → Bull Put (sell ~0.45Δ PE, buy ~0.20Δ PE); SELL → Bear Call
(sell ~0.45Δ CE, buy ~0.20Δ CE). Defined risk ≈ ₹13–15k/spread (the "less margin"
goal). User CONFIRMED the canonical signal engine is **filter-OFF, full state
machine** (active-line block + zone suppression + slModeOnly latch + forced-
opposite-on-SL flips).

**All work lives in `backtesting/aakash_signal_spread/`** — see `SESSION_NOTES.md`
(context/results/caveats) and `TODO.md` (next steps). Scripts:
`aakash_signal_3m_replay.py` (signal port), `aakash_options_spread_test.py`
(signals→spreads), `aakash_indicator.pine` (ground truth). Data:
`db/historify.duckdb` (NIFTY 1m spot + NFO option 1m, coverage ~Oct 2024→2026-05-26;
NO June 2026 options yet; NIFTY index volume=0 so true VWAP impossible → trend
filter disabled).

**Results so far (per 1 lot, NET charges, NO slippage):** May 2026 full month =
**+₹50,282**, 104 spreads, 60% win, 15 up/2 down days. BUT this is a favorable
trending month and **slippage is unmodeled and decisive** — at 2 pt/leg the edge
vanishes (sensitivity: 0.5pt→+₹37k, 1pt→+₹23k, 2pt→−₹4k).

**Why:** validating before committing to a full Oct-2024→May-2026 backtest.
**How to apply:** before trusting any result, FIRST add the slippage model + a
multi-day range aggregator (P0 in TODO.md). Don't present the frictionless number
as an edge. Related: [[landing-page-scanner-chart-parity]] (other Aakash Pine work).
