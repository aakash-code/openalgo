# Tick capture: raw tick-level storage for backtesting

OpenAlgo normally keeps 1-minute history (Historify). **Tick capture** is an
opt-in recorder that persists *every raw tick* — the full 5-level order book plus
open interest — for index and option contracts, so you can develop and backtest
strategies at tick resolution. It is off by default.

It is the tick-level analogue of the Writer-Flow / OI-flow recorder: the same
ATM-window selection and daily expiry rollover, but instead of one minute-sampled
option-chain row it stores the raw feed, tick by tick.

## What it captures

- **Underlyings:** configured in `TICKS_UNDERLYINGS` (default `NIFTY` and
  `BANKNIFTY` on `NSE_INDEX`), plus each underlying's index spot.
- **Contracts:** the ATM ± `TICKS_STRIKES_AROUND_ATM` strike window (default 15,
  i.e. ~60 options/underlying) of the **nearest expiry**, recomputed every
  minute so the set follows spot and rolls at expiry automatically, plus the
  `TICKS_FUTURES_MONTHS` nearest futures (default 2 = current + next month).
- **Per tick (Depth mode):** last price, day OHLC, cumulative volume, OI,
  total buy/sell quantity, average price, and the **5-level bid/ask book**
  (`price`, `quantity`; order counts where the broker sends them). Both the
  **exchange tick time** (`ts_exch`) and the local **arrival time** (`ts_recv`)
  are stored.

Greeks and IV are present in some broker feeds (e.g. Upstox) but are not yet
forwarded by the streaming adapters; the store schema reserves nullable columns
for them so they can be added later without a schema change.

## How it works (and why it does not slow trading)

```
broker feed → websocket proxy → TickCapture._on_tick → real queue → writer thread → Parquet lake → DuckDB
```

- The tick callback does nothing but a lockless membership test and a
  `put_nowait` onto a real queue (`services/tick_capture_service.py`).
- A **real OS thread** drains the queue and writes Parquet, so the blocking disk
  I/O never runs on the eventlet hub — the trading path is untouched. The queue
  is bounded and sheds the oldest tick under flood (the next tick supersedes it).
- Subscriptions reuse the shared broker feed pool; capturing ~122 symbols adds no
  measurable load and never tears the live feed down.

## Storage

A **Parquet-per-day lake** (not a single growing database file), queried by
DuckDB (`database/ticks_store.py`):

```
db/ticks/date=2026-11-18/underlying=NIFTY/part-093015-00001.parquet
db/ticks/date=2026-11-18/underlying=NIFTY/compacted.parquet   # after the close
```

- Partitioned by `date` and `underlying` (hive-style), so queries prune by date
  and retention/archival is "move or drop one folder".
- One primary sort, **no secondary indexes** — DuckDB row-group zone maps serve
  the scans (the Historify #1779 lesson), which keeps large tables from OOMing.
- After the close a compaction job merges the day's part-files into one sorted
  file per underlying. It runs at `TICKS_COMPACT_HOURS` (default 16:00 IST, after
  the equity close); if you capture MCX underlyings, move it past 23:30.
- **No automatic retention** — this is a keep-forever archive. At full depth it
  writes roughly 0.5-1 GB/day, so archive old day-folders off-box before the disk
  fills (see *Keeping it forever*).

## Enabling it

Set these in `.env` (all optional except the first; defaults shown):

| Key | Default | Meaning |
| --- | --- | --- |
| `TICKS_RECORDER_ENABLED` | `false` | Master switch |
| `TICKS_UNDERLYINGS` | `NIFTY:NSE_INDEX,BANKNIFTY:NSE_INDEX` | `UNDERLYING:QUOTE_EXCHANGE` list |
| `TICKS_STRIKES_AROUND_ATM` | `15` | Strikes each side of ATM (1-50) |
| `TICKS_FUTURES_MONTHS` | `2` | Near futures per underlying (0-3); 2 = current + next |
| `TICKS_MODE` | `Depth` | `LTP` / `Quote` / `Depth` |
| `TICKS_DATA_DIR` | `db/ticks` | Lake root |
| `TICKS_FLUSH_SECONDS` | `5` | Flush buffer every N seconds |
| `TICKS_FLUSH_ROWS` | `50000` | …or every N rows, whichever first |
| `TICKS_QUEUE_MAX` | `200000` | Bounded tick queue |
| `TICKS_COMPRESSION` | `zstd` | Parquet codec |
| `TICKS_COMPACT_HOURS` | `16` | Post-close compaction hour(s) IST |

Each underlying records only while its own market is open (from the market
calendar), exactly like the OI-flow recorder.

## Reading it back

**Resampled bars** (same shape as any other history source, so existing
backtests work unchanged):

```python
from services.history_service import get_history
ok, resp, status = get_history(
    symbol="NIFTY28NOV2524000CE", exchange="NFO", interval="1m",
    start_date="2026-11-18", end_date="2026-11-18", source="ticks",
)
# resp["data"] -> [{timestamp, open, high, low, close, volume, oi}, ...]
```

**Raw ticks** for tick-level backtests:

```python
from database import ticks_store
df = ticks_store.query_ticks("NIFTY28NOV2524000CE", "2026-11-18", "2026-11-18")
# full depth columns: ltp, oi, bid_px_1..5, bid_qty_1..5, ask_px_1..5, ask_qty_1..5, ts_exch, ts_recv, ...
```

Because DuckDB reads Parquet directly, an external read-only backtest process can
query the same lake concurrently while capture is running.

## Keeping it forever

A single 200 GB disk cannot hold full-depth ticks indefinitely. The intended
pattern is **tiering**: keep recent days hot on the server and copy older
day-folders to a second (free) box, where DuckDB can still query them. A folder
is a day, so archiving is a plain file move. (GitHub is not an option: its
file/repo/LFS limits and terms of service rule out bulk tick data.)

## Limitations

- Near (current-week) expiry only; add next-expiry capture by extending the
  selection in `services/tick_capture_service.py`.
- Greeks/IV not captured yet (schema-ready; needs an adapter change).
- `get_ohlcv` bars are UTC-interval aligned; 1-minute bars coincide with
  Historify, wider intervals may differ at the IST session offset.
