# Writer Flow: what option writers did at each strike, through the day

The OI Profile shows open interest at one moment. Writer Flow shows what
changed and for how long, strike by strike: where call writers have been
covering for the last 20 minutes, where put writers keep adding underneath,
which strike has not moved for 10 minutes. It is a separate feature with its
own recorder and database; it does not change the OI Profile.

How to read it on the chart is in [writer-flow-guide.md](writer-flow-guide.md).

It is drawn in two places, both reading the same record:

- **On the `/trading` chart**, where a strike is a price and the record is in
  time, so the flow lines up with the candles.
- **At Tools > Writer Flow** (`/writerflow`), a full page for studying the day.

## On the chart

Chart the index (NIFTY, BANKNIFTY, SENSEX on `NSE_INDEX` / `BSE_INDEX`) and
click **Writer Flow** in the toolbar, beside OI Profile. It turns on both
halves below, and off again; the gear beside it opens the settings. Both are
also in Indicators, to add one without the other:

- **Writer Flow**, on the candles.
  - Behind them, each strike carries a thin strip per step: calls just above
    the strike line, puts just below. Green is flow that helps the index (call
    writers covering, put writers adding), red is flow that weighs on it (call
    writers adding, put writers covering). The stronger the colour, the more
    open interest moved; moves under 8% of the day's typical size are left out
    so the strips show where something happened.
  - At the right edge, each strike's current streak: a bar as long as the open
    interest it has moved, with its minutes ("C 8m" for calls, "P 12m" for
    puts) once it has run five minutes. A small grey tick is a steady side.
    Hover a strike for the reading in words.
  - Diamonds where a writing or covering streak reached 10, 15 and 20 minutes,
    larger for longer.
  - Settings: the underlying (blank follows the chart), the step, calls, puts
    or both, and each layer on or off.
- **Writer Flow Totals**, in its own pane: the call and put open interest
  change since the record began (lakhs), and for every bar the put change
  minus the call change: green when puts were added faster than calls.

Strikes sit at their own price, so on a futures chart, which trades at a
premium to the index, the strips sit that premium below the candles they
belong to. The index chart is the one these read best on.

The chart asks `/writerflow/api/flow` once a minute while the tab is in front;
both indicators, and every chart of the same underlying, share one request.
The module is `strategies/indicators/writer_flow_live.js`, loaded at runtime
like every custom indicator; it ships with OpenAlgo beside the OI Profile.

## The page

Tools > Writer Flow: the strike ladder (calls left, puts right, bars for the
current streak), the strike-by-time picture for calls and puts with the price
path and the streak markers, and the call and put change per step with the
price line. It refreshes every minute during the session and shows the strikes
the recorder is watching now.

## Turning the recorder on

Off by default. In `.env`:

```
OI_FLOW_RECORDER_ENABLED=true
OI_FLOW_UNDERLYINGS=NIFTY:NSE_INDEX,BANKNIFTY:NSE_INDEX,SENSEX:BSE_INDEX
OI_FLOW_STRIKES_AROUND_ATM=15
```

Restart OpenAlgo. Every minute that the underlying's options market is open (the
market calendar's hours: F&O to 15:40, MCX to 23:55, holidays and special
sessions included) it takes
one option-chain answer per underlying and stores, for every contract within
the strike window around ATM, the open interest, premium, volume, lot size and
the underlying's price. The cost is one broker call per underlying per minute,
about 4% of Upstox's half-hour request allowance for three underlyings.

Minutes it did not catch are filled from one-minute history (one call per
contract, paced by the shared history gate): the morning, when it is started
during the session, and any minute missed after that. Every five minutes, and
once at 15:35, it lists the minutes since 09:15 that hold nothing, leaving the
last two to the live beat, and asks history for them, for the contracts
recorded within five minutes either side of the gap: a gap during a fast move
is filled for the strikes that were near the money then, not now. A minute is
asked for twice at most, so a holiday or a broker outage does not cost calls
all day. With nothing missing it makes no broker call at all. A minute the
live recorder already holds is never overwritten by history, and a candle with
no open interest reading is skipped rather than stored as zero.

