"""End-of-day data check against the exchange's own closing file.

For each underlying, compares the day's recorded open interest with NSE's
F&O bhavcopy and measures coverage (services/eod_data_check explains what is
held exact and what is only measured). Appends one row per underlying to
log/eod_data_check.csv and prints a summary.

Exit codes, for the scheduler that runs it after the close:
    0  checked, nothing fell short
    1  checked, something fell short (the sentences are printed under PROBLEMS)
    3  the exchange has not published the day's file yet; try again later

Usage:
    uv run python scripts/eod_data_check.py
    uv run python scripts/eod_data_check.py --day 2026-11-20 --underlying NIFTY BANKNIFTY
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import date, datetime

import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.eod_data_check import check, exchange_close, problems  # noqa: E402

OUT = os.path.join("log", "eod_data_check.csv")
IST = pytz.timezone("Asia/Kolkata")


def _row(report: dict) -> dict:
    rec, tic = report["sources"]
    both = report["records_agree"]
    return {
        "run_at": datetime.now(IST).isoformat(timespec="seconds"),
        "day": report["day"],
        "underlying": report["underlying"],
        "records_agree": f"{both['exact']}/{both['compared']}",
        "recorder_minutes_share": report.get("recorder_minutes_share"),
        "index_minutes_share": report.get("index_minutes_share"),
        "longest_gap_seconds": report.get("longest_gap_seconds"),
        "longest_gap_from": report.get("longest_gap_from"),
        "strikes_compared": rec.compared,
        "median_from_close": rec.median_share,
        "p90_from_close": rec.p90_share,
        "ticks_median_from_close": tic.median_share,
        "wild": len(rec.wild) + len(tic.wild),
        "unknown": len(rec.unknown) + len(tic.unknown),
        "problems": " | ".join(problems(report)),
    }


def summary(report: dict) -> str:
    """One line a trader can read: what matched, how far from the close, any gap."""
    rec = report["sources"][0]
    both = report["records_agree"]
    parts = [
        f"{report['underlying']} {report['day']}:",
        f"records agree on {both['exact']} of {both['compared']} strikes,",
    ]
    if rec.median_share is not None:
        parts.append(
            f"live OI {rec.median_share:.1%} from the exchange's close (typical), "
            f"{rec.p90_share:.1%} (9 in 10),"
        )
    cov = report.get("recorder_minutes_share")
    if cov is not None:
        parts.append(f"{cov:.1%} of minutes recorded,")
    gap = report.get("longest_gap_seconds")
    if gap is not None:
        at = report.get("longest_gap_from")
        parts.append(f"longest feed gap {gap:.0f}s" + (f" at {at}" if at else ""))
    return " ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--day", help="YYYY-MM-DD; default today in IST")
    ap.add_argument("--underlying", nargs="+", default=["NIFTY", "BANKNIFTY"])
    args = ap.parse_args()
    day = date.fromisoformat(args.day) if args.day else datetime.now(IST).date()

    exchange = exchange_close(day)
    if exchange is None:
        print(f"The exchange has not published the {day} closing file yet.")
        return 3

    rows, issues = [], []
    for underlying in args.underlying:
        report = check(underlying.upper(), day, exchange)
        print(summary(report))
        rows.append(_row(report))
        issues += problems(report)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    new = not os.path.exists(OUT)
    with open(OUT, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        if new:
            writer.writeheader()
        writer.writerows(rows)

    if issues:
        print("PROBLEMS")
        for line in issues:
            print(f"- {line}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
