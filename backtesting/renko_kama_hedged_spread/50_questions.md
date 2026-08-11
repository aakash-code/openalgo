# 50 Questions — Renko+KAMA Hedged NIFTY Credit Spread

Reference: `renko_kama_spread_backtest.py`, `results/trades_detail.csv`, `results/daily_summary.csv`.
Config discussed below: 5-min bars, 15pt renko brick, KAMA(14,2,30) on brick closes,
CHOP(14)<38.2 filter, 5 lots (qty=325), 90-day window (2026-04-27 to 2026-07-24).
**Numbers below are post-fix** (an EOD-square-off bug that let positions carry
overnight was found and fixed during this analysis — see the note at the bottom).

## Performance & risk shape

1. Why is max drawdown (-Rs 48,305) concentrated in a single ~3-week stretch
   (2026-05-04 to 2026-05-25) rather than spread evenly across the 90 days?
2. What was different about the market regime during 2026-05-04 to 2026-05-25
   that made this strategy's signal wrong so often in a row?
3. It took 42 days to recover the drawdown (2026-05-04 to 2026-06-15) — is that
   recovery time acceptable relative to how often this magnitude of drawdown
   might recur, and is 90 days even enough history to know that?
4. Is the max drawdown likely to be *worse* than backtested once real slippage
   and partial fills are added (this backtest fills at the option's own last
   traded 1m/5m close, not a modeled bid/ask spread)?
5. How does max drawdown scale with LOTS — does going to 10 lots roughly double
   it, or does margin-driven position sizing change the relationship?
6. What is the Sharpe/Sortino ratio of the daily P&L series, not just the
   weekly-P&L "stability" metric already computed?
7. Is there a maximum consecutive-losing-day streak, and how many trades/days
   does it take before someone monitoring this live would reasonably conclude
   "the edge is gone" vs "this is normal variance"?
8. How sensitive is net P&L to the exact CHOP threshold (38.2) — does 35 or 42
   change the outcome by a little or a lot? (Grid tested 61.8/50/38.2 only.)
9. How sensitive is net P&L to the exact brick size (15pt) — is 15 a genuine
   local optimum or noise from only testing 10/15/20/25/30/ATR?
10. Does the win rate (55.1%) hold up if the sample is split into two 45-day
    halves, or is it concentrated in one half?

## Gap-up / gap-down behavior

11. Why does the strategy earn ~20x more per gap-down day (Rs 5,601 avg) than
    per gap-up day (Rs 277 avg) — is this a real structural asymmetry (e.g.
    put IV skew makes bull-put spreads better priced) or a 90-day sample-size
    artifact (16 gap-down days vs 12 gap-up days)?
12. On gap-up days that fade (reverse down), the strategy loses on average
    (-Rs 1,006/day) — is this because the bear-call entry comes too late
    (after CHOP/KAMA confirm the reversal, most of the move is already gone)?
13. On gap-down days, both continuation and fade are profitable — why does
    gap-down not have the same fade-losses problem that gap-up does?
14. Would skipping trading entirely on gap-up days (>0.3%) improve risk-adjusted
    return, or does it just cut a small amount of both winning and losing days?
15. Is the 0.3% gap threshold used for classification here the right cutoff, or
    would a smaller/larger threshold change which days get bucketed where?
16. Does the *size* of the gap (not just up/down direction) correlate with
    that day's P&L — do bigger gaps behave differently than marginal ones?
17. How does the strategy behave on the specific worst day (2026-05-06, gap-up
    +0.49%, gap-and-go continuation, still -Rs 19,633)? Continuation days are
    supposed to be the "good" gap-up case — what went wrong here specifically?
