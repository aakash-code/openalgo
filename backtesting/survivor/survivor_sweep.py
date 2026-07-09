#!/usr/bin/env python
"""
Parameter sweep for the Survivor strategy (2-year window by default).

Runs the engine as a subprocess for each config, then reads survivor_equity.csv
to compute net P&L, Sharpe, max drawdown, peak margin and capital efficiency.
Goal: find configs that raise profit PER UNIT of capital/risk — the honest route
toward a higher absolute target (e.g. Rs 2 Cr) rather than brute size.

Run:  uv run python backtesting/survivor/survivor_sweep.py
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ENGINE = HERE / "survivor_backtest.py"
WINDOW = {"START_DATE": "2024-07-01"}      # 2-year window
BASE = dict(PE_GAP="20", CE_GAP="20", PE_SYMBOL_GAP="200", CE_SYMBOL_GAP="200",
            MIN_PRICE_TO_SELL="15")

# label -> overrides on top of BASE
CONFIGS = {
    "base (g20/s200/m15)":     {},
    "closer strikes s150":     {"PE_SYMBOL_GAP": "150", "CE_SYMBOL_GAP": "150"},
    "further strikes s250":    {"PE_SYMBOL_GAP": "250", "CE_SYMBOL_GAP": "250"},
    "tight trigger g15":       {"PE_GAP": "15", "CE_GAP": "15"},
    "loose trigger g30":       {"PE_GAP": "30", "CE_GAP": "30"},
    "low min-prem m10":        {"MIN_PRICE_TO_SELL": "10"},
    "closer+tight s150/g15":   {"PE_SYMBOL_GAP": "150", "CE_SYMBOL_GAP": "150",
                                "PE_GAP": "15", "CE_GAP": "15"},
}


def metrics():
    eq = pd.read_csv(HERE / "survivor_equity.csv", parse_dates=["ts"]) \
        .sort_values("ts").reset_index(drop=True)
    eq["dp"] = eq["equity"].diff().fillna(eq["equity"].iloc[0])
    years = max((eq["ts"].iloc[-1] - eq["ts"].iloc[0]).days / 365.25, 1e-9)
    net = eq["equity"].iloc[-1]
    ann = net / years
    sharpe = eq["dp"].mean() / eq["dp"].std(ddof=1) * np.sqrt(252)
    maxdd = (eq["equity"] - eq["equity"].cummax()).min()
    peak = eq["margin"].max()
    worst = eq["dp"].min()
    return net, ann, sharpe, maxdd, peak, worst, ann / peak if peak else 0


def main():
    rows = []
    for label, ov in CONFIGS.items():
        env = {**os.environ, **WINDOW, **BASE, **ov}
        print(f"running: {label} ...", flush=True)
        subprocess.run(["uv", "run", "python", str(ENGINE)], env=env,
                       cwd=HERE.parents[1], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        net, ann, sharpe, maxdd, peak, worst, roc = metrics()
        size_for_2cr = 2e7 / net                      # qty multiple to reach Rs 2 Cr
        rows.append({
            "config": label, "net_2yr": net, "sharpe": sharpe,
            "max_dd": maxdd, "worst_day": worst, "peak_margin": peak,
            "ann_ret_on_peak_%": 100 * roc, "x_size_for_2cr": size_for_2cr,
            "capital_for_2cr": peak * size_for_2cr,
        })

    df = pd.DataFrame(rows).sort_values("ann_ret_on_peak_%", ascending=False)
    pd.set_option("display.width", 220, "display.max_columns", 20)
    fmt = {c: "{:,.0f}".format for c in
           ["net_2yr", "max_dd", "worst_day", "peak_margin", "capital_for_2cr"]}
    fmt.update({"sharpe": "{:.2f}".format, "ann_ret_on_peak_%": "{:.1f}".format,
                "x_size_for_2cr": "{:.2f}".format})
    print("\n" + "=" * 120)
    print("  SURVIVOR PARAMETER SWEEP — 2-year window (sorted by capital efficiency)")
    print("=" * 120)
    print(df.to_string(index=False, formatters=fmt))
    print("=" * 120)
    print("  x_size_for_2cr = quantity multiple to reach Rs 2 Cr in 2 yrs")
    print("  capital_for_2cr = peak margin AT that size (the capital the HNI must fund)")
    df.to_csv(HERE / "survivor_sweep_results.csv", index=False)
    print(f"  saved -> {HERE / 'survivor_sweep_results.csv'}")


if __name__ == "__main__":
    main()
do 