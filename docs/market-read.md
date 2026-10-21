# Market read

The **Market read** panel on the `/trading` right rail reads, every minute,
what a chart cannot show at once: who is writing NIFTY's options, what its
futures traders are doing, where support and resistance sit and whether they
are holding, what volatility is doing, and whether BANKNIFTY and SENSEX agree.
It says what it finds in sentences, with the numbers behind each.

It needs the Writer Flow recorder (`OI_FLOW_RECORDER_ENABLED=true`), which
records the option chain it reads. See [writer-flow.md](writer-flow.md).

## What it reads

| Reading | What it is | How it counts |
| --- | --- | --- |
| Option writers | Open interest added to puts minus calls, strikes within 6 of the price, over 10 minutes | Puts lead or calls lead once the difference stays above 1% of the open interest near the money for 3 minutes running |
| Futures | Price against open interest over 5 minutes, current and next month added | Price up with open interest up: new longs. Price up with it down: shorts covering. Price down with it up: new shorts. Price down with it down: longs exiting |
| Support | The strike at or below the price with the most put open interest | Weakening once it is 10% below its own peak for the day |
| Resistance | The strike at or above the price with the most call open interest | The same |
| Volatility | India VIX, and implied volatility at the money priced off the future | A jump is 0.25 India VIX points within 10 minutes |
| Other indices | BANKNIFTY and SENSEX option writers, read the same way | Agree, or turned first |

A level **turns from support to resistance** when the price has stayed below
its strike for 3 minutes, its put open interest is at least 5% below its peak,
and call open interest there is rising. The same rule, the other way round,
turns resistance into support.

## Alerts

Only turns alert: the lead changing hands, a level turning, writers leaving
the current support or resistance, India VIX jumping, and the opening gap
filling. Everything else waits in the panel's timeline.

- **Sound and popup**: a tone and a popup on `/trading`, with the panel open or
  closed.
- **Telegram**: through the Telegram bot, at most one message every 10
  minutes; events in between go together in the next message. A restart
  reads the morning again and sends nothing for it.

Each is a switch in the panel's settings, as is the read itself.

To keep alerts to turns, a lead that changes back within 15 minutes, or the
same side returning after a quiet spell, is shown but not alerted, and a
strike turns at most once every 30 minutes. On expiry day open interest falls
at every strike as positions close, so "writers leaving" is never alerted that
day.

## What it does not tell you

- **It describes; it does not forecast.** On the three days it was built from
  (16, 19 and 21-Oct-2026), net writer flow tracked the price of the previous
  15 minutes (correlation +0.3 to +0.8) and not the next 15 (-0.3 to +0.1).
  Every minute's reading is stored in `market_read_minutes`, so this can be
  measured again over more days.
- **"Writers" means open interest added.** Who started a trade, buyer or
  seller, cannot be told from minute open interest and last price. Within a
  few tenths of a volatility point, the same minute reads as writing or as
  buying depending on how it is priced, so the panel does not label it.
- **No news.** A turn can follow news the panel cannot see.

## Cost

One batch quote a minute for the current- and next-month futures, India VIX
and the index (its previous close): about 375 broker requests a day. The
first read after a start, and every 15 minutes after it, fills missed minutes
from one-minute history for the two futures and India VIX, paced as
background work.

## Settings

| `.env` key | Default | Meaning |
| --- | --- | --- |
| `MARKET_READ_UNDERLYING` | `NIFTY` | The NSE index read in full. Its futures must be listed on NFO. |

The switches (read, Telegram, sound) are kept in the Writer Flow record, not in
`.env`, and take effect at once.
