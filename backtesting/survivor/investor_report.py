#!/usr/bin/env python
"""
Investor-ready PDF performance report for the options income strategy.

IMPORTANT: This report intentionally exposes ZERO strategy logic — no parameters,
no entry/exit rules, no strike selection, no code. It presents performance,
calendar P&L (day / week-expiry / month / year), capital & margin, and risk only.

Reads:  survivor_trades.csv, survivor_equity.csv, survivor_summary.json
Writes: Investor_Performance_Report.pdf  (+ granular CSVs for due diligence)

Run:  uv run --with matplotlib python backtesting/survivor/investor_report.py
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

HERE = Path(__file__).resolve().parent
FIRM = "[ YOUR FIRM NAME ]"
PRODUCT = "NIFTY Weekly Options — Systematic Income Strategy"
NAVY = "#0f2741"
BLUE = "#1f6fb2"
GREEN = "#1a7f43"
RED = "#c0392b"
GREY = "#6b7785"
TRADING_DAYS = 252


# ---------- formatting helpers ----------
def inr(x):
    return f"Rs {x:,.0f}"


def lakh(x):
    return f"Rs {x/1e5:,.2f} L"


def crore(x):
    return f"Rs {x/1e7:,.2f} Cr"


def pct(x):
    return f"{x:+.1f}%"


# ---------- load + derive ----------
def load():
    tr = pd.read_csv(HERE / "survivor_trades.csv",
                     parse_dates=["entry_ts", "exit_ts"])
    eq = pd.read_csv(HERE / "survivor_equity.csv", parse_dates=["ts"]) \
        .sort_values("ts").reset_index(drop=True)
    eq["daily_pnl"] = eq["equity"].diff().fillna(eq["equity"].iloc[0])
    eq["date"] = eq["ts"].dt.normalize()
    summ = json.loads((HERE / "survivor_summary.json").read_text())
    return tr, eq, summ


def metrics(tr, eq, summ):
    pnl = eq["daily_pnl"]
    years = max((eq["ts"].iloc[-1] - eq["ts"].iloc[0]).days / 365.25, 1e-9)
    net = eq["equity"].iloc[-1]
    m = {}
    m["start"] = eq["ts"].iloc[0].date()
    m["end"] = eq["ts"].iloc[-1].date()
    m["years"] = years
    m["tdays"] = len(eq)
    # authoritative net = sum of realised trade P&L (robust to equity-curve
    # end-of-period MTM/settlement timing)
    m["net"] = float(tr["net_pnl"].sum())
    m["annual"] = m["net"] / years
    m["sharpe"] = pnl.mean() / pnl.std(ddof=1) * np.sqrt(TRADING_DAYS)
    dn = pnl[pnl < 0].std(ddof=1)
    m["sortino"] = pnl.mean() / dn * np.sqrt(TRADING_DAYS) if dn else 0
    m["maxdd"] = (eq["equity"] - eq["equity"].cummax()).min()
    m["worst_day"] = pnl.min()
    m["best_day"] = pnl.max()
    m["pos_days"] = (pnl > 0).mean() * 100
    m["legs"] = len(tr)
    m["wins"] = int((tr.net_pnl > 0).sum())
    m["win_rate"] = m["wins"] / len(tr) * 100
    m["avg_win"] = tr.loc[tr.net_pnl > 0, "net_pnl"].mean()
    m["avg_loss"] = tr.loc[tr.net_pnl <= 0, "net_pnl"].mean()
    # capital / margin
    m["peak_margin"] = summ.get("peak_margin_estimate", eq["margin"].max())
    m["capital_base"] = m["peak_margin"]
    m["max_leg_margin"] = tr["margin"].max()
    m["med_leg_margin"] = tr["margin"].median()
    m["roc_annual"] = m["annual"] / m["capital_base"] * 100
    # per-expiry (exit date == weekly expiry)
    tr["expiry"] = tr["exit_ts"].dt.normalize()
    pe = tr.groupby("expiry").agg(pnl=("net_pnl", "sum"),
                                  margin=("margin", "sum"),
                                  legs=("net_pnl", "count")).reset_index()
    pe["ret_pct"] = pe["pnl"] / pe["margin"] * 100
    m["per_expiry"] = pe
    m["n_expiries"] = len(pe)
    m["pos_expiries"] = (pe.pnl > 0).mean() * 100
    m["avg_expiry_pnl"] = pe.pnl.mean()
    m["best_expiry"] = pe.pnl.max()
    m["worst_expiry"] = pe.pnl.min()
    m["avg_expiry_ret"] = pe.ret_pct.mean()
    return m


# ---------- page builders ----------
def _page(fig_title=None):
    fig = plt.figure(figsize=(11.69, 8.27))      # A4 landscape
    fig.subplots_adjust(left=0.06, right=0.94, top=0.9, bottom=0.08)
    return fig


def _band(fig, title, sub=""):
    ax = fig.add_axes([0, 0.92, 1, 0.08]); ax.axis("off")
    ax.add_patch(plt.Rectangle((0, 0), 1, 1, color=NAVY))
    ax.text(0.06, 0.55, title, color="white", fontsize=16, fontweight="bold",
            va="center")
    if sub:
        ax.text(0.94, 0.5, sub, color="#9fc0dd", fontsize=10, ha="right", va="center")


def _footer(fig, page):
    ax = fig.add_axes([0, 0, 1, 0.05]); ax.axis("off")
    ax.text(0.06, 0.5, "SIMULATED / BACKTESTED RESULTS — HYPOTHETICAL. "
            "Not a live track record. Past performance is not indicative of future results.",
            color=GREY, fontsize=7, va="center")
    ax.text(0.94, 0.5, f"{FIRM}   |   Confidential   |   p.{page}",
            color=GREY, fontsize=7, ha="right", va="center")


def _table(ax, df, col_labels, colw=None, fs=8, head_color=NAVY):
    ax.axis("off")
    t = ax.table(cellText=df, colLabels=col_labels, loc="center",
                 cellLoc="center", colWidths=colw)
    t.auto_set_font_size(False); t.set_fontsize(fs); t.scale(1, 1.45)
    for (r, c), cell in t.get_celld().items():
        cell.set_edgecolor("#d8dee5")
        if r == 0:
            cell.set_facecolor(head_color); cell.set_text_props(color="white",
                                                                fontweight="bold")
        elif r % 2 == 0:
            cell.set_facecolor("#f4f7fa")
    return t


def cover(pdf, m):
    fig = _page()
    ax = fig.add_axes([0, 0, 1, 1]); ax.axis("off")
    ax.add_patch(plt.Rectangle((0, 0.78), 1, 0.22, color=NAVY))
    ax.text(0.5, 0.90, PRODUCT, color="white", fontsize=24, fontweight="bold",
            ha="center")
    _lbl = os.environ.get("REPORT_LABEL", "")
    ax.text(0.5, 0.845,
            "Simulated Performance Report" + (f"  —  {_lbl}" if _lbl else ""),
            color="#9fc0dd", fontsize=13, ha="center")
    ax.text(0.5, 0.72, f"Period:  {m['start']}  to  {m['end']}   "
            f"({m['years']:.2f} years)", fontsize=12, ha="center", color=NAVY)

    cards = [("Net P&L", crore(m["net"]), GREEN),
             ("Annualised P&L", lakh(m["annual"]), GREEN),
             ("Return on Capital", f"{m['roc_annual']:.1f}% / yr", BLUE),
             ("Win Rate", f"{m['win_rate']:.1f}%", BLUE),
             ("Max Drawdown", lakh(m["maxdd"]), RED),
             ("Capital Required", crore(m["capital_base"]), NAVY)]
    x0, y0, w, h = 0.08, 0.40, 0.265, 0.13
    for i, (lab, val, col) in enumerate(cards):
        cx = x0 + (i % 3) * (w + 0.035)
        cy = y0 - (i // 3) * (h + 0.04)
        ax.add_patch(plt.Rectangle((cx, cy), w, h, facecolor="#f4f7fa",
                                   edgecolor=col, linewidth=1.6))
        ax.text(cx + 0.02, cy + h - 0.035, lab, fontsize=10, color=GREY)
        ax.text(cx + 0.02, cy + 0.028, val, fontsize=16, color=col,
                fontweight="bold")

    ax.text(0.5, 0.14, "STRICTLY CONFIDENTIAL — FOR PROSPECTIVE INVESTOR REVIEW",
            fontsize=10, ha="center", color=RED, fontweight="bold")
    ax.text(0.5, 0.085, "Hypothetical / backtested performance — see disclosures "
            "on the final page.", fontsize=8.5, ha="center", color=GREY)
    ax.text(0.5, 0.04, f"Prepared by {FIRM}  ·  Generated "
            f"{datetime.now():%d %b %Y}", fontsize=8.5, ha="center", color=GREY)
    pdf.savefig(fig); plt.close(fig)


def summary_equity(pdf, m, eq):
    fig = _page(); _band(fig, "Executive Summary", "Strategy overview & equity curve")
    # left: metric table
    axl = fig.add_axes([0.06, 0.12, 0.36, 0.74])
    rows = [
        ["Net P&L", crore(m["net"])],
        ["Annualised P&L", lakh(m["annual"])],
        ["Return on capital (p.a.)", f"{m['roc_annual']:.1f}%"],
        ["Sharpe ratio", f"{m['sharpe']:.2f}"],
        ["Sortino ratio", f"{m['sortino']:.2f}"],
        ["Win rate (per trade)", f"{m['win_rate']:.1f}%"],
        ["Profitable expiries", f"{m['pos_expiries']:.0f}%"],
        ["Positive days", f"{m['pos_days']:.0f}%"],
        ["Max drawdown", lakh(m["maxdd"])],
        ["Worst single day", lakh(m["worst_day"])],
        ["Total trades (legs)", f"{m['legs']:,}"],
        ["Weekly expiries traded", f"{m['n_expiries']}"],
    ]
    _table(axl, rows, ["Metric", "Value"], colw=[0.62, 0.38], fs=9)
    # right: equity curve
    axr = fig.add_axes([0.48, 0.40, 0.46, 0.46])
    axr.plot(eq["ts"], eq["equity"], color=BLUE, lw=1.6)
    axr.fill_between(eq["ts"], eq["equity"], color=BLUE, alpha=0.08)
    axr.set_title("Cumulative Net P&L (Rs)", fontsize=10, color=NAVY)
    axr.grid(alpha=0.3); axr.tick_params(labelsize=8)
    axr.yaxis.set_major_formatter(lambda v, _: f"{v/1e5:.0f}L")
    # description block (generic, no secrets)
    axd = fig.add_axes([0.48, 0.12, 0.46, 0.22]); axd.axis("off")
    desc = ("A fully systematic, rules-based options income strategy on NIFTY "
            "weekly options. It harvests option premium through the expiry cycle "
            "with continuous, automated position management and disciplined exits "
            "at expiry. Execution is 100% rule-driven (no discretion). Position "
            "sizing reflects the prevailing NIFTY lot size over the period "
            "(25 to 75 to 65, per SEBI revisions). Results are net of estimated "
            "brokerage, statutory charges and slippage.")
    axd.text(0, 1, "About the strategy", fontsize=10, color=NAVY, fontweight="bold",
             va="top")
    axd.text(0, 0.8, desc, fontsize=8.6, color="#33414f", va="top", wrap=True)
    _footer(fig, 2); pdf.savefig(fig); plt.close(fig)


def calendar_returns(pdf, m, eq):
    fig = _page(); _band(fig, "Calendar Returns", "Year-wise & month-wise net P&L")
    dp = eq.set_index("ts")["daily_pnl"]
    yearly = dp.resample("YE").sum()
    monthly = dp.resample("ME").sum()
    mt = monthly.to_frame("pnl")
    mt["year"] = mt.index.year; mt["mon"] = mt.index.strftime("%b")
    piv = mt.pivot_table(index="year", columns="mon", values="pnl", aggfunc="sum")
    order = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
             "Oct", "Nov", "Dec"]
    piv = piv.reindex(columns=order)
    piv_l = (piv / 1e5)

    # yearly table
    axy = fig.add_axes([0.06, 0.66, 0.40, 0.20])
    yr_rows = [[str(ts.year), lakh(v), pct(v / m["capital_base"] * 100)]
               for ts, v in yearly.items()]
    _table(axy, yr_rows, ["Year", "Net P&L", "Return on capital"],
           colw=[0.3, 0.4, 0.4], fs=9)
    axy.set_title("Year-wise", fontsize=10, color=NAVY, loc="left", pad=2)

    # monthly heat table (Rs Lakh)
    axm = fig.add_axes([0.06, 0.12, 0.88, 0.44])
    cell = [[("" if pd.isna(piv_l.iloc[i, j]) else f"{piv_l.iloc[i, j]:,.1f}")
             for j in range(piv_l.shape[1])] for i in range(piv_l.shape[0])]
    t = axm.table(cellText=cell, colLabels=order,
                  rowLabels=[str(y) for y in piv_l.index],
                  loc="center", cellLoc="center")
    t.auto_set_font_size(False); t.set_fontsize(8.5); t.scale(1, 1.6)
    vmax = np.nanmax(np.abs(piv_l.values))
    for (r, c), cl in t.get_celld().items():
        cl.set_edgecolor("#d8dee5")
        if r == 0 or c == -1:
            cl.set_facecolor(NAVY); cl.set_text_props(color="white", fontweight="bold")
        else:
            v = piv_l.iloc[r - 1, c]
            if not pd.isna(v):
                a = min(abs(v) / vmax, 1) * 0.55
                cl.set_facecolor((0.10, 0.50, 0.26, a) if v >= 0 else (0.75, 0.22, 0.17, a))
    axm.axis("off")
    axm.set_title("Month-wise net P&L  (Rs Lakh)", fontsize=10, color=NAVY,
                  loc="left", pad=6)
    # monthly bar
    axb = fig.add_axes([0.06, 0.60, 0.88, 0.0])  # placeholder remove
    axb.remove()
    _footer(fig, 3); pdf.savefig(fig); plt.close(fig)
    return piv, yearly, monthly


def per_expiry_page(pdf, m):
    fig = _page(); _band(fig, "Per-Expiry Performance", "Week-by-week (weekly expiry cycle)")
    pe = m["per_expiry"]
    axc = fig.add_axes([0.06, 0.46, 0.88, 0.40])
    colors = [GREEN if v >= 0 else RED for v in pe["pnl"]]
    axc.bar(pe["expiry"], pe["pnl"] / 1e5, color=colors, width=4)
    axc.axhline(0, color=GREY, lw=0.8)
    axc.set_title("Net P&L per weekly expiry (Rs Lakh)", fontsize=10, color=NAVY)
    axc.grid(alpha=0.25, axis="y"); axc.tick_params(labelsize=8)
    # stats
    axs = fig.add_axes([0.06, 0.12, 0.40, 0.28])
    rows = [
        ["Weekly expiries traded", f"{m['n_expiries']}"],
        ["Profitable expiries", f"{m['pos_expiries']:.0f}%"],
        ["Average P&L / expiry", lakh(m["avg_expiry_pnl"])],
        ["Best expiry", lakh(m["best_expiry"])],
        ["Worst expiry", lakh(m["worst_expiry"])],
        ["Avg return / expiry (on margin)", f"{m['avg_expiry_ret']:.2f}%"],
    ]
    _table(axs, rows, ["Per-expiry metric", "Value"], colw=[0.66, 0.34], fs=9)
    # histogram
    axh = fig.add_axes([0.54, 0.12, 0.40, 0.28])
    axh.hist(pe["pnl"] / 1e5, bins=30, color=BLUE, alpha=0.8)
    axh.set_title("Distribution of per-expiry P&L (Rs Lakh)", fontsize=9, color=NAVY)
    axh.grid(alpha=0.25); axh.tick_params(labelsize=8)
    _footer(fig, 4); pdf.savefig(fig); plt.close(fig)


def daily_page(pdf, m, eq):
    fig = _page(); _band(fig, "Daily P&L", "Day-wise profit & loss profile")
    axc = fig.add_axes([0.06, 0.46, 0.88, 0.40])
    colors = [GREEN if v >= 0 else RED for v in eq["daily_pnl"]]
    axc.bar(eq["ts"], eq["daily_pnl"] / 1e5, color=colors, width=1.0)
    axc.axhline(0, color=GREY, lw=0.8)
    axc.set_title("Daily net P&L (Rs Lakh)", fontsize=10, color=NAVY)
    axc.grid(alpha=0.25, axis="y"); axc.tick_params(labelsize=8)
    # best/worst days
    worst = eq.nsmallest(8, "daily_pnl")[["date", "daily_pnl"]]
    best = eq.nlargest(8, "daily_pnl")[["date", "daily_pnl"]]
    axw = fig.add_axes([0.06, 0.12, 0.40, 0.28])
    rows = [[str(r.date.date()), lakh(r.daily_pnl)] for r in worst.itertuples()]
    _table(axw, rows, ["Worst 8 days", "P&L"], colw=[0.55, 0.45], fs=8.5,
           head_color=RED)
    axb = fig.add_axes([0.54, 0.12, 0.40, 0.28])
    rows = [[str(r.date.date()), lakh(r.daily_pnl)] for r in best.itertuples()]
    _table(axb, rows, ["Best 8 days", "P&L"], colw=[0.55, 0.45], fs=8.5,
           head_color=GREEN)
    fig.text(0.06, 0.40, f"Positive days: {m['pos_days']:.0f}%     "
             f"Best day: {lakh(m['best_day'])}     Worst day: {lakh(m['worst_day'])}",
             fontsize=9, color=NAVY)
    _footer(fig, 5); pdf.savefig(fig); plt.close(fig)


def capital_page(pdf, m, eq, tr):
    fig = _page(); _band(fig, "Capital & Margin", "Funding requirement & trade activity")
    # margin utilisation over time (end-of-day)
    axc = fig.add_axes([0.06, 0.46, 0.88, 0.40])
    axc.fill_between(eq["ts"], eq["margin"] / 1e7, color=BLUE, alpha=0.5)
    axc.axhline(m["peak_margin"] / 1e7, color=RED, lw=1, ls="--",
                label=f"Peak {crore(m['peak_margin'])}")
    axc.set_title("Margin deployed over time (Rs Crore)", fontsize=10, color=NAVY)
    axc.legend(fontsize=8); axc.grid(alpha=0.25); axc.tick_params(labelsize=8)
    # capital table
    axm = fig.add_axes([0.06, 0.12, 0.40, 0.28])
    rows = [
        ["Peak margin (capital required)", crore(m["peak_margin"])],
        ["Max margin — single trade", lakh(m["max_leg_margin"])],
        ["Typical margin — single trade", lakh(m["med_leg_margin"])],
        ["Avg margin / expiry", lakh(m["per_expiry"]["margin"].mean())],
        ["Return on capital (p.a.)", f"{m['roc_annual']:.1f}%"],
    ]
    _table(axm, rows, ["Capital / Margin", "Value"], colw=[0.66, 0.34], fs=9)
    # activity table
    axa = fig.add_axes([0.54, 0.12, 0.40, 0.28])
    rows = [
        ["Total trades (legs)", f"{m['legs']:,}"],
        ["Avg trades / expiry", f"{m['legs']/m['n_expiries']:.0f}"],
        ["Win rate", f"{m['win_rate']:.1f}%"],
        ["Average winning trade", inr(m["avg_win"])],
        ["Average losing trade", inr(m["avg_loss"])],
    ]
    _table(axa, rows, ["Trade activity", "Value"], colw=[0.62, 0.38], fs=9)
    _footer(fig, 6); pdf.savefig(fig); plt.close(fig)


def risk_page(pdf, m, eq):
    fig = _page(); _band(fig, "Risk & Disclosures", "Drawdown profile & important notices")
    axc = fig.add_axes([0.06, 0.52, 0.88, 0.34])
    dd = (eq["equity"] - eq["equity"].cummax()) / 1e5
    axc.fill_between(eq["ts"], dd, color=RED, alpha=0.5)
    axc.set_title("Drawdown (Rs Lakh, mark-to-market)", fontsize=10, color=NAVY)
    axc.grid(alpha=0.25); axc.tick_params(labelsize=8)
    axr = fig.add_axes([0.06, 0.30, 0.40, 0.18])
    rows = [["Sharpe ratio", f"{m['sharpe']:.2f}"],
            ["Sortino ratio", f"{m['sortino']:.2f}"],
            ["Max drawdown", lakh(m["maxdd"])],
            ["Worst single day", lakh(m["worst_day"])]]
    _table(axr, rows, ["Risk metric", "Value"], colw=[0.6, 0.4], fs=9)
    axd = fig.add_axes([0.06, 0.04, 0.88, 0.22]); axd.axis("off")
    disc = (
        "IMPORTANT DISCLOSURES.  The performance shown is HYPOTHETICAL and derived "
        "from a historical SIMULATION (backtest) on 1-minute market data; it is NOT "
        "an actual trading record and no live capital was deployed. Hypothetical "
        "results have inherent limitations — they are prepared with the benefit of "
        "hindsight and do not reflect real-order liquidity, fills, or margin "
        "behaviour during stressed markets. Option selling carries the risk of "
        "large, sudden losses that can exceed the premium received. Margin and "
        "charges are estimates. Returns are regime-dependent and the test window "
        "may not represent future market conditions. PAST PERFORMANCE IS NOT "
        "INDICATIVE OF FUTURE RESULTS. This document is confidential, for "
        "information only, and is NOT investment advice or an offer to buy or sell "
        "any security. Prospective subscribers must assess suitability independently."
    )
    axd.text(0, 1, disc, fontsize=8, color="#33414f", va="top", wrap=True)
    _footer(fig, 7); pdf.savefig(fig); plt.close(fig)


def export_csvs(eq, piv, yearly, monthly, m):
    eq[["date", "daily_pnl", "equity", "margin"]].to_csv(
        HERE / "report_daily_pnl.csv", index=False)
    m["per_expiry"].to_csv(HERE / "report_weekly_expiry_pnl.csv", index=False)
    monthly.to_frame("net_pnl").to_csv(HERE / "report_monthly_pnl.csv")
    yearly.to_frame("net_pnl").to_csv(HERE / "report_yearly_pnl.csv")


def main():
    tr, eq, summ = load()
    m = metrics(tr, eq, summ)
    out = HERE / "Investor_Performance_Report.pdf"
    with PdfPages(out) as pdf:
        cover(pdf, m)
        summary_equity(pdf, m, eq)
        piv, yearly, monthly = calendar_returns(pdf, m, eq)
        per_expiry_page(pdf, m)
        daily_page(pdf, m, eq)
        capital_page(pdf, m, eq, tr)
        risk_page(pdf, m, eq)
    export_csvs(eq, piv, yearly, monthly, m)
    print(f"PDF  -> {out}")
    print("CSVs -> report_daily_pnl.csv, report_weekly_expiry_pnl.csv, "
          "report_monthly_pnl.csv, report_yearly_pnl.csv")
    print(f"\nHeadline: net {crore(m['net'])} | annual {lakh(m['annual'])} | "
          f"RoC {m['roc_annual']:.1f}%/yr | win {m['win_rate']:.1f}% | "
          f"maxDD {lakh(m['maxdd'])} | peak margin {crore(m['peak_margin'])}")
    print(f"Per-trade margin: max {lakh(m['max_leg_margin'])}, "
          f"typical {lakh(m['med_leg_margin'])}")


if __name__ == "__main__":
    main()
