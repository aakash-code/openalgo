#!/usr/bin/env python
"""Magnetic Zones options-selling backtest — full 12-config matrix driver.

Runs every combination of:
    entry      : range_fade | touch_fade
    structure  : naked      | hedged
    timeframe  : daily_intraday | weekly_overnight | monthly_overnight

Each config is reported in FAITHFUL and REALISTIC modes. Capital is fixed at
Rs 1 Cr with fixed lots (ROI is reported against the 1 Cr base).

Usage:
    uv run python -m backtesting.magnetic_zones_options.run_all
Env overrides:
    START=2024-04-01  END=2026-06-09  LOTS=10  SLIPPAGE_PTS=1.0
    HEDGE_WIDTH=200   TP_PCT=0.5      SL_MULT=2.0   ONLY=range_fade|hedged|W-ovn
"""

from __future__ import annotations

import json
import math
import os
from itertools import product
from pathlib import Path

import pandas as pd

from .data_loader import DataLoader
from .engine import Config, run

OUT_DIR = Path(__file__).resolve().parent / "results"
CAPITAL = 1e7

START = os.environ.get("START", "2024-04-01")
END = os.environ.get("END", "2026-06-09")
LOTS = int(os.environ.get("LOTS", 10))
HEDGE_WIDTH = int(os.environ.get("HEDGE_WIDTH", 200))
TP_PCT = float(os.environ.get("TP_PCT", 0.50))
SL_MULT = float(os.environ.get("SL_MULT", 2.0))
ONLY = os.environ.get("ONLY", "")  # comma-substring filter on config name


def summarize(trades: list[dict]) -> dict | None:
    if not trades:
        return None
    df = pd.DataFrame(trades)
    wins = df[df["win"]]
    losses = df[~df["win"]]
    net = df["pnl"].sum()
    # Equity ordered by exit day; drawdown + annualised Sharpe from daily P&L.
    df = df.copy()
    df["exit_day"] = pd.to_datetime(df["exit_day"], errors="coerce")
    daily = df.groupby("exit_day")["pnl"].sum().sort_index()
    cum = daily.cumsum()
    max_dd = float((cum - cum.cummax()).min()) if len(cum) else 0.0
    sharpe = (daily.mean() / daily.std() * math.sqrt(252)) if daily.std() else 0.0
    pf = (wins["pnl"].sum() / abs(losses["pnl"].sum())) if len(losses) and losses["pnl"].sum() != 0 else None
    return {
        "trades": len(df),
        "win_pct": round(len(wins) / len(df) * 100, 1),
        "net": round(net, 0),
        "roi_pct": round(net / CAPITAL * 100, 1),
        "pf": round(pf, 2) if pf is not None else None,
        "max_dd": round(max_dd, 0),
        "avg": round(df["pnl"].mean(), 0),
        "sharpe": round(sharpe, 2),
    }


def equity_html(name: str, trades: list[dict], path: Path):
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception:
        return
    df = pd.DataFrame(trades)
    if df.empty:
        return
    df["exit_day"] = pd.to_datetime(df["exit_day"], errors="coerce")
    daily = df.groupby("exit_day")["pnl"].sum().sort_index()
    eq = CAPITAL + daily.cumsum()
    dd = eq - eq.cummax()
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True,
                        row_heights=[0.5, 0.25, 0.25],
                        subplot_titles=("Equity (Rs)", "Drawdown (Rs)", "Per-trade P&L (Rs)"))
    fig.add_trace(go.Scatter(x=eq.index, y=eq.values, name="Equity"), row=1, col=1)
    fig.add_trace(go.Scatter(x=dd.index, y=dd.values, fill="tozeroy", name="Drawdown"), row=2, col=1)
    fig.add_trace(go.Bar(x=pd.to_datetime(df["exit_day"]), y=df["pnl"], name="Trade P&L"), row=3, col=1)
    fig.update_layout(title=f"Magnetic Zones — {name} (REALISTIC)", height=850, showlegend=False)
    fig.write_html(str(path))


def main():
    OUT_DIR.mkdir(exist_ok=True)
    print(f"Window {START} -> {END} | lots={LOTS} | hedge_width={HEDGE_WIDTH} | "
          f"TP={TP_PCT:.0%} | SL={SL_MULT}x | capital Rs{CAPITAL:,.0f}")
    loader = DataLoader()
    try:
        print(f"[DB] expiries: {len(loader.weekly_expiries)} weekly, "
              f"{len(loader.monthly_expiries)} monthly | "
              f"spot days: {len(loader.trading_days(START, END))}\n")

        entries = ["range_fade", "touch_fade"]
        structures = ["naked", "hedged"]
        timeframes = ["daily_intraday", "weekly_overnight", "monthly_overnight"]

        summary = {}
        rows = []
        for e, s, tf in product(entries, structures, timeframes):
            cfg = Config(entry=e, structure=s, timeframe=tf, lots=LOTS,
                         hedge_width_pts=HEDGE_WIDTH, tp_pct=TP_PCT, sl_mult=SL_MULT)
            if ONLY and not all(tok in cfg.name for tok in ONLY.split(",")):
                continue
            faithful, realistic, skips = run(cfg, loader, START, END)
            sf = summarize(faithful)
            sr = summarize(realistic)
            summary[cfg.name] = {"faithful": sf, "realistic": sr, "skips": skips}
            safe = cfg.name.replace("|", "_")
            if realistic:
                pd.DataFrame(realistic).to_csv(OUT_DIR / f"mz_trades_{safe}.csv", index=False)
                equity_html(cfg.name, realistic, OUT_DIR / f"mz_equity_{safe}.html")
            if sr:
                rows.append({"config": cfg.name, **{f"R_{k}": v for k, v in sr.items()},
                             "F_net": sf["net"] if sf else None,
                             "F_win%": sf["win_pct"] if sf else None})
            print(f"  {cfg.name:34s} | realistic: "
                  + (f"{sr['trades']:>4d} tr  win {sr['win_pct']:>5.1f}%  "
                     f"net Rs{sr['net']:>13,.0f}  ROI {sr['roi_pct']:>6.1f}%  "
                     f"PF {str(sr['pf']):>5}  DD Rs{sr['max_dd']:>13,.0f}  Sh {sr['sharpe']:>5}"
                     if sr else "no trades")
                  + f"  | skips {skips}")

        json.dump(summary, open(OUT_DIR / "mz_summary.json", "w"), indent=2, default=str)
        if rows:
            comp = pd.DataFrame(rows).sort_values("R_net", ascending=False)
            print("\n=== COMPARISON (sorted by REALISTIC net) ===")
            print(comp.to_string(index=False))
            comp.to_csv(OUT_DIR / "mz_comparison.csv", index=False)
        print(f"\nSaved -> {OUT_DIR}/  (mz_summary.json, mz_comparison.csv, mz_trades_*.csv, mz_equity_*.html)")
    finally:
        loader.close()


if __name__ == "__main__":
    main()
