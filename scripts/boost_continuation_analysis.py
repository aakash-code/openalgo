"""
Intraday Boost "entry -> continuation" micro-analysis.

For every stock's FIRST appearance in a day's TradeFinder Intraday Boost
list (from the 5-min snapshot recorder's DuckDB store, db/tf_boost_snapshots.duckdb),
measures what price did next: did it keep trending in the direction it was
already moving (continuation), or fade/reverse? Bucketed by time-of-day and
by score/rank at entry, pooled across every day the recorder has captured,
to answer "when in the day is it actually worth picking a Boost entrant, and
does a higher score/rank mean better follow-through."

One-off analysis script, console output only (no file/DB writes).

Usage:
    OA_API_KEY=<key> .venv/bin/python3 scripts/boost_continuation_analysis.py
    OA_API_KEY=<key> OA_HOST_URL=http://127.0.0.1:5000 .venv/bin/python3 scripts/boost_continuation_analysis.py
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime
from statistics import median

import duckdb
import requests

API_KEY = os.environ.get("OA_API_KEY", "")
HOST = os.environ.get("OA_HOST_URL", "http://127.0.0.1:5000")
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "tf_boost_snapshots.duckdb")

CONTINUATION_THRESHOLD_PCT = 0.5   # further move, same direction, within horizon
REVERSAL_THRESHOLD_PCT = 0.3       # move against entry direction
HORIZONS_MIN = [15, 30, 60]        # + "EOD" handled separately
REQUEST_DELAY_S = 0.2

TIME_BUCKETS = [
    ("09:15-09:30", 9 * 60 + 15, 9 * 60 + 30),
    ("09:30-10:00", 9 * 60 + 30, 10 * 60),
    ("10:00-10:30", 10 * 60, 10 * 60 + 30),
    ("10:30-11:00", 10 * 60 + 30, 11 * 60),
    ("11:00-12:00", 11 * 60, 12 * 60),
    ("12:00-13:00", 12 * 60, 13 * 60),
    ("13:00-14:00", 13 * 60, 14 * 60),
    ("14:00-15:00", 14 * 60, 15 * 60),
    ("15:00+", 15 * 60, 24 * 60),
]

if not API_KEY:
    print("ERROR: set OA_API_KEY=<your-key> before running", file=sys.stderr)
    sys.exit(1)


@dataclass
class EntryEvent:
    snapshot_date: Date
    symbol: str
    entry_time: datetime
    rank: int
    score: float
    change_pct: float
    ltp: float


@dataclass
class EntryResult:
    ev: EntryEvent
    time_bucket: str
    score_tercile: str
    rank_bucket: str
    # horizon_label -> forward %move (entry-direction-adjusted), or None if no data
    fwd_move: dict[str, float | None]
    fwd_class: dict[str, str]  # 'continuation' | 'reversal' | 'flat' | 'no_data'
    mfe: dict[str, float | None]
    mae: dict[str, float | None]


# ── Step 1: pull first-entry events from DuckDB ─────────────────────────────


def load_first_entries() -> list[EntryEvent]:
    con = duckdb.connect(DB_PATH, read_only=True)
    rows = con.execute(
        """
        SELECT s.snapshot_date, s.symbol, s.snapshot_time, s.rank, s.score, s.change_pct, s.ltp
        FROM tf_boost_snapshots s
        INNER JOIN (
            SELECT snapshot_date, symbol, MIN(snapshot_time) AS first_time
            FROM tf_boost_snapshots
            WHERE list_type = 'intraday_boost'
            GROUP BY 1, 2
        ) f ON s.snapshot_date = f.snapshot_date
           AND s.symbol = f.symbol
           AND s.snapshot_time = f.first_time
        WHERE s.list_type = 'intraday_boost'
        ORDER BY s.snapshot_date, s.snapshot_time
        """
    ).fetchall()
    con.close()
    events = []
    for snapshot_date, symbol, snapshot_time, rank, score, change_pct, ltp in rows:
        if change_pct is None or abs(change_pct) < 1e-9:
            continue  # no directional bias to test
        events.append(
            EntryEvent(
                snapshot_date=snapshot_date,
                symbol=symbol,
                entry_time=snapshot_time,
                rank=rank,
                score=score or 0.0,
                change_pct=change_pct,
                ltp=ltp or 0.0,
            )
        )
    return events


def time_bucket_for(entry_time: datetime) -> str:
    minutes = entry_time.hour * 60 + entry_time.minute
    for label, start, end in TIME_BUCKETS:
        if start <= minutes < end:
            return label
    return TIME_BUCKETS[-1][0]


def rank_bucket_for(rank: int) -> str:
    if rank <= 5:
        return "1-5"
    if rank <= 15:
        return "6-15"
    return "16+"


# ── Step 2: fetch candles ────────────────────────────────────────────────────


def fetch_candles(symbol: str, date_str: str) -> list[dict]:
    try:
        resp = requests.post(
            f"{HOST}/api/v1/history",
            json={
                "apikey": API_KEY,
                "symbol": symbol,
                "exchange": "NSE",
                "interval": "5m",
                "start_date": date_str,
                "end_date": date_str,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        data = resp.json().get("data") or []
        candles = []
        for d in data:
            ts = d.get("timestamp")
            if ts is None:
                dt_str = d.get("date") or d.get("datetime")
                if not dt_str:
                    continue
                ts = datetime.fromisoformat(str(dt_str).replace("Z", "+00:00")).timestamp()
            try:
                o, h, l, c = float(d["open"]), float(d["high"]), float(d["low"]), float(d["close"])
            except (KeyError, TypeError, ValueError):
                continue
            candles.append({"time": float(ts), "open": o, "high": h, "low": l, "close": c})
        candles.sort(key=lambda c: c["time"])
        return candles
    except Exception:
        return []


# ── Step 3: forward-move measurement ─────────────────────────────────────────


def classify(move_pct: float) -> str:
    if move_pct >= CONTINUATION_THRESHOLD_PCT:
        return "continuation"
    if move_pct <= -REVERSAL_THRESHOLD_PCT:
        return "reversal"
    return "flat"


def analyze_entry(ev: EntryEvent, candles: list[dict]) -> EntryResult | None:
    if not candles:
        return None
    entry_ts = ev.entry_time.timestamp()
    # candle at/just after entry_time
    entry_idx = None
    for i, c in enumerate(candles):
        if c["time"] >= entry_ts - 1:
            entry_idx = i
            break
    if entry_idx is None:
        return None
    entry_price = candles[entry_idx]["close"]
    direction = 1 if ev.change_pct > 0 else -1

    fwd_move: dict[str, float | None] = {}
    fwd_class: dict[str, str] = {}
    mfe: dict[str, float | None] = {}
    mae: dict[str, float | None] = {}

    def compute(label: str, end_idx: int) -> None:
        window = candles[entry_idx : end_idx + 1]
        if len(window) < 2 or entry_price == 0:
            fwd_move[label] = None
            fwd_class[label] = "no_data"
            mfe[label] = None
            mae[label] = None
            return
        last_close = window[-1]["close"]
        move_pct = direction * (last_close - entry_price) / entry_price * 100
        fwd_move[label] = move_pct
        fwd_class[label] = classify(move_pct)
        best = max(direction * (c["high"] - entry_price) / entry_price * 100 for c in window)
        worst = min(direction * (c["low"] - entry_price) / entry_price * 100 for c in window)
        mfe[label] = best
        mae[label] = worst

    bars_per_min = 1 / 5  # 5m candles
    for horizon in HORIZONS_MIN:
        end_idx = min(entry_idx + round(horizon * bars_per_min), len(candles) - 1)
        compute(f"{horizon}m", end_idx)
    compute("EOD", len(candles) - 1)

    return EntryResult(
        ev=ev,
        time_bucket=time_bucket_for(ev.entry_time),
        score_tercile="",  # filled in later (needs cross-day percentile)
        rank_bucket=rank_bucket_for(ev.rank),
        fwd_move=fwd_move,
        fwd_class=fwd_class,
        mfe=mfe,
        mae=mae,
    )


# ── Step 4: bucketing / aggregation ──────────────────────────────────────────


def assign_score_terciles(events: list[EntryEvent]) -> dict[tuple, str]:
    """Per-day score tercile assignment, keyed by (snapshot_date, symbol, entry_time)."""
    by_day: dict[Date, list[EntryEvent]] = {}
    for ev in events:
        by_day.setdefault(ev.snapshot_date, []).append(ev)
    tercile_map: dict[tuple, str] = {}
    for day_events in by_day.values():
        sorted_evs = sorted(day_events, key=lambda e: e.score, reverse=True)
        n = len(sorted_evs)
        for i, ev in enumerate(sorted_evs):
            if i < n / 3:
                t = "Top"
            elif i < 2 * n / 3:
                t = "Mid"
            else:
                t = "Bottom"
            tercile_map[(ev.snapshot_date, ev.symbol, ev.entry_time)] = t
    return tercile_map


HORIZON_LABELS = [f"{h}m" for h in HORIZONS_MIN] + ["EOD"]


def summarize(results: list[EntryResult], horizon: str) -> dict:
    valid = [r for r in results if r.fwd_class[horizon] != "no_data"]
    n = len(valid)
    if n == 0:
        return {"n": 0}
    cont = sum(1 for r in valid if r.fwd_class[horizon] == "continuation")
    rev = sum(1 for r in valid if r.fwd_class[horizon] == "reversal")
    moves = [r.fwd_move[horizon] for r in valid if r.fwd_move[horizon] is not None]
    return {
        "n": n,
        "cont_pct": cont / n * 100,
        "rev_pct": rev / n * 100,
        "avg_move": sum(moves) / len(moves) if moves else 0.0,
        "median_move": median(moves) if moves else 0.0,
    }


def print_table(title: str, rows: list[tuple[str, list[EntryResult]]]) -> None:
    print()
    print("━" * 130)
    print(title)
    print("━" * 130)
    header = f"{'Bucket':<16}{'n':>6}" + "".join(
        f"{h + ' cont%':>14}{h + ' avgMv%':>14}" for h in HORIZON_LABELS
    )
    print(header)
    print("-" * 130)
    for label, group in rows:
        if not group:
            continue
        cells = ""
        n_overall = len(group)
        for h in HORIZON_LABELS:
            s = summarize(group, h)
            if s["n"] == 0:
                cells += f"{'--':>14}{'--':>14}"
            else:
                cells += f"{s['cont_pct']:>13.1f}%{s['avg_move']:>13.2f}%"
        print(f"{label:<16}{n_overall:>6}{cells}")


def main() -> None:
    print(f"Loading first-entry events from {DB_PATH} ...")
    events = load_first_entries()
    print(f"{len(events)} directional first-entry events found across all recorded days.")
    if not events:
        print("No data to analyze.")
        return

    tercile_map = assign_score_terciles(events)

    results: list[EntryResult] = []
    candle_cache: dict[tuple[str, str], list[dict]] = {}
    total = len(events)
    for i, ev in enumerate(events, 1):
        date_str = ev.snapshot_date.isoformat()
        key = (ev.symbol, date_str)
        if key not in candle_cache:
            candle_cache[key] = fetch_candles(ev.symbol, date_str)
            time.sleep(REQUEST_DELAY_S)
        candles = candle_cache[key]
        res = analyze_entry(ev, candles)
        if res is not None:
            res.score_tercile = tercile_map.get((ev.snapshot_date, ev.symbol, ev.entry_time), "Mid")
            results.append(res)
        if i % 25 == 0 or i == total:
            print(f"  ... processed {i}/{total} events, {len(results)} usable so far", file=sys.stderr)

    print(f"\n{len(results)} entries had usable forward candle data.")

    # ── By time-of-day bucket ──
    by_time = [(label, [r for r in results if r.time_bucket == label]) for label, _, _ in TIME_BUCKETS]
    print_table("BY TIME-OF-DAY (entry time)", by_time)

    # ── By score tercile ──
    by_score = [(t, [r for r in results if r.score_tercile == t]) for t in ["Top", "Mid", "Bottom"]]
    print_table("BY SCORE TERCILE (within-day rank of TradeFinder score at entry)", by_score)

    # ── By rank bucket ──
    by_rank = [(b, [r for r in results if r.rank_bucket == b]) for b in ["1-5", "6-15", "16+"]]
    print_table("BY LIST-RANK BUCKET (position in the Boost list at entry)", by_rank)

    # ── Cross-tab: time bucket x score tercile (60m horizon), n>=15 only ──
    print()
    print("━" * 130)
    print("CROSS-TAB: time-of-day x score tercile — 60m continuation rate (cells with n>=15 only)")
    print("━" * 130)
    print(f"{'Time bucket':<16}{'Top':>16}{'Mid':>16}{'Bottom':>16}")
    for label, _, _ in TIME_BUCKETS:
        row = f"{label:<16}"
        for tercile in ["Top", "Mid", "Bottom"]:
            cell = [r for r in results if r.time_bucket == label and r.score_tercile == tercile]
            s = summarize(cell, "60m")
            if s["n"] < 15:
                row += f"{'n=' + str(s['n']):>16}"
            else:
                row += f"{s['cont_pct']:>13.1f}% "
        print(row)

    # ── Closing summary ──
    print()
    print("━" * 130)
    print("SUMMARY")
    print("━" * 130)
    best_time = max(
        (label for label, _, _ in TIME_BUCKETS),
        key=lambda label: summarize([r for r in results if r.time_bucket == label], "60m").get("cont_pct", -1)
        if summarize([r for r in results if r.time_bucket == label], "60m")["n"] >= 15
        else -1,
    )
    best_score = max(
        ["Top", "Mid", "Bottom"],
        key=lambda t: summarize([r for r in results if r.score_tercile == t], "60m").get("cont_pct", -1),
    )
    days = sorted({ev.snapshot_date.isoformat() for ev in events})
    print(f"Pooled across {len(days)} trading days: {', '.join(days)}")
    print(f"Best time-of-day bucket (60m continuation rate, n>=15 cells only): {best_time}")
    print(f"Best score tercile (60m continuation rate): {best_score}")
    print(
        "Caveat: this is 9 trading days of one market regime — a real pattern worth using as a "
        "starting heuristic, not a statistical guarantee. Re-run periodically as more days accumulate."
    )


if __name__ == "__main__":
    main()
