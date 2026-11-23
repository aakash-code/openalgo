"""
End-of-day data check: is what OpenAlgo recorded right, and how far is the
live feed from the exchange's own close?

After every close NSE publishes the F&O bhavcopy, one file with every
contract's official closing open interest (services/nse_oi_bhavcopy). This
check holds the day's recorded data to it, and to itself.

What can be exact, and what cannot (measured on NIFTY, 18-20 Nov 2026):

- **Our two records must agree exactly.** The OI Flow record (each option's
  last sample, which Writer Flow, its Totals and Market read are built from)
  and the tick store (each option's last tick) both carry the broker's feed.
  Any difference between them is a fault of ours.
- **The live feed never equals the official close exactly.** Equity
  derivatives trade to 15:40 (SEBI's Closing Auction Session, from
  3-Aug-2026, moved only the cash close; database/market_calendar_db), the
  broker's last OI update arrives about 15:39, and the official figure is
  settled after that. On those three days the last live OI sat a typical few
  percent from the close, up to 14% on one strike, and the next morning's
  first tick already carried the first update of the day (8-14 of 78 strikes
  exact). So the distance is measured and reported, and only a contract far
  off (WILD_SHARE) or unknown to the exchange (a mapping fault) is a problem.
- **Coverage**: the share of session minutes the OI Flow record holds, the
  minutes in which the index sent no tick, and the longest stretch with no
  tick at all across the underlying's contracts. The 20-Nov run found a
  124 s gap: a deploy's container swap.

`compare`, `agree` and `problems` are pure; the readers do the I/O.
"""

from __future__ import annotations

import glob
import os
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, time

import pytz

from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")
OPEN = time(9, 15)
# Options trade to 15:40; the index's own ticks stop at the 15:30 cash close.
OPTIONS_CLOSE = time(15, 40)
INDEX_CLOSE = time(15, 30)
# Only contracts still being read near the close are compared. Strikes enter
# and leave the watched window as the price moves; one that left at 11:00
# holds its 11:00 OI, which compared with the close read as a fault.
LAST_SEEN_FROM = time(15, 35)
# Further than this from the official close is a wrong contract or a dead
# quote, not the feed's last-minutes lag (whose worst was 14% on 20-Nov).
WILD_SHARE = 0.25
# Contracts holding less than this are too small for a share to mean anything:
# 130 against 65 (two lots against one) read as 100% off on 20-Nov.
WILD_MIN_OI = 100_000
# A tick stream this quiet across every contract of an underlying is a feed
# gap, not a quiet market: an ATM NIFTY option ticks about three times a second.
FEED_GAP_SECONDS = 30
# Shares below this are reported as a shortfall.
MIN_SHARE = 0.99
WORST_SHOWN = 5


def _session_minutes(close: time) -> int:
    return (close.hour * 60 + close.minute) - (OPEN.hour * 60 + OPEN.minute)


@dataclass
class SourceResult:
    """How one of our records stood against the exchange's close."""

    source: str
    compared: int = 0
    exact: int = 0
    # Absolute distance from the official close, as a share of it.
    median_share: float | None = None
    p90_share: float | None = None
    wild: list[dict] = field(default_factory=list)
    # Symbols we hold that the exchange's file does not carry: a mapping fault.
    unknown: list[str] = field(default_factory=list)
    worst: list[dict] = field(default_factory=list)


def compare(source: str, ours: dict[str, int], exchange: dict[str, float]) -> SourceResult:
    """Each of our symbols' last OI against the exchange's close. Pure."""
    out = SourceResult(source=source)
    shares: list[float] = []
    misses: list[dict] = []
    for symbol, value in ours.items():
        ref = exchange.get(symbol)
        if ref is None:
            out.unknown.append(symbol)
            continue
        out.compared += 1
        diff = int(value) - int(ref)
        if diff == 0:
            out.exact += 1
            shares.append(0.0)
            continue
        share = abs(diff) / ref if ref else 1.0
        shares.append(share)
        miss = {
            "symbol": symbol,
            "ours": int(value),
            "exchange": int(ref),
            "share": round(share, 4),
        }
        misses.append(miss)
        if share > WILD_SHARE and max(int(ref), int(value)) >= WILD_MIN_OI:
            out.wild.append(miss)
    if shares:
        ordered = sorted(shares)
        out.median_share = round(statistics.median(ordered), 4)
        out.p90_share = round(ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))], 4)
    misses.sort(key=lambda m: m["share"], reverse=True)
    out.worst = misses[:WORST_SHOWN]
    out.unknown.sort()
    return out


def agree(a: dict[str, int], b: dict[str, int]) -> dict:
    """How our two records agree on the contracts both hold. Pure."""
    both = sorted(set(a) & set(b))
    differ = [{"symbol": s, "recorder": a[s], "ticks": b[s]} for s in both if a[s] != b[s]]
    return {
        "compared": len(both),
        "exact": len(both) - len(differ),
        "share": round((len(both) - len(differ)) / len(both), 4) if both else None,
        "differ": differ[:WORST_SHOWN],
    }


def _is_option(symbol: str) -> bool:
    return symbol.endswith(("CE", "PE"))


def recorder_close(underlying: str, day: date) -> tuple[dict[str, int], float | None]:
    """The OI Flow record's last OI per option seen near the close, and its minute coverage."""
    from database.oi_flow_db import available_sessions, load_day

    sessions = [
        s
        for s in available_sessions(limit_days=60)
        if s["underlying"] == underlying and s["trade_date"] == day
    ]
    last: dict[str, tuple[int, datetime]] = {}
    minutes: set[datetime] = set()
    for s in sessions:
        for row in load_day(underlying, s["expiry"], day):
            oi = row.get("oi")
            t = row["sample_time"]
            if oi is not None and oi > 0:
                last[row["symbol"]] = (int(oi), t)
            if OPEN < t.time() <= OPTIONS_CLOSE:
                minutes.add(t.replace(second=0, microsecond=0))
    coverage = len(minutes) / _session_minutes(OPTIONS_CLOSE) if sessions else None
    close = {k: v for k, (v, t) in last.items() if _is_option(k) and t.time() >= LAST_SEEN_FROM}
    return close, coverage


