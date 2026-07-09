#!/usr/bin/env python
"""
Risk-overlay comparison for the Survivor strategy — FULL 5 YEARS (incl. the
June-2024 crash, the worst event in the data).

Runs four scenarios as subprocesses and compares net P&L, Sharpe, max drawdown,
worst single day, return skew, and the worst-week (Jun-2024) damage. The point:
does a per-leg stop-loss + daily kill-switch tame the -Rs 43L tail while keeping
most of the profit? If so, the strategy can be sized up to Rs 2 Cr responsibly.

Run:  uv run python backtesting/survivor/survivor_risk_compare.py
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ENGINE = HERE / "survivor_backtest.py"

SCENARIOS = {
    "1. baseline (no overlay)":     {},
    "2. stop-loss 2x":              {"STOP_LOSS_MULT": "2.0"},
    "3. kill-switch Rs4L/day":      {"DAILY_LOSS_CAP": "400000"},
    "4. stop 2x + kill Rs4L":       {"STOP_LOSS_MULT": "2.0", "DAILY_LOSS_CAP": "400000"},
}

JUN24 = (pd.Timestamp("2024-06-03"), pd.Timestamp("2024-06-10"))   # election week


def analyse():
    eq = pd.read_csv(HERE / "survivor_equity.csv", parse_dates=["ts"]) \
        .sort_values("ts").reset_index(drop=True)
    eq["dp"] = eq["equity"].diff().fillna(eq["equity"].iloc[0])
    tr = pd.read_csv(HERE / "survivor_trades.csv")
    net = eq["equity"].iloc[-1]
    sharpe = eq["dp"].mean() / eq["dp"].std(ddof=1) * np.sqrt(252)
    maxdd = (eq["equity"] - eq["equity"].cummax()).min()
    worst_day = eq["dp"].min()
    skew = eq["dp"].skew()
    jun = eq[(eq["ts"] >= JUN24[0]) & (eq["ts"] <= JUN24[1])]["dp"].sum()
    stops = int((tr["reason"] == "stop").sum()) if "reason" in tr else 0
    kills = int((tr["reason"] == "kill").sum()) if "reason" in tr else 0
    return dict(net=net, sharpe=sharpe, maxdd=maxdd, worst_day=worst_day,
                skew=skew, jun24_week=jun, stops=stops, kills=kills)


def main():
    rows = []
    for label, ov in SCENARIOS.items():
        env = {**os.environ, **ov}
        env.pop("START_DATE", None)            # force FULL history
        env.pop("END_DATE", None)
        print(f"running: {label} ...", flush=True)
        subprocess.run(["uv", "run", "python", str(ENGINE)], env=env,
                       cwd=HERE.parents[1], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        m = analyse()
        m["scenario"] = label
        rows.append(m)

    df = pd.DataFrame(rows)[["scenario", "net", "sharpe", "maxdd", "worst_day",
                             "skew", "jun24_week", "stops", "kills"]]
    fmt = {c: "{:,.0f}".format for c in ["net", "maxdd", "worst_day", "jun24_week"]}
    fmt.update({"sharpe": "{:.2f}".format, "skew": "{:+.2f}".format,
                "stops": "{:,.0f}".format, "kills": "{:,.0f}".format})
    pd.set_option("display.width", 220, "display.max_columns", 20)
    print("\n" + "=" * 120)
    print("  RISK-OVERLAY COMPARISON — FULL 5 YEARS (NIFTY weekly, base size)")
    print("=" * 120)
    print(df.to_string(index=False, formatters=fmt))
    print("=" * 120)
    print("  net=5yr net P&L | maxdd=max MTM drawdown | worst_day=worst single day")
    print("  jun24_week=net P&L across the Jun-2024 election week (the tail event)")
    df.to_csv(HERE / "survivor_risk_compare.csv", index=False)
    print(f"  saved -> {HERE / 'survivor_risk_compare.csv'}")


if __name__ == "__main__":
    main()
