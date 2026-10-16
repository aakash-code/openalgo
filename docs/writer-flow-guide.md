# Writer Flow: how to read it

Writer Flow shows, strike by strike and minute by minute, what option writers
are doing: adding positions, covering them, or sitting still, and for how
long. Option writers carry most of the open interest, so where they add and
where they run tells you where the big money expects the index to stay.

How it is recorded and calculated is in [writer-flow.md](writer-flow.md). This
page is about reading it.

## Turning it on

1. The recorder must be on (`OI_FLOW_RECORDER_ENABLED=true` in `.env`, then
   restart). It records NIFTY, BANKNIFTY and SENSEX by default; add
   commodities to `OI_FLOW_UNDERLYINGS` as `CRUDEOIL:MCX`, `NATURALGAS:MCX`.
   Each one records while its own market is open, by the market calendar:
   NIFTY options to 15:40 (closing auction), MCX to 23:55.
2. On `/trading`, chart the **index** (NIFTY on NSE_INDEX, SENSEX on
   BSE_INDEX), or for a commodity the **near-month future**
   (CRUDEOIL19NOV26FUT), which is what MCX options are priced against. Any
   timeframe works.
   Any other F&O underlying records itself: open its chart (TECHM27OCT26FUT,
   or the TECHM stock chart) with Writer Flow on and stay on it for 15
   seconds, and the chart says "Started recording TECHM". Flicking past a
   symbol for a second or two starts nothing. The earlier part of today is filled from
   history in a few minutes, and the chart fills in by itself. Up to five
   charted underlyings record this way at once (`OI_FLOW_ON_DEMAND_MAX`), and
   each stops half an hour after its last chart closes.
3. Click **Writer Flow** in the toolbar, next to OI Profile. It adds the chart
   layer and the **Writer Flow Totals** pane together; click again to remove
   both. The gear beside it opens the settings.

On a futures chart the strikes sit about one futures premium below where the
index chart puts them, because the chart's price is the future's price.

## What is on the chart

### 1. The strips behind the candles

Each strike has two thin strips along its price line: **calls just above the
line, puts just below**. Each little block is one minute (or three, on SENSEX).

- **Green**: that minute's flow helps the index go up (call writers covering,
  or put writers adding).
- **Red**: that minute's flow pushes the index down (call writers adding, or
  put writers covering).
- **Brighter** means more open interest moved. Very small moves are left out,
  so empty means nothing worth showing happened.

Read the strips as a history: a long run of red on the call strip of a strike
above the price is call writers building a ceiling there.

### 2. The streak bars at the right edge

For every strike the recorder is watching now, one bar for calls (just above
the strike line) and one for puts (just below). A bar is the **current
streak**: what that side has been doing without a break, and how much open
interest it has moved. Longer bar, more contracts.

The colour says who is acting:

| Colour | Means |
| --- | --- |
| Red | **Call writers adding**: a ceiling is being built at this strike |
| Sky blue | **Call writers covering**: the ceiling is being removed |
| Green | **Put writers adding**: a floor is being built at this strike |
| Amber | **Put writers covering**: the floor is being removed |
| Violet | Option buyers adding |
| Pink | Option buyers exiting |
| Grey | Open interest moving but the premium did not move, so who is acting is unclear |
| Small grey tick | Steady: no meaningful change |

The key is in the lower left corner of the chart. If you prefer plain
green and red (good for the index, bad for the index), set **Streak Colours**
to *By direction*.

**The words on a bar**, for example `Call cover 8m` or `Put write 12m`:
which side, what it is doing, and for how many minutes the streak has lasted.
Words appear once a streak reaches 5 minutes (setting: *Label Streaks From*).
`+OI` and `-OI` are the grey case: open interest rising or falling with the
premium flat.

**Hover a strike's bars** for the reading in full, for example
"Calls: writers covering for 18 min, -23.40L. Puts: writers adding for 6 min,
+4.10L". A strike out of the recorder's window says so instead of guessing.

### 3. The diamonds

A diamond marks the moment a **writing or covering streak reached 10, 15 or
20 minutes** in a row, at that strike's price. Small is 10 minutes, medium 15,
large 20. Three diamonds in a row on one strike, growing, is one streak that
kept going for 20 minutes or more. **Hover a diamond** to see the time, the
strike, what the writers were doing and how much open interest moved.

The diamond takes its colour from the same key as the streak bars.

### 4. The status line

The line at the bottom left names the underlying, the expiry, the step and the
time of the latest minute. If something is wrong (recorder off, nothing
recorded yet, OpenAlgo unreachable) it says so here in words.

## Writer Flow Totals, the lower pane

It adds up every strike the recorder watches, calls and puts separately.

- **Red line: call OI added since the open**, in lakh contracts (in lots on
  MCX, which quotes open interest in lots; the status line names the unit). Rising means
  call writers are adding overall: more resistance is being built above.
- **Green line: put OI added since the open**, in the same unit. Rising means
  put writers are adding overall: more support is being built below.
- **Bars: for that candle, put OI change minus call OI change**. A green bar
  is a candle in which puts were added faster than calls (supportive); a red
  bar is the opposite.

Both lines start at zero at 09:15 (or when recording began) and count every
contract in the window, so a green line well above the red line means put
writers have been far busier than call writers today.

## What to look for, and when

- **First 15 to 30 minutes**: which strikes the writers choose. The strike
  with the biggest red call bars above the price and the biggest green put bars
  below it are today's likely ceiling and floor.
- **A breakout**: call writers covering (sky blue) at the strike just above
  the price, while put writers add (green) below. Covering that runs 10, 15,
  20 minutes (growing diamonds) is the writers giving up, not noise.
- **A breakdown**: the mirror image. Put writers covering (amber) under the
  price while call writers add (red) above.
- **A range day**: red call bars and green put bars holding for a long time,
  and the totals lines both rising slowly. Writers are comfortable on both
  sides.
- **Steady for a long time** (grey ticks): nobody is defending or leaving that
  strike. A strike that goes steady after a long writing streak has finished
  building, it is not being covered.
- **Before you act**, check the totals: a single strike covering while the
  totals keep showing more call writing is one strike being rolled, not a
  change of mind.

## Settings

| Setting | What it does |
| --- | --- |
| Underlying | Blank follows the chart (NIFTY futures read NIFTY) |
| Step | Auto follows how often the exchange updates OI: 1 minute for NIFTY and BANKNIFTY, 3 for SENSEX |
| Show | Calls and puts, calls only, or puts only |
| Flow Behind Candles | The strips on or off |
| Current Streaks At Right Edge | The bars on or off |
| Streak Markers | The diamonds on or off |
| Flow Opacity | How strong the strips are drawn |
| Label Streaks From | Minutes a streak must last before its words show |
| Max Streak Bar Width | Width of the longest bar |
| Streak Colours | By action (a colour each) or by direction (green and red) |
| Colour Key | The key in the lower left corner on or off |

## Limits worth knowing

- A charted underlying is recorded only from the moment its chart is first
  opened with Writer Flow on; the part of today before that comes from
  history, and earlier days are not filled. List it in `OI_FLOW_UNDERLYINGS`
  to record it every day whether or not a chart is open.
- On a far-month future the broker often has no bars; use the near month.
- Only the nearest expiry is recorded.
- The session's first minute (09:15, or 09:00 on MCX) can only come from the
  live recorder: one-minute history has no reading of the state at the open.
- Open interest says how many contracts are open, not who opened them. The
  writer or buyer reading comes from the premium moving with it, and when the
  premium does not move the side is shown as unclear rather than guessed.