def _day_ms(day: date, at: time) -> int:
    return int(IST.localize(datetime.combine(day, at)).timestamp() * 1000)


def ticks_close(underlying: str, day: date) -> tuple[dict[str, int], dict]:
    """The tick store's last OI per option seen near the close, and the day's feed gaps."""
    from database.ticks_store import _duck, _ql, data_dir

    path = os.path.join(
        data_dir(), f"date={day.isoformat()}", f"underlying={underlying}", "*.parquet"
    )
    if not glob.glob(path):
        return {}, {}
    open_ms = _day_ms(day, OPEN)
    opt_close_ms = _day_ms(day, OPTIONS_CLOSE)
    idx_close_ms = _day_ms(day, INDEX_CLOSE)
    src = f"read_parquet({_ql(path)}, union_by_name=true)"
    with _duck() as conn:
        conn.execute("SET threads=1")
        conn.execute("SET enable_progress_bar=false")
        rows = conn.execute(
            f"""SELECT symbol, arg_max(oi, ts_recv) FROM {src} WHERE oi > 0 GROUP BY 1
                HAVING max(ts_recv) >= {_day_ms(day, LAST_SEEN_FROM)}"""
        ).fetchall()
        gap, gap_end = conn.execute(
            f"""SELECT max(d), arg_max(ts_recv, d) FROM (
                  SELECT ts_recv, ts_recv - lag(ts_recv) OVER (ORDER BY ts_recv) AS d
                  FROM {src} WHERE ts_recv BETWEEN {open_ms} AND {opt_close_ms})"""
        ).fetchone()
        index_minutes = conn.execute(
            f"""SELECT count(DISTINCT ts_recv // 60000) FROM {src}
                WHERE symbol = {_ql(underlying)} AND ts_recv BETWEEN {open_ms} AND {idx_close_ms - 1}"""
        ).fetchone()[0]
    gap_at = (
        datetime.fromtimestamp((gap_end - gap) / 1000, IST).strftime("%H:%M:%S")
        if gap and gap_end
        else None
    )
    feed = {
        "longest_gap_seconds": round((gap or 0) / 1000, 1),
        "longest_gap_from": gap_at,
        "index_minutes_share": round(index_minutes / _session_minutes(INDEX_CLOSE), 4),
    }
    return {s: int(v) for s, v in rows if v is not None and _is_option(s)}, feed


def exchange_close(day: date) -> dict[str, float] | None:
    """The exchange's closing OI for every NSE option that day, or None if not yet published."""
    from services.nse_oi_bhavcopy import _download, _parse

    raw = _download(day)
    if raw is None:
        return None
    oi, _ = _parse(raw)
    return oi


def check(underlying: str, day: date, exchange: dict[str, float]) -> dict:
    """Every comparison and coverage figure for one underlying and day."""
    rec, rec_cov = recorder_close(underlying, day)
    tic, feed = ticks_close(underlying, day)
    return {
        "day": day.isoformat(),
        "underlying": underlying,
        "records_agree": agree(rec, tic),
        "recorder_minutes_share": round(rec_cov, 4) if rec_cov is not None else None,
        **feed,
        "sources": [compare("recorder", rec, exchange), compare("ticks", tic, exchange)],
    }


def problems(report: dict) -> list[str]:
    """Plain sentences for whatever fell short, for an alert. Empty when all is well."""
    out = []
    u = report["underlying"]
    names = {"recorder": "the OI Flow record", "ticks": "the tick store"}
    both = report["records_agree"]
    if both["compared"] and both["share"] < MIN_SHARE:
        out.append(
            f"{u}: the OI Flow record and the tick store disagree on "
            f"{both['compared'] - both['exact']} of {both['compared']} strikes at the close."
        )
    for r in report["sources"]:
        # Checked first: a record whose every symbol is unknown to the exchange
        # compares nothing, and "held nothing" would hide the mapping fault.
        if r.unknown:
            out.append(
                f"{u}: {len(r.unknown)} contracts in {names[r.source]} are not in the "
                f"exchange's file, such as {r.unknown[0]}."
            )
        if r.compared == 0:
            if not r.unknown:
                out.append(f"{u}: {names[r.source]} held nothing near the close to compare.")
            continue
        if r.wild:
            w = r.wild[0]
            out.append(
                f"{u}: {len(r.wild)} strikes in {names[r.source]} are more than "
                f"{WILD_SHARE:.0%} from the exchange's close, the worst {w['symbol']} "
                f"({w['ours']:,} against {w['exchange']:,})."
            )
    cov = report.get("recorder_minutes_share")
    if cov is not None and cov < MIN_SHARE:
        out.append(f"{u}: the OI Flow record holds {cov:.1%} of the session's minutes.")
    idx = report.get("index_minutes_share")
    if idx is not None and idx < MIN_SHARE:
        out.append(f"{u}: the index sent prices in {idx:.1%} of the session's minutes.")
    gap = report.get("longest_gap_seconds")
    if gap is not None and gap > FEED_GAP_SECONDS:
        at = report.get("longest_gap_from")
        out.append(
            f"{u}: the live feed went quiet for {gap:.0f} seconds" + (f" from {at}." if at else ".")
        )
    return out