18. Is "day_return_pct" (close vs that day's own open) the right proxy for
    gap continuation/fade, or should it be measured against the pre-gap level
    (prior close) instead of the day's own open?
19. Do gap days correlate with higher realized volatility that would blow
    through the delta bands (0.45-0.55 / 0.20-0.25), causing worse strike
    selection specifically on those days?
20. Should EXPIRY_DATE proximity interact with gap behavior — are gap-day
    losses worse near weekly expiry (higher gamma) than mid-week?

## Signal engine (Renko / KAMA / CHOP)

21. Why does variant A (KAMA on renko brick closes) beat variant B (KAMA on
    raw price) at every single brick size tested — is this specific to NIFTY's
    intraday noise profile, or would it hold on other indices/timeframes?
22. Is 5-minute definitively better than 1-minute, or was the 1-minute result
    handicapped by the same lookahead-labeling bug found and fixed in this
    session (now that it's fixed, does re-running 1-minute change the gap)?
23. Was 15-minute or higher ever tested? Does the "coarser timeframe reduces
    noise" trend from 1m->5m continue improving at 15m/30m, or does it peak
    and then start losing genuine signal?
24. How many renko bricks form on an average day at 15pt/5-min — is the
    strategy under-trading (too few signals to be statistically meaningful)
    or over-trading relative to what a human discretionary trader would do?
25. Does the ATR-based dynamic brick size ever outperform fixed 15pt once the
    EOD bug fix is applied, or does fixed sizing remain dominant?
26. KAMA(14,2,30) parameters were never swept — would a faster KAMA (shorter
    length, or a wider fast/slow band) change the whipsaw-vs-lag tradeoff?
27. Is there a meaningful difference between a "flip" exit (immediate
    re-entry into the opposite spread) and adding a short cooldown/confirmation
    period before flipping, to avoid single-bar reversals?
28. Since CHOP forces the trend to *neutral* (not to "hold current position"),
    does a position ever sit through a long choppy stretch without an EOD
    close because CHOP suppressed the *opposite* signal that would have
    flipped it out sooner? Is that desirable or a hidden risk?
29. What fraction of trading days never generate a valid signal at all
    (CHOP always >= 38.2 all day) — how much idle/no-trade time is there?
30. Does the strategy behave differently in the first hour (09:15-10:15,
    often the most volatile) vs mid-day vs the last hour before square-off?

## Options execution & pricing

31. The backtest fills using the option's own last-traded 1m/5m close — how
    much does this diverge from a realistic fill using bid/ask quotes,
    especially on far-OTM hedge legs with thin liquidity?
32. Delta/IV are solved from the option's own live premium via Black-Scholes on
    spot (not Black-76 on a forward/synthetic future, which OpenAlgo's own
    `optiongreeks()` uses) — how much would delta selection differ using the
    house Greeks engine instead, and would that change which strikes get picked?
33. Is Rs 0.05 the right floor for excluding illiquid/stale option prices, or
    does it let through prices that are technically nonzero but not
    realistically tradeable?
34. How often does `pick_in_band` fall back to "closest to band center" because
    no strike actually lands inside [0.45,0.55] or [0.20,0.25] delta — and does
    that fallback produce meaningfully worse trades than an in-band pick?
35. Are strike gaps ever wide enough (NIFTY's 50-point grid vs a fast-moving
    spot) that the realized short/hedge delta is far outside the intended band
    by the time the order would actually fill live?
36. Real Upstox margin (calibrated in this session) has a ~Rs 30,468 floor
    per lot regardless of spread width — does a narrower, cheaper structure
    (e.g., always using the same OTM offsets rather than delta-targeting)
    achieve similar P&L with less margin drag?
37. How much of the gap between "required_margin" (pre-hedge-netting) and
    "final_margin" (post-netting) is broker-specific — would Zerodha or
    another broker's SPAN engine give a materially different final margin
    for the same position?
38. Since lot size and lot value change over time (SEBI schedule), how much
    would results differ if the backtest used the ACTUAL historical lot size
    at each expiry rather than assuming a constant 65 throughout?
39. Charges use a fixed cost model (brokerage/STT/txn/GST/stamp) — how
    sensitive is net P&L to broker choice (discount vs full-service) given
    107 trades' worth of round-trip charges?
40. Does the width (strike gap) between short and hedge legs correlate with
    trade P&L — are wider spreads (higher margin, higher max loss) actually
    the more profitable ones, or is width just noise?

## Live-execution readiness

41. The live strategy file polls history every 30s and resamples locally —
    how much lag exists between a bar "closing" in wall-clock time and the
    strategy actually detecting it, and does that lag matter for entries near
    volatile bars?
42. Does the live file's warm-up (15 trailing days via `client.history`)
    reliably return the same bar-count/quality as the DuckDB-backed backtest,
    or could a live warm-up gap produce a different initial KAMA/CHOP state?
43. What happens if `optionsmultiorder` partially fills (one leg fills, the
    other rejects) — does the current code detect and handle that, or does it
    silently leave a naked position?
44. What happens if the sequenced close (buy back short, then sell hedge) has
    its first leg fill but the second leg's placeorder call fails/times out —
    is there a monitoring/alerting path for that half-closed state?
45. Since EXPIRY_DATE is hardcoded and never auto-rolled, what's the operational
    plan for catching a stale expiry before Thursday if nobody manually
    updates the parameter that week?
46. Has this exact live file been given a full dry run in Analyzer Mode yet,
    and if so, did the observed entries/exits match what the backtest would
    have predicted for the same day's data?
47. Is there a plan to log structured trade records (not just text logs) from
    the live strategy so live performance can be compared against this
    backtest apples-to-apples later?
48. What is the intended response if the live strategy crashes mid-day with an
    open position — does the host's SIGKILL-after-15s escalation leave a
    naked/unhedged position with no automatic recovery?
49. Should a hard per-day loss cap or max-trades-per-day breaker be added on
    top of signal-flip/EOD-only exit, given the -Rs 48,305 single-stretch
    drawdown already observed in only 90 days?
50. Before sizing this beyond 5 lots for real capital, what additional
    out-of-sample period (a different 90 days, ideally one including a
    genuinely trending month and a genuinely rangebound month) would be needed
    to trust these numbers aren't overfit to this particular quarter?

---

## Bug found and fixed during this analysis

The original `run_backtest()` had `if side is None: continue` positioned
*before* the EOD square-off check, so whenever the trend went neutral (CHOP
too high) right at the end of a session, the day's forced-close never ran and
the position silently carried into the next trading day. This was caught by
noticing a "flip" trade in the detail CSV whose entry and exit were on
different calendar dates. Fixed by moving the EOD check outside the
`side is not None` guard so it always runs regardless of trend state. This
changed the 5-lot 90-day result from **net +Rs 353,490 (83 trades)** to the
corrected **net +Rs 152,849 (107 trades)** — a ~57% downward revision. All
numbers in this document and the accompanying CSVs are post-fix.