A minute goes missing when OpenAlgo is starved of CPU or restarted. On
16-Oct-2026 a machine at a load average of 103 held the beat back from 12:32
to 12:35, and a restart cost 12:15; the next fill restored all five minutes
for NIFTY, BANKNIFTY and SENSEX within four minutes.

The record lives in `db/oi_flow.duckdb` (`OI_FLOW_DATABASE_PATH` to move it).
A strike that drifts outside the window as ATM moves keeps what was recorded
for it and is sampled again when it is back inside. Until then its present is
unknown, which is not the same as steady: the page leaves it out, and the
chart keeps its history behind the candles but draws no current streak for it.

DuckDB lets one process hold the file, and that is OpenAlgo while it runs. To
look at the record from a script, copy the file (with its `.wal`) and open the
copy. Each minute is written in one transaction, so nothing reads half a
minute.

## How a strike is read

Open interest alone cannot tell a writer from a buyer, so a strike side (call
or put) is read from its OI and its premium together:

| OI | Premium | Reading |
| --- | --- | --- |
| up | down | **Writing**: writers adding |
| down | up | **Covering**: writers exiting |
| up | up | Buying: buyers adding |
| down | down | Unwinding: buyers exiting |
| up or down | unchanged | Adding or reducing: side unclear |
| below the noise floor | any | **Steady** |

The noise floor is the larger of 0.5% of what the strike side held and 5 lots.
Open interest has no measurement noise, every change is contracts traded, so
the floor only mutes moves too small to matter: at 2% it read 63% of the
minutes near NIFTY's ATM on 16-Oct-2026 as steady although OI changed in
80-85% of them; at 0.5%, 30%.

**A streak** follows the direction of OI, and its reading comes from the
premium across the whole streak, not minute by minute. A strike whose OI fell
for 40 minutes while its premium rose overall reads "covering for 40 minutes",
however much the premium wobbled in between. A streak ends when OI moves
against it by more than the noise floor, or after 5 minutes without a
meaningful move. Covering arrives in chunks: on 16-Oct-2026 the NIFTY 23000 CE
covered for half an hour with quiet minutes in between, and a stricter rule
split it into pieces too short to notice.

**Steady for** is the time since the strike side's last meaningful move.

**The step** follows how often the exchange updates OI for that underlying,
measured from the day's own data: on 16-Oct-2026 NIFTY's ATM options changed
OI in 80-85% of minutes (a one-minute step), SENSEX's in about 37% and
CRUDEOIL's in about 24% (three minutes). Reading SENSEX by the minute would
call two minutes in three "steady" when it is only waiting for the next update.

**Events** are raised when a writing or covering streak reaches 10, 15 and 20
minutes.

Every threshold above is a setting (`FlowSettings` in
`services/oi_flow_analysis.py`).

## What it showed on its first morning

NIFTY, 16-Oct-2026, 09:15 to 10:44, ATM plus or minus 3 strikes:

- 09:25 to 09:35: call writers adding as NIFTY opened, 23000 CE +89.6L in 10
  minutes and 22950 CE +64.5L in 20.
- From about 10:00: call writers covering through the rally, 23000 CE -53.3L
  and 22950 CE -50.2L in 15 minutes.
- At 10:44: put writers holding underneath, 23100 PE writing for 45 minutes
  (+88.8L) and 23150 PE for 46 minutes (+51.9L).

## What is trusted, and what is not

A wrong number on screen is worse than a missing one, so anything that is not
a reading is treated as no reading (`reading_oi`, `reading_ltp` in
`services/oi_flow_analysis.py`):

- The option chain answers 0 for a contract whose quote did not come back.
  The recorder does not store a leg with neither open interest nor a price,
  and the analysis reads a 0 after a contract has held OI as no reading.
  Otherwise one dropped quote drew the strike's whole OI as covered, then
  written back a minute later.
- A premium of 0 is a contract not traded yet, not a price: the side is shown
  as unclear rather than read from a leap from nothing.
- A contract unseen for longer than the quiet break starts over from its next
  reading instead of drawing hours of change as one step.
