#!/usr/bin/env python
"""
Convert the ultimate-on-dhanloader output (ultimate_dhanloader_results/*.json)
into the CSV/JSON the investor PDF generator expects, applying the standard
cost model so the report is net of estimated charges.

Run:  uv run python backtesting/survivor/ultimate_to_report.py
"""
import json
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
SRC = HERE / "ultimate_dhanloader_results"

# cost model (matches survivor_backtest.py)
BROKERAGE = 20.0
STT_SELL = 0.001
EXCH_TXN = 0.0003503
SEBI = 0.000001
STAMP = 0.00003
GST = 0.18


def leg_charges(entry_value, exit_value):
    brokerage = 2 * BROKERAGE
    stt = STT_SELL * entry_value
    txn = EXCH_TXN * (entry_value + exit_value)
    sebi = SEBI * (entry_value + exit_value)
    stamp = STAMP * exit_value
    gst = GST * (brokerage + txn + sebi)
    return brokerage + stt + txn + sebi + stamp + gst


def main():
    td = json.load(open(SRC / "trades.json"))
    trades = td["trades"]
    ds = json.load(open(SRC / "daily_stats.json"))
    summ = td.get("summary", {})

    rows = []
    for t in trades:
        qty = t["qty"]
        entry_val = t["entry_price"] * qty
        exit_val = t["exit_price"] * qty
        ch = leg_charges(entry_val, exit_val)
        gross = t["realised_pnl"]
        rows.append({
            "side": t["option_type"], "strike": t["strike"], "qty": qty,
            "entry_ts": t["entry_time"], "entry_prem": round(t["entry_price"], 2),
            "exit_ts": t["exit_time"], "exit_prem": round(t["exit_price"], 2),
            "gross_pnl": round(gross, 2), "charges": round(ch, 2),
            "net_pnl": round(gross - ch, 2), "reason": t.get("exit_reason", ""),
            "margin": round(t["margin_used"], 2),
            "entry_spot": round(t.get("nifty_at_entry", 0), 2),
        })
    tr = pd.DataFrame(rows)
    # normalise to uniform datetimes (some end-of-backtest exits are date-only)
    tr["entry_ts"] = pd.to_datetime(tr["entry_ts"], errors="coerce")
    tr["exit_ts"] = pd.to_datetime(tr["exit_ts"], errors="coerce")
    tr.to_csv(HERE / "survivor_trades.csv", index=False)

    # equity = cumulative NET realised (by exit date) + current unrealised MTM.
    # This reconciles exactly to the trade-level net at the end, while keeping
    # the mark-to-market drawdown shape during the period.
    net_by_date = (tr.groupby(tr["exit_ts"].dt.normalize())["net_pnl"]
                     .sum().sort_index().cumsum())
    eq = []
    for d in ds:
        day = pd.to_datetime(d["date"]).normalize()
        ts = day + pd.Timedelta(hours=15, minutes=29)
        sofar = net_by_date[net_by_date.index <= day]
        cum_net = float(sofar.iloc[-1]) if len(sofar) else 0.0
        unreal = d.get("unrealised_pnl", 0) or 0
        eq.append((ts, cum_net + unreal, d.get("current_margin", 0)))
    # terminal point: reconcile to total net realised (captures end-of-backtest
    # settlements whose exit dates fall beyond the last daily snapshot)
    eq.append((tr["exit_ts"].max(), float(tr["net_pnl"].sum()), 0.0))
    pd.DataFrame(eq, columns=["ts", "equity", "margin"]).to_csv(
        HERE / "survivor_equity.csv", index=False)

    net = float(tr["net_pnl"].sum())
    wins = int((tr["net_pnl"] > 0).sum())
    out = {
        "scope": "ULTIMATE engine on dhanloader (date-aware lots)",
        "total_legs": len(tr),
        "win_rate_pct": round(100 * wins / len(tr), 2),
        "realised_net_pnl": round(net, 2),
        "total_credit_collected": summ.get("total_credit_collected"),
        "peak_margin_estimate": summ.get("peak_concurrent_margin"),
        "gross_pnl_before_costs": summ.get("total_realised_pnl"),
    }
    json.dump(out, open(HERE / "survivor_summary.json", "w"), indent=2)
    print(f"Converted {len(tr)} trades.")
    print(f"  gross P&L : Rs {summ.get('total_realised_pnl',0):,.0f}")
    print(f"  net P&L   : Rs {net:,.0f}  (after est. charges)")
    print(f"  peak margin: Rs {summ.get('peak_concurrent_margin',0):,.0f}")


if __name__ == "__main__":
    main()
