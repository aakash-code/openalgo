#!/usr/bin/env python
"""
Survivor Strategy — Institutional Tear Sheet
============================================

Reads the engine outputs (survivor_trades.csv, survivor_equity.csv) and produces
a 360-degree performance + RISK report suitable for due diligence.

Deliberately includes the unflattering parts: this is a SHORT-PREMIUM strategy
(negatively skewed, short gamma/vega), so the report foregrounds tail risk,
capital-sizing reality, return distribution skew/kurtosis, drawdown profile, and
the backtest's fidelity limitations — the questions an HNI's advisor will ask.

Run:  uv run python backtesting/survivor/survivor_analytics.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

OUT_DIR = Path(__file__).resolve().parent
TRADING_DAYS = 252
RISK_FREE = 0.065          # ~6.5% Indian risk-free (T-bill), for Sharpe
MARGIN_BUFFER = 1.25       # capital an HNI must fund = peak margin x buffer


def _fmt(x):
    return f"{x:,.0f}"


def load():
    eq = pd.read_csv(OUT_DIR / "survivor_equity.csv", parse_dates=["ts"])
    eq = eq.sort_values("ts").reset_index(drop=True)
    eq["date"] = eq["ts"].dt.normalize()
    eq["daily_pnl"] = eq["equity"].diff().fillna(eq["equity"].iloc[0])
    tr = pd.read_csv(OUT_DIR / "survivor_trades.csv",
                     parse_dates=["entry_ts", "exit_ts"])
    return eq, tr


def section(title):
    print("\n" + "=" * 74)
    print(f"  {title}")
    print("=" * 74)


def main():
    eq, tr = load()
    pnl = eq["daily_pnl"]
    n_days = len(eq)
    years = max((eq["ts"].iloc[-1] - eq["ts"].iloc[0]).days / 365.25, 1e-9)

    total_pnl = eq["equity"].iloc[-1]
    annual_pnl = total_pnl / years

    # ---- capital sizing (the honesty anchor) ----
    peak_margin = eq["margin"].max()
    avg_margin = eq.loc[eq["margin"] > 0, "margin"].mean()
    funded_capital = peak_margin * MARGIN_BUFFER
    ret_on_funded = annual_pnl / funded_capital
    ret_on_peak = annual_pnl / peak_margin
    ret_on_avg = annual_pnl / avg_margin

    # ---- risk-adjusted (capital-independent ratios from daily PnL) ----
    daily_mean, daily_std = pnl.mean(), pnl.std(ddof=1)
    downside = pnl[pnl < 0]
    downside_std = downside.std(ddof=1)
    sharpe = (daily_mean / daily_std) * np.sqrt(TRADING_DAYS) if daily_std else 0
    sortino = (daily_mean / downside_std) * np.sqrt(TRADING_DAYS) if downside_std else 0

    # ---- drawdown (mark-to-market, what the HNI sees on statement) ----
    curve = eq["equity"]
    runmax = curve.cummax()
    dd = curve - runmax
    max_dd = dd.min()
    max_dd_pct_funded = max_dd / funded_capital
    # underwater duration
    underwater = dd < 0
    longest = cur = 0
    for u in underwater:
        cur = cur + 1 if u else 0
        longest = max(longest, cur)

    # ---- distribution / tail ----
    skew = pnl.skew()
    kurt = pnl.kurt()
    pct_pos = (pnl > 0).mean() * 100
    worst = eq.nsmallest(8, "daily_pnl")[["date", "daily_pnl"]]
    best = eq.nlargest(5, "daily_pnl")[["date", "daily_pnl"]]

    # ---- trade stats ----
    wins = tr[tr.net_pnl > 0]
    losses = tr[tr.net_pnl <= 0]
    profit_factor = wins.net_pnl.sum() / abs(losses.net_pnl.sum()) if len(losses) else np.inf
    expectancy = tr.net_pnl.mean()
    worst_leg = tr.net_pnl.min()
    best_leg = tr.net_pnl.max()

    # ---- monthly & yearly PnL ----
    m = eq.set_index("ts")["daily_pnl"].resample("ME").sum()
    monthly = m.to_frame("pnl")
    monthly["year"] = monthly.index.year
    monthly["month"] = monthly.index.strftime("%b")
    piv = monthly.pivot_table(index="year", columns="month", values="pnl", aggfunc="sum")
    piv = piv.reindex(columns=["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])
    yearly = eq.set_index("ts")["daily_pnl"].resample("YE").sum()

    # =====================================================================
    section("SURVIVOR STRATEGY — TEAR SHEET (NIFTY weekly, net of est. costs)")
    print(f"  Period            : {eq['ts'].iloc[0].date()} -> {eq['ts'].iloc[-1].date()}"
          f"  ({years:.2f} yrs, {n_days} trading days)")
    print(f"  Total net P&L     : Rs {_fmt(total_pnl)}")
    print(f"  Annualised P&L    : Rs {_fmt(annual_pnl)}")
    print(f"  Total legs        : {len(tr):,}   (win rate {len(wins)/len(tr)*100:.1f}%)")

    section("CAPITAL REQUIRED  (the number that sets the real return)")
    print(f"  Peak margin (estimate)      : Rs {_fmt(peak_margin)}")
    print(f"  Avg  margin (estimate)      : Rs {_fmt(avg_margin)}")
    print(f"  Funded capital (peak x{MARGIN_BUFFER}) : Rs {_fmt(funded_capital)}")
    print( "  --- return on capital ---")
    print(f"  on FUNDED capital  (honest) : {ret_on_funded*100:6.1f}% / yr")
    print(f"  on peak margin              : {ret_on_peak*100:6.1f}% / yr")
    print(f"  on avg  margin  (flattering): {ret_on_avg*100:6.1f}% / yr")

    section("RISK-ADJUSTED PERFORMANCE")
    print(f"  Sharpe ratio                : {sharpe:5.2f}")
    print(f"  Sortino ratio               : {sortino:5.2f}")
    print(f"  Calmar (annPnL / |maxDD|)   : {annual_pnl/abs(max_dd):5.2f}")
    print(f"  Max drawdown (MTM)          : Rs {_fmt(max_dd)}  "
          f"({max_dd_pct_funded*100:.1f}% of funded capital)")
    print(f"  Longest underwater          : {longest} trading days")
    print(f"  Profit factor               : {profit_factor:5.2f}")

    section("RETURN DISTRIBUTION  (short premium => watch the skew)")
    print(f"  Positive days               : {pct_pos:.1f}%")
    print(f"  Daily P&L skew              : {skew:+.2f}   "
          f"({'NEGATIVE — fat left tail' if skew < 0 else 'positive'})")
    print(f"  Daily P&L excess kurtosis   : {kurt:+.2f}   "
          f"({'fat tails' if kurt > 0 else 'thin tails'})")
    print(f"  Best leg / Worst leg        : Rs {_fmt(best_leg)} / Rs {_fmt(worst_leg)}")
    print(f"  Per-leg expectancy          : Rs {_fmt(expectancy)}")
    print("\n  Worst 8 days (the steamroller days):")
    for _, r in worst.iterrows():
        print(f"    {r['date'].date()}   Rs {_fmt(r['daily_pnl'])}")
    print("  Best 5 days:")
    for _, r in best.iterrows():
        print(f"    {r['date'].date()}   Rs {_fmt(r['daily_pnl'])}")

    section("MONTHLY NET P&L  (Rs)")
    with pd.option_context("display.float_format", lambda v: f"{v:,.0f}",
                           "display.width", 200):
        print(piv.fillna(0).to_string())
    print("\n  Yearly:")
    for ts, v in yearly.items():
        print(f"    {ts.year}: Rs {_fmt(v)}")
    neg_months = int((m < 0).sum())
    print(f"\n  Losing months: {neg_months} / {len(m)}   "
          f"worst month: Rs {_fmt(m.min())}")

    section("CSV/HTML ARTIFACTS")
    piv.to_csv(OUT_DIR / "survivor_monthly_pnl.csv")
    print(f"  monthly P&L  -> {OUT_DIR / 'survivor_monthly_pnl.csv'}")
    print(f"  equity curve -> {OUT_DIR / 'survivor_equity.html'}")
    print(f"  trade log    -> {OUT_DIR / 'survivor_trades.csv'}")
    print("=" * 74)


if __name__ == "__main__":
    main()