- A one-minute history candle holds the state at the end of its minute, which
  the live recorder reads a minute later (live OI at t matched the candle
  labelled t-1 in 25 of 35 minutes on 16-Oct-2026). Filled minutes are stored
  at the candle's minute plus one.
- Numbers that are not finite are never stored.
- One process writes the record. The recorder claims `<record>.writer` with an
  OS file lock for its life; a second process (an old one still shutting
  down, or a second copy) does not start recording and logs why.

## Hours and units come from OpenAlgo, not from this code

- **Hours**: each underlying records while its options market is open, as
  `database/market_calendar_db.get_effective_session_window` gives it (the
  same calendar `/api/v1/market/timings` serves): F&O to 15:40 since the
  closing auction (03-Aug-2026), MCX 09:00 to 23:55, trading holidays, the MCX
  evening session on an NSE holiday, and special sessions. The recorder asks
  once per exchange per day; if the calendar cannot be read it uses the
  calendar module's own defaults.
- **Options exchange**: `services/option_symbol_service.get_option_exchange`
  (NSE_INDEX to NFO, BSE_INDEX to BFO, MCX to MCX, and the rest).
- **MCX's underlying price**: MCX lists no spot, so history-filled minutes take
  the near-month future through `resolve_underlying_quote`, as the live chain
  does.
- **Open interest units**: NFO and BFO quote quantity, MCX quotes lots, and
  no OpenAlgo API states which (see
  `docs/audit/broker-exchange-separation-audit.md`). The analysis reads it
  from the data: a contract whose readings are all whole lots is in quantity,
  otherwise in lots (`oi_lot`). Before this, a "five lots" noise floor became
  500 on CRUDEOIL and every MCX minute read steady. The API reports the unit
  (`oi_unit`: `quantity` or `lots`) and the chart and the page show lakh qty or
  lots to match. Never "contracts": the exchange counts one lot as one
  contract, so NIFTY quantity called contracts read 65 times too high.
- **Premium ticks**: no tick size is assumed; prices sit on their tick grid, so
  only floating-point noise is ignored.

## Answers are built once per write

Every refresh used to rebuild the whole day (0.5 to 0.9 s at 13:00 on
16-Oct-2026, growing through the session, repeated for every open chart).
`services/writer_flow_service.py` keeps each answer, as the bytes sent, until
its underlying's write count moves; two viewers asking at once wait on one
build; after each write the recorder rebuilds only the views a chart asked for
in the last 150 seconds, which is a chart still open (it asks every minute).
Answers are sent gzipped (160 KB became 30 KB), and the chart asks with
`lite=1`, leaving out the per-cell readings it does not draw.

### A new minute reaches the chart within about a second

After each write the recorder rebuilds the views being watched and then
announces `writer_flow_update` with the underlying over Socket.IO
(`services/oi_flow_recorder_service._announce`). The chart indicator hears it
through `onServerEvent` and fetches at once, served from memory because the
rebuild came first. Before, it found the minute at its own 60-second poll,
counted from when the chart was opened: anywhere from 5 to 60 seconds after
the reading. The poll stays as the safety net and is pushed back a full beat
after each announcement, so an announced minute is not fetched twice. A
failed announcement is logged and never stops the minute beat.

The exchange itself updates open interest about once a minute (every three
for SENSEX), so this is as current as the data gets.

### Flicking through symbols does not stall the platform

A build is still 0.5 to 0.8 s of Python: measured on 16-Oct-2026's full days,
NIFTY 0.62 s (24,216 rows), BANKNIFTY 0.64 s, SENSEX 0.55 s, CRUDEOIL 0.80 s
(36,163 rows), NATURALGAS 0.69 s. A cached answer is 0.1 ms. Three things keep
the cost from reaching the rest of the platform:

- **Only open charts are rebuilt.** Rebuilding every view asked for in the last
  ten minutes meant eight symbols looked at in passing cost five to six seconds
  of CPU every minute, all day. Now a symbol looked at and left costs nothing
  until it is asked for again (then one build, under a second).
