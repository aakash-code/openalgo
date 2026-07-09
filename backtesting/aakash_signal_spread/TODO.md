# TODO — before the full Oct-2024 → May-2026 backtest

The single-month May 2026 result (+₹50k/lot, frictionless) is promising but
**not trustworthy yet**. Fix these first, in roughly this order.

## P0 — DONE (2026-06-13)

- [x] **Slippage model** — added to `aakash_spread_backtest.py` (`SLIPPAGE_PTS`
      per leg per side, applied adversely on all 4 fills). Decisive: 1pt halved
      the May result.
- [x] **Multi-day aggregator** — `aakash_spread_backtest.py` with equity curve,
      max drawdown, monthly table, win rate. 2026 YTD (1pt) = +₹45k/lot, 35%
      win, DD −₹23k. Full 5-year run (2021→2026) launched.
- [ ] **Review full 5-year result** (`results/full_run_2021_2026.log`) — does the
      edge survive across 2021-2023 regimes, or is it a 2024-2026 artifact?
- [ ] **Slippage sweep** (0.5 / 1.0 / 2.0 pt) on the full history to bound the
      assumption — at what slippage does the edge die?

## P1 — strategy design questions to settle

- [ ] **Exit logic review.** Currently flips on every SL signal + EOD square-off.
      The flip churn (~6 trades/day) is the main slippage sink. Test
      alternatives: (a) hold to a fixed profit target / % of credit; (b) one
      trade per day; (c) stop trading after N flips/day; (d) hold winners to
      expiry for theta.
- [ ] **Decide bid/ask realism.** Can we reconstruct approximate spreads from
      the data (high-low of 1m bar as a proxy), or assume a fixed tick model?
- [ ] **Liquidity/strike availability check.** Confirm the ~0.20Δ long leg
      always has a tradable quote intraday (some far-OTM strikes are thin).

## P2 — fidelity / nice to have

- [ ] **Real VWAP (trend filter).** NIFTY index volume = 0 in DB. If we want the
      Pine trend filter faithful, pull NIFTY-future 1m volume for VWAP. Decide
      whether the filter even helps (the earlier 0DTE study found a momentum
      filter HURT — test before adding).
- [ ] **June 2026 (and beyond) option data.** Download once the June weekly
      contracts expire and become available via Historify expired-FNO. Then we
      can run the user's original target day (June 12) exactly.
- [ ] **Lot-size handling across history.** Lot size changed over time (25→75→65
      per SEBI). The tester already reads per-expiry lot from DB — verify across
      the full range.

## Open questions for Aakash

1. Slippage assumption to use as the base case? (0.5 / 1.0 / 2.0 pt per leg)
2. Keep the SL-flip churn, or move to fewer trades/day?
3. Hold the last position to expiry (theta) or always square off at 15:24?
4. Position sizing: fixed 1 lot, or scale by a capital/risk cap?
