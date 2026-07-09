#!/usr/bin/env python3
"""
15-day paper-test aggregate report.

Reads logs/breakout/daily_summary.csv (written by the live strategy each session and/or
by sector_replay.py) and prints the go/no-go dashboard: equity curve, % profitable days,
best/worst day, max drawdown across the period, per-trade expectancy, and a verdict scaled
to the capital you intend to deploy.

If a date appears more than once (e.g. replayed under different settings), the LAST row for
that date is used — so re-running a day overwrites the earlier figure in the report.

Usage:
    uv run python strategies/sector_report.py
    uv run python strategies/sector_report.py --from 2026-06-09 --to 2026-06-27 --capital 300000
"""

import argparse
import csv as _csv
import json
import os


def _spark(values) -> str:
    if not values:
        return ""
    blocks = "▁▂▃▄▅▆▇█"
    lo, hi = min(values), max(values)
    rng = (hi - lo) or 1
    return "".join(blocks[min(7, int((v - lo) / rng * 7))] for v in values)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", default="logs/breakout/daily_summary.csv")
    ap.add_argument("--from", dest="dfrom", default=None)
    ap.add_argument("--to", dest="dto", default=None)
    ap.add_argument("--capital", type=float, default=300000, help="capital to scale the verdict to")
    ap.add_argument("--risk-pct", type=float, default=2.0, help="max %% of capital you accept losing in a day")
    args = ap.parse_args()

    if not os.path.exists(args.summary):
        print(f"No summary file at {args.summary} — run the strategy (DRY_RUN) or sector_replay.py first.")
        return

    # Read, keep the LAST row per date, optional date filter
    by_date: dict = {}
    with open(args.summary, newline="") as f:
        for r in _csv.DictReader(f):
            d = r.get("date", "")
            if args.dfrom and d < args.dfrom:
                continue
            if args.dto and d > args.dto:
                continue
            by_date[d] = r
    days = [by_date[d] for d in sorted(by_date)]
    if not days:
        print("No days in range.")
        return

    def F(r, k):
        try:
            return float(r.get(k) or 0)
        except Exception:
            return 0.0

    nets   = [F(r, "net") for r in days]
    cum    = []
    c = 0.0
    for x in nets:
        c += x
        cum.append(c)
    total_net = cum[-1]
    n_days    = len(days)
    pos_days  = sum(1 for x in nets if x > 0)
    total_trades = int(sum(F(r, "n_trades") for r in days))
    total_wins   = int(sum(F(r, "wins") for r in days))
    peak_margin  = max((F(r, "peak_margin") for r in days), default=0)

    # Max drawdown across the whole equity curve
    run_peak = 0.0; max_dd = 0.0
    for v in cum:
        run_peak = max(run_peak, v)
        max_dd = min(max_dd, v - run_peak)

    avg_day   = total_net / n_days
    best_day  = max(nets); worst_day = min(nets)
    expectancy = total_net / total_trades if total_trades else 0
    win_rate  = total_wins / total_trades * 100 if total_trades else 0

    print("=" * 74)
    print(f"  SECTOR STRATEGY — {n_days}-DAY PAPER TEST REPORT  ({days[0]['date']} → {days[-1]['date']})")
    print("=" * 74)
    print(f"  {'DATE':<12} {'TRADES':>6} {'WIN%':>6} {'NET':>10} {'CUM':>11} {'maxDD':>9} {'pkMargin':>10}")
    print("  " + "-" * 70)
    for r, cu in zip(days, cum):
        print(f"  {r['date']:<12} {int(F(r,'n_trades')):>6} {F(r,'win_rate'):>5.0f}% "
              f"{F(r,'net'):>+10,.0f} {cu:>+11,.0f} {F(r,'max_drawdown_rs'):>9,.0f} "
              f"{F(r,'peak_margin'):>10,.0f}")
    print("  " + "-" * 70)
    print(f"  Equity curve: {_spark(cum)}")
    print()
    print(f"  Total net P&L        : Rs.{total_net:>+,.0f}")
    print(f"  Trading days         : {n_days}   profitable: {pos_days} ({pos_days/n_days*100:.0f}%)")
    print(f"  Avg / Best / Worst   : Rs.{avg_day:>+,.0f} / Rs.{best_day:>+,.0f} / Rs.{worst_day:>+,.0f}")
    print(f"  Trades / Win rate    : {total_trades}  /  {win_rate:.0f}%")
    print(f"  Expectancy per trade : Rs.{expectancy:>+,.0f}")
    print(f"  Max drawdown (period): Rs.{max_dd:>,.0f}")
    print(f"  Peak margin used     : Rs.{peak_margin:>,.0f}")
    print("=" * 74)

    # ── Verdict scaled to chosen capital ───────────────────────────────────────
    # The test ran at some peak margin; scale results to the user's capital.
    scale = (args.capital / peak_margin) if peak_margin else 1.0
    daily_loss_cap = args.capital * args.risk_pct / 100
    scaled_worst = worst_day * scale
    scaled_total = total_net * scale
    scaled_avg   = avg_day * scale

    print(f"  VERDICT (scaled to Rs.{args.capital:,.0f} capital, {scale:.1f}x test size)")
    print("  " + "-" * 70)
    print(f"  Projected total over period : Rs.{scaled_total:>+,.0f}")
    print(f"  Projected avg day           : Rs.{scaled_avg:>+,.0f}")
    print(f"  Projected WORST day         : Rs.{scaled_worst:>+,.0f}")
    print(f"  Your daily loss tolerance   : Rs.{-daily_loss_cap:>,.0f}  ({args.risk_pct:.0f}% of capital)")
    print()

    checks = []
    checks.append(("15+ trading days captured", n_days >= 15))
    checks.append(("Period net positive", total_net > 0))
    checks.append((">50% days profitable", pos_days / n_days > 0.5))
    checks.append(("Worst day within loss tolerance", scaled_worst >= -daily_loss_cap))
    checks.append(("Positive per-trade expectancy", expectancy > 0))
    for label, ok in checks:
        print(f"    [{'PASS' if ok else 'FAIL'}] {label}")
    go = all(ok for _, ok in checks)
    print()
    print(f"  {'>>> GO — data supports deploying at this size.' if go else '>>> NOT YET — keep paper-testing / reduce size.'}")
    print("=" * 74)


if __name__ == "__main__":
    main()
