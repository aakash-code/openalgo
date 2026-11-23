# End-of-day data check

After each close, OpenAlgo can check the day's recorded open interest against
the exchange's own closing file (NSE's F&O bhavcopy) and report how complete
the day's data was. Code: `services/eod_data_check.py`, run by
`scripts/eod_data_check.py`.

## What it holds exact, and what it only measures

| Check | Rule | Why |
| --- | --- | --- |
| OI Flow record against the tick store | Must agree exactly, strike by strike, at the close | Both carry the broker's feed, so a difference is ours |
| Our last live OI against the exchange's close | Measured: typical and 9-in-10 distance | Options trade to 15:40, the broker's last OI arrives about 15:39 and the official close is settled after it. NIFTY 18-20 Nov 2026: 0.3-3.8% typical |
| A contract far from the close | Problem if more than 25% off and over 1 lakh OI | A wrong contract or a dead quote, not the last minutes' lag |
| A contract the exchange does not list | Problem | A symbol mapped wrong |
| Recorded minutes, index minutes | Problem below 99% | Holes during the day |
| Longest feed gap | Problem over 30 s, reported with its time | On 20 Nov 2026 it found a 124 s outage at 09:22:34 |

Only contracts still being read from 15:35 are compared: strikes enter and
leave the watched range as the price moves, and one last seen at 11:00 holds
its 11:00 OI.

## Running it

```bash
uv run python scripts/eod_data_check.py                      # today, NIFTY and BANKNIFTY
uv run python scripts/eod_data_check.py --day 2026-11-20 --underlying NIFTY
```

It appends a row per underlying to `log/eod_data_check.csv` and exits 0 (all
well), 1 (something fell short) or 3 (the exchange has not published the day's
file yet). On a server, run it after the file appears (NSE publishes in the
evening) and retry on 3.