- **Builds run on one real thread, one at a time** (`writer-flow-builder`).
  Under the production eventlet worker a build run in the request or the
  recorder's job held the only hub, so orders, charts and ticks waited it out;
  `test_eventlet_cross_thread_locks.py` measures the hub staying live through
  a 0.6 s build. One at a time means a burst of switches queues builds rather
  than running them side by side on the same CPU.
- **A build steps aside every few milliseconds.** Python lets one thread run
  at a time, and a request that waits on the network or a database hands the
  interpreter back each time, then queues for it behind the build: measured,
  request-shaped work went from 0.5 ms to 15 ms while a build ran, and live
  on MCX the platform's own requests took up to 573 ms at each minute's
  rebuild. `analyse()` takes a `pause` hook, called every 400 rows and every
  strike side, and the builder passes a 1 ms sleep: the same work stays at
  0.3 ms during a build, and a build takes about 0.16 s longer, in the
  background.
- **Recording starts only after the chart stays on a symbol for 15 seconds.**
  Until then the chart only reads what is recorded. Flicking through ten
  stocks used to start ten recordings and some 600 history calls.

Two history savings came with it, both measured against the broker queue that
chart loads share (about three calls a second):

- **Background history calls give way to charts.** Historify downloads and the
  Writer Flow fill take a turn only when the queue is empty
  (`services/history_service._enforce_rate_limit`), so a chart's request is
  never queued behind them. Before, four waiting background calls put a
  chart's request 2.3 s behind them; now it waits only for the turn already
  booked. A background caller gives way for at most 10 s
  (`_BACKGROUND_YIELD_MAX`), because one of them, the single-symbol Historify
  download, runs inside a request.
- **The opening minute is never asked of history.** History cannot hold it,
  and asking cost one call per contract, twice a day and after every restart:
  about 630 calls a day for five underlyings, every one answered with nothing.

## For whoever changes the queries

The day filter in `database/oi_flow_db.py` is written into the SQL as
literals, after each value is checked against its shape, rather than bound as
parameters. Measured on one morning's 30K rows: `load_day` took 1.2 s with
`?` (even with only two of the three values bound) and 40 ms with literals;
listing the sessions, 1.6 to 6.8 s against 12 ms. An index on (underlying,
expiry, trade_date) changed nothing (1.94 s against 1.97 s), so there is none.

## Charted underlyings record themselves

An underlying that is not in `OI_FLOW_UNDERLYINGS` starts recording once a
chart of it has stayed open with Writer Flow on for 15 seconds: a stock future
(TECHM27OCT26FUT on NFO), the stock itself, or an index or MCX future. The
chart sends its exchange with its request, and
`services/oi_flow_recorder_service.request_recording` works out where the
underlying is quoted (NFO with an index of that name in the master contract
is the index, otherwise the NSE stock; BFO likewise against BSE), adds it, and
schedules its first minute and a fill of the day so far on the recorder's own
thread. The request itself makes no broker call.

- **It stays while a chart asks.** The chart asks every minute; thirty minutes
  after the last ask the underlying is let go. What was recorded stays.
- **At most `OI_FLOW_ON_DEMAND_MAX` at a time** (default 5, up to 20, 0 turns
  it off). One more is refused in words on the chart rather than dropping one
  somebody is watching. The check and the add happen under one lock, so
  charts asking at once cannot pass the cap.
- **No options, no recording.** An underlying with no option expiry listed is
  dropped after its first check and the chart says why; it is not asked about
  again for six hours.
- **The page and past days start nothing.** Only the chart sends its exchange,
  only after 15 seconds on the symbol, and only a request for today starts a
  recording.

What it costs while trading: each charted underlying is one more option-chain
call a minute, the same as a listed one. The first fill is one history call
per contract (about 60 for a stock at the default window), and history calls
share one queue of about three a second with the chart's own history loads.
Booked back to back, a fill would have put a trader's next symbol switch some
20 seconds behind it, so every fill rests 0.7 s after each call: it runs at
about one call a second and a chart load waits at most one slot.

## Not yet

- Only the nearest expiry is recorded.
