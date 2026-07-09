#!/usr/bin/env python3
"""
Sector timeline → flat CSV for Excel study.

Reads a day's sector_snapshots_DATE.jsonl (captured every ~3 min) and flattens it into
one row per (timestamp, sector, stock), with BOTH within-sector rankings (by price change%
and by TF r_factor) plus 3-min delta columns. Open in Excel to see how sectors rotated and
which stocks led each sector through the day, and exactly what changed each interval.

Usage:
    uv run python strategies/sector_timeline.py --date 2026-06-09
    uv run python strategies/sector_timeline.py --range 2026-06-09:2026-06-27
    ... [--log-dir logs/breakout] [--rank-by chg|rf]   (rank-by only affects row sort order)
"""

import argparse
import csv as _csv
import importlib.util
import os
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))

_spec = importlib.util.spec_from_file_location(
    "strat", os.path.join(_HERE, "breakout_intraday_strategy.py"))
B = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(B)

FIELDS = [
    "date", "time", "sector", "sector_rank", "sector_rank_delta", "sector_avg_chg",
    "sector_breadth", "active", "stock", "change_pct", "change_delta",
    "rank_in_sector_chg", "rank_in_sector_chg_delta", "r_factor", "rank_in_sector_rf", "top4",
]


def _sector_members(snap: dict) -> dict[str, list[str]]:
    """Invert stock_sectors → {sector: [stocks...]} for one snapshot."""
    out: dict[str, list[str]] = {}
    for sym, secs in snap.get("stock_sectors", {}).items():
        for s in secs:
            out.setdefault(s, []).append(sym)
    return out


def export_day(date_str: str, log_dir: str, rank_by: str) -> str | None:
    snaps = B.load_sector_snapshots(date_str, log_dir)
    if not snaps:
        print(f"  {date_str}: no sector_snapshots — skipped")
        return None

    out_path = os.path.join(log_dir, f"sector_timeline_{date_str}.csv")
    rows_written = 0

    # State carried across snapshots for delta computation
    prev_sector_rank: dict[str, int] = {}
    prev_stock_chg:   dict[str, float] = {}
    prev_stock_rank_chg: dict[str, int] = {}   # keyed "sector|stock"

    with open(out_path, "w", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()

        for snap in snaps:
            ts        = snap.get("time", "")
            sec_ranks = snap.get("sector_ranks", {})
            sec_score = snap.get("sector_scores", {})
            breadth   = snap.get("sector_breadth", {})
            longs     = set(snap.get("long_sectors", []))
            shorts    = set(snap.get("short_sectors", []))
            top_n     = snap.get("sector_top_n", {})
            chg_map   = snap.get("stock_change_pct", {})
            rf_map    = snap.get("stock_rfactor", {})
            members   = _sector_members(snap)

            cur_stock_rank_chg: dict[str, int] = {}

            # Order sectors by their rank for tidy output
            for sector in sorted(sec_ranks, key=lambda s: sec_ranks.get(s, 999)):
                stocks = members.get(sector, [])
                if not stocks:
                    continue
                # Within-sector orderings
                by_chg = sorted(stocks, key=lambda s: chg_map.get(s, 0.0), reverse=True)
                by_rf  = sorted(stocks, key=lambda s: rf_map.get(s, 0.0), reverse=True)
                rank_chg = {s: i + 1 for i, s in enumerate(by_chg)}
                rank_rf  = {s: i + 1 for i, s in enumerate(by_rf)}

                s_rank = sec_ranks.get(sector)
                s_rank_delta = (s_rank - prev_sector_rank[sector]) if sector in prev_sector_rank else ""
                active = "LONG" if sector in longs else ("SHORT" if sector in shorts else "-")
                top4_set = set(top_n.get(sector, []))

                order = by_chg if rank_by == "chg" else by_rf
                for sym in order:
                    key = f"{sector}|{sym}"
                    chg = chg_map.get(sym, 0.0)
                    rcg = rank_chg[sym]
                    chg_delta = round(chg - prev_stock_chg[sym], 3) if sym in prev_stock_chg else ""
                    rcg_delta = (rcg - prev_stock_rank_chg[key]) if key in prev_stock_rank_chg else ""
                    cur_stock_rank_chg[key] = rcg
                    w.writerow({
                        "date": date_str, "time": ts, "sector": sector,
                        "sector_rank": s_rank, "sector_rank_delta": s_rank_delta,
                        "sector_avg_chg": sec_score.get(sector, ""),
                        "sector_breadth": breadth.get(sector, ""),
                        "active": active, "stock": sym,
                        "change_pct": chg, "change_delta": chg_delta,
                        "rank_in_sector_chg": rcg, "rank_in_sector_chg_delta": rcg_delta,
                        "r_factor": rf_map.get(sym, ""), "rank_in_sector_rf": rank_rf[sym],
                        "top4": "Y" if sym in top4_set else "",
                    })
                    rows_written += 1

            # Roll state forward
            prev_sector_rank = dict(sec_ranks)
            prev_stock_chg   = dict(chg_map)
            prev_stock_rank_chg = cur_stock_rank_chg

    print(f"  {date_str}: {len(snaps)} snapshots → {rows_written} rows → {out_path}")
    return out_path


def _daterange(start: str, end: str):
    d0 = datetime.strptime(start, "%Y-%m-%d")
    d1 = datetime.strptime(end, "%Y-%m-%d")
    while d0 <= d1:
        if d0.weekday() < 5:
            yield d0.strftime("%Y-%m-%d")
        d0 += timedelta(days=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--range", dest="rng", help="START:END inclusive (skips weekends)")
    ap.add_argument("--log-dir", default="logs/breakout")
    ap.add_argument("--rank-by", choices=["chg", "rf"], default="rf",
                    help="row sort order within each sector (both ranks are always columns)")
    args = ap.parse_args()

    print(f"Sector timeline export  (rank-by={args.rank_by})")
    print("=" * 60)
    if args.rng:
        a, b = args.rng.split(":")
        for ds in _daterange(a, b):
            export_day(ds, args.log_dir, args.rank_by)
    elif args.date:
        export_day(args.date, args.log_dir, args.rank_by)
    else:
        print("Provide --date YYYY-MM-DD or --range START:END")


if __name__ == "__main__":
    main()
