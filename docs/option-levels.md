# Option Levels: support and resistance from where option writers sit

Price-based support and resistance ask where price has bounced before. Option
Levels asks a different question: where are the option sellers positioned, and
is that wall building or breaking. Option writers carry most of the open
interest, so the strikes they defend are the levels the index tends to respect
until the writers leave.

It reads the same record as [Writer Flow](writer-flow.md) — nothing new is
recorded for it — and draws a few clean lines on the `/trading` chart: green
where put writers hold (support), red where call writers hold (resistance).

This is not the Meridian indicators. "Meridian - Options Levels" in the picker
is a third-party price-action study (prior highs and lows, pivots, gaps, round
numbers, VWAP, ATR); despite its name it reads no option data. Option Levels
here is built from the recorded open interest.

## Turning it on

1. The Writer Flow recorder must be on (`OI_FLOW_RECORDER_ENABLED=true` in
   `.env`, then restart) and the underlying recording — see
   [writer-flow-guide.md](writer-flow-guide.md#turning-it-on). An underlying
   with nothing recorded yet says so on the chart.
2. On `/trading`, chart the index (NIFTY on `NSE_INDEX`, SENSEX on `BSE_INDEX`)
   or a commodity's near-month future, and click **Levels** in the toolbar,
   next to Writer Flow. Click again to remove it; the gear beside it opens the
   settings. It is also in Indicators, as **Option Levels**.

It updates every minute while the market is open, from the same beat that fills
Writer Flow.

## How to read it

- **A line at each major level**, not at every strike. Green is support (put
  writers sit below the price and defend it); red is resistance (call writers
  sit above and cap it). Only the strongest few on each side are drawn, so the
  chart stays readable.
- **Thickness is the strength of the level over the recorded month** — a thread
  at a weak level, a bold band at the heaviest wall of the cycle. The **primary**
  level on each side (the one the most positions have built through) is boldest
  and its price label is filled; the rest are thinner with an outlined label, so
  the levels rank themselves at a glance.
- **Solid while the wall holds, dashed while writers are leaving it**, with a
  bright tick at the right edge where writers are adding. The ranking is the
  month; the solid-or-dashed state is today, minute by minute, as the open
  interest changes. A thick line going dashed is a heavy level coming apart.
- **A faint dashed line at the gamma flip** — where the options' net gamma
  changes sign. Price tends to stick and chop near it; it is not a wall.
- **Hover a level** (near the right edge, so the crosshair keeps the rest of the
  chart) for its put and call open interest, how each is changing over the last
  ten minutes, and whether writers are adding or leaving.

## Where the strength comes from

A level's strength is not just today's open interest. It is built from the last
month of the record, across the weekly and monthly expiries the underlying has,
so a strike that has repeatedly held heavy writing scores higher than one that
built only today. Recent days count more (a seven-day half-life), so the levels
follow the positioning as it rolls rather than freezing on an old expiry.

The **side** of a level — support or resistance — is always today's positioning,
so a level never shows green above the current price: a strike that held puts a
month ago when the index was higher, and now holds calls, reads as resistance
today while keeping the strength it earned over the cycle.

## It describes, it does not forecast

Option Levels shows where writers are and how that is changing. It is not a buy
or sell signal. On the days it was built and measured, the strike with the most
open interest was rarely reached and did not reliably hold when it was, and
whether writers were adding at a strike made no difference to whether price
bounced there. Use it to see the structure a move is happening inside, not to
predict the move. A level that lines up with a price level from another study is
a stronger confluence than either alone.

## Behind it

- **Endpoint:** `GET /writerflow/api/levels?underlying=<SYM>` (session auth; the
  chart calls it). Optional `date=YYYY-MM-DD` and `expiry=19OCT26`.
- **Service:** `services/oi_levels_service.py` reads the record, builds the day
  with the pure core in `services/oi_levels/`, and adds each strike's rolling-
  month strength (`persist_put` / `persist_call`). The core does no I/O, so it is
  tested without a running platform (`test/test_oi_levels_core.py`,
  `test/test_oi_levels_service.py`).
- **Overlay:** the `Option Levels` indicator in
  `strategies/indicators/writer_flow_live.js`, loaded at runtime like any custom
  indicator, ships with Writer Flow and is offered wherever it is installed. A
  browser end-to-end check is `frontend/e2e/option-levels.spec.ts`.
- **Tuning:** `CYCLE_DAYS` and `HALF_LIFE_DAYS` in the service set how far back
  the strength looks and how fast old positioning fades; `LINE_MIN` / `LINE_MAX`
  in the indicator set the thinnest and thickest line.
