# CPR Option Selling — NIFTY Backtest

Faithful replication of the TradingView **CPR Option Sale Strategy** (© DeuceDavis, MPL-2.0)
on NIFTY **weekly** options. Data: `db/historify.duckdb` — NIFTY spot 1m + expired NFO option 1m.

- **Engine:** `cpr_backtest.py` — `uv run python backtesting/cpr_option_selling/cpr_backtest.py`
- **Window:** 2022-01-01 → 2026-06 (~4.5 yrs). Weekly CPR from prior week H/L/C; enter at week open,
  exit at weekly expiry. 1 lot/signal at date-aware SEBI lot size (25→75→65).
- **Two exit models:** FAITHFUL (hold-to-expiry, no costs — matches the indicator's win% definition)
  and REALISTIC (intra-week SL = 2× credit, 1 pt/leg slippage, ₹30/leg/side costs).

## Results (hedge-width sweep)

| Width | FAITHFUL win% | FAITHFUL net | FAITHFUL PF | REALISTIC win% | REALISTIC net | REALISTIC PF |
|------:|--------------:|-------------:|------------:|---------------:|--------------:|-------------:|
| 50    | 78.6%         | −₹9.0k       | 0.73        | 50.0%          | −₹73.4k       | 0.11 |
| 100   | 80.5%         | −₹12.7k      | 0.78        | 59.7%          | −₹56.9k       | 0.31 |
| 200   | 82.1%         | −₹29.0k      | 0.72        | 67.9%          | −₹89.9k       | 0.35 |
| 300   | 82.5%         | −₹55.3k      | 0.63        | 71.2%          | −₹131.7k      | 0.33 |

(net = total P&L for 1 lot/trade, ~70–80 trades.)

## Hedged vs NAKED (same CPR signals/strikes)

| Variant | FAITHFUL win% | FAITHFUL net | FAITHFUL avg-loss | REALISTIC win% | REALISTIC net | REALISTIC PF |
|---------|--------------:|-------------:|------------------:|---------------:|--------------:|-------------:|
| Hedged 50-wide | 78.6% | −₹9.0k  | −₹2.4k  | 50.0% | −₹73.4k  | 0.11 |
| **NAKED**      | 85.5% | −₹55.5k | **−₹16.4k** | 78.3% | **−₹144.4k** | 0.44 |

Naked detail (realistic): **73 EXPIRY exits net +₹13.3k** (profitable in the normal case) but
**10 STOPLOSS exits net −₹157.6k** wipe it out — worst single trade −₹35k. Faithful (no SL) worst
single −₹34k (one Iron-Condor week). Naked also needs **~5–8× the SPAN margin** per lot.

**Verdict:** naked lifts win-rate (85.5%) and makes the *typical* week positive, but a handful of tail
days erase a year of premium — the textbook naked-selling failure mode. Hedged caps each loss and is
strictly better risk-adjusted; both are net-negative on NIFTY as the indicator codes them.

## Takeaways

1. **The indicator's high win% is REAL** — ~80% faithful win rate reproduced. But **high win% ≠ profitable**:
   net P&L is **negative at every width even frictionless** (PF < 1). Classic premium-selling shape —
   the few losers (a 50-wide spread risks `width − credit`) outweigh the many small wins.
2. **Frictions kill it.** Adding a 2× SL, slippage and brokerage drops PF to 0.1–0.35 and net to
   −₹57k…−₹132k. The intra-week SL crystallises losses that a hold-to-expiry would sometimes recover.
3. **Wider hedges raise win% but enlarge absolute losses** → worse net. The Pine's default 50-wide is
   the least-bad, but still a net bleed.
4. **Iron Condor (inside-CPR) was the worst sub-bucket** (small sample, 11 trades) — two short legs,
   double the tail.

## Caveats (do not over-trust)

- **Data coverage:** of ~125 weekly expiries in-window, **42 were skipped** because the indicator-chosen
  short strike had no stored option data, +13 for near-zero credit. Results are on the ~70–80 that had
  data — a coverage bias, not a full census.
- Expiry settlement uses spot intrinsic (≈ NSE settlement). Entry at 09:20, MTM at 15:15.
- Single contract per signal; no compounding, no margin-based sizing.

**Bottom line:** the CPR map is a fine *signal* (high hit rate) but, as coded, is **not a profitable
NIFTY system** once realistic risk and costs are applied. To make it viable you'd need an edge it lacks:
profit-target exits, dynamic position sizing, regime/IV filters, or skipping the Iron-Condor leg.
