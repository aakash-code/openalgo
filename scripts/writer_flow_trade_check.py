"""Does reading the trades make Writer Flow more useful? A day-by-day check.

For each recorded day that has both an OI Flow record and ticks, Writer Flow
is read three ways: premium net of the index move (the default), raw premium,
and trades (services.tick_trade_flow). For each reading, the writers'
near-the-money pressure over the last 15 minutes is compared with the index's
move over the next 15 and 30 minutes:

- ic15 / ic30: Spearman rank correlation of that signal with the move ahead
  (0 is no use; it is read across many days, never from one).
- hit15 / hit30: how often the signal's sign matched the move's, over minutes
  where both were non-zero.

The minutes overlap, so one day's numbers are not independent samples. Judge
the readings by how they compare day after day, over 20-30 sessions, not by
any single day. Each run appends its rows to log/writer_flow_trade_check.csv.

Usage:
    uv run python scripts/writer_flow_trade_check.py --underlying NIFTY
    uv run python scripts/writer_flow_trade_check.py --underlying NIFTY --day 2026-11-20
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from database.oi_flow_db import available_sessions, load_day  # noqa: E402
from database.ticks_store import available_days  # noqa: E402
from services.oi_flow_analysis import FlowSettings, analyse  # noqa: E402
from services.tick_trade_flow import minute_trades  # noqa: E402

LOOKBACK = 15
HORIZONS = (15, 30)
OUT = os.path.join("log", "writer_flow_trade_check.csv")


def _scores(out: dict) -> dict:
    trend = out.get("trend") or {}
    spots = [t.get("spot") for t in out["totals"]]
    pressure = trend.get("pressure") or []
    n = min(len(spots), len(pressure))
    frame = pd.DataFrame({"spot": spots[:n], "p": pressure[:n]}).ffill()
    frame["signal"] = frame["p"].rolling(LOOKBACK, min_periods=LOOKBACK).sum()
    row = {}
    for h in HORIZONS:
        ahead = frame["spot"].shift(-h) - frame["spot"]
        both = pd.DataFrame({"s": frame["signal"], "a": ahead}).dropna()
        row[f"ic{h}"] = (
            round(both["s"].corr(both["a"], method="spearman"), 3) if len(both) > 30 else None
        )
        signed = both[(both["s"] != 0) & (both["a"] != 0)]
        row[f"hit{h}"] = (
            round(float(((signed["s"] > 0) == (signed["a"] > 0)).mean()), 3)
            if len(signed) > 30
            else None
        )
        row[f"n{h}"] = len(both)
    return row


def check_day(underlying: str, day: date) -> list[dict]:
    held = [
        s for s in available_sessions() if s["underlying"] == underlying and s["trade_date"] == day
    ]
    if not held:
        return []
    samples = load_day(underlying, held[0]["expiry"], day)
    trades = minute_trades(underlying, day)
    readings = [
        ("premium_net", FlowSettings(step_seconds=60), None),
        ("premium_raw", FlowSettings(step_seconds=60, net_of_underlying=False), None),
    ]
    if trades:
        readings.append(("trades", FlowSettings(step_seconds=60, read_trades=True), trades))
    rows = []
    for name, settings, t in readings:
        out = analyse(samples, settings, trades=t)
        stats = out.get("trades") or {}
        rows.append(
            {
                "run_at": datetime.now().isoformat(timespec="seconds"),
                "day": day.isoformat(),
                "underlying": underlying,
                "reading": name,
                "agree_with_premium": stats.get("agree_share"),
                "steps_decided_by_trades": stats.get("decided"),
                **_scores(out),
            }
        )
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--underlying", default="NIFTY")
    ap.add_argument("--day", help="YYYY-MM-DD; default every day with ticks")
    args = ap.parse_args()
    days = (
        [date.fromisoformat(args.day)]
        if args.day
        else [date.fromisoformat(d) for d in available_days()]
    )
    rows = [r for d in days for r in check_day(args.underlying.upper(), d)]
    if not rows:
        print("No day has both an OI Flow record and ticks for", args.underlying)
        return
    print(pd.DataFrame(rows).drop(columns=["run_at"]).to_string(index=False))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    new = not os.path.exists(OUT)
    with open(OUT, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        if new:
            w.writeheader()
        w.writerows(rows)
    print(f"\nAppended {len(rows)} rows to {OUT}")


if __name__ == "__main__":
    main()
