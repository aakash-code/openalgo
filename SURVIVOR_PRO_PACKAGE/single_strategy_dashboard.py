import json
from pathlib import Path

import pandas as pd


def format_inr(value):
    if pd.isna(value):
        return "NA"
    sign = "-" if value < 0 else ""
    rounded = int(round(abs(float(value))))
    digits = str(rounded)
    if len(digits) <= 3:
        grouped = digits
    else:
        grouped = digits[-3:]
        digits = digits[:-3]
        parts = []
        while digits:
            parts.append(digits[-2:])
            digits = digits[:-2]
        grouped = ",".join(reversed(parts)) + "," + grouped
    return f"{sign}Rs {grouped}"


def format_pct(value):
    if pd.isna(value):
        return "NA"
    return f"{float(value):.2f}%"


def lakh_list(series, digits=2):
    return (pd.Series(series).fillna(0) / 100000.0).round(digits).tolist()


def ts_list(series):
    return [value.isoformat() if pd.notna(value) else None for value in series]


def base_layout(title, height, y_title):
    layout = {
        "title": {"text": title, "x": 0.02, "xanchor": "left"},
        "height": height,
        "paper_bgcolor": "white",
        "plot_bgcolor": "white",
        "margin": {"l": 42, "r": 24, "t": 60, "b": 42},
        "font": {"family": "Arial, sans-serif", "color": "#0f172a"},
        "hovermode": "x unified",
        "legend": {"orientation": "h", "y": 1.12, "x": 0},
        "xaxis": {"gridcolor": "rgba(148,163,184,0.12)"},
        "yaxis": {"gridcolor": "rgba(148,163,184,0.18)"},
    }
    if y_title:
        layout["yaxis"]["title"] = y_title
    return layout


def load_strategy(folder: Path, strategy_name: str):
    trades_payload = json.loads((folder / "results" / "trades.json").read_text())
    daily_payload = json.loads((folder / "results" / "daily_stats.json").read_text())
    expiry_payload = json.loads((folder / "results" / "expiry_stats.json").read_text())

    trades = pd.DataFrame(trades_payload["trades"])
    daily = pd.DataFrame(daily_payload)
    expiry = pd.DataFrame(expiry_payload)

    trades["strategy"] = strategy_name
    trades["entry_dt"] = pd.to_datetime(trades.get("entry_time"), errors="coerce")
    trades["exit_dt"] = pd.to_datetime(trades["exit_time"], errors="coerce")
    trades = trades.dropna(subset=["exit_dt"]).copy()
    if "entry_price" not in trades:
        trades["entry_price"] = None
    if "exit_price" not in trades:
        trades["exit_price"] = None
    if "margin_used" not in trades:
        trades["margin_used"] = 0.0
    if "nifty_at_entry" not in trades:
        trades["nifty_at_entry"] = None
    if "qty" not in trades:
        trades["qty"] = None

    trades["holding_hours"] = (
        (trades["exit_dt"] - trades["entry_dt"]).dt.total_seconds() / 3600.0
    )
    trades["exit_date"] = trades["exit_dt"].dt.tz_localize(None).dt.normalize()
    trades["exit_week"] = trades["exit_dt"].dt.tz_localize(None).dt.to_period("W-MON").astype(str)
    trades["exit_month"] = trades["exit_dt"].dt.tz_localize(None).dt.to_period("M").astype(str)
    trades["exit_year"] = trades["exit_dt"].dt.tz_localize(None).dt.year.astype(str)
    trades["weekday"] = trades["exit_dt"].dt.day_name()
    trades = trades.sort_values("exit_dt").reset_index(drop=True)
    trades["cum_pnl"] = trades["realised_pnl"].cumsum()
    trades["equity_peak"] = trades["cum_pnl"].cummax()
    trades["drawdown"] = trades["cum_pnl"] - trades["equity_peak"]

    daily["date"] = pd.to_datetime(daily["date"], errors="coerce")
    daily = daily.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    daily["daily_realised_pnl"] = daily["cumulative_rpnl"].diff().fillna(daily["cumulative_rpnl"])
    daily["exit_week"] = daily["date"].dt.to_period("W-MON").astype(str)
    daily["exit_month"] = daily["date"].dt.to_period("M").astype(str)
    daily["exit_year"] = daily["date"].dt.year.astype(str)
    daily["weekday"] = daily["date"].dt.day_name()
    daily["equity_peak"] = daily["cumulative_rpnl"].cummax()
    daily["drawdown"] = daily["cumulative_rpnl"] - daily["equity_peak"]

    weekly = (
        daily.groupby("exit_week", as_index=False)
        .agg(
            pnl=("daily_realised_pnl", "sum"),
            margin=("current_margin", "max"),
            open_positions=("open_positions", "max"),
        )
        .sort_values("exit_week")
    )
    monthly = (
        daily.groupby("exit_month", as_index=False)
        .agg(
            pnl=("daily_realised_pnl", "sum"),
            margin=("current_margin", "max"),
            open_positions=("open_positions", "max"),
        )
        .sort_values("exit_month")
    )
    yearly = (
        daily.groupby("exit_year", as_index=False)
        .agg(
            pnl=("daily_realised_pnl", "sum"),
            margin=("current_margin", "max"),
            open_positions=("open_positions", "max"),
        )
        .sort_values("exit_year")
    )
    weekday = (
        trades.groupby("weekday", as_index=False)
        .agg(pnl=("realised_pnl", "sum"), trades=("symbol", "count"))
    )
    weekday["weekday"] = pd.Categorical(
        weekday["weekday"],
        categories=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
        ordered=True,
    )
    weekday = weekday.sort_values("weekday")
    exit_reason = (
        trades.groupby("exit_reason", as_index=False)
        .agg(trades=("symbol", "count"), pnl=("realised_pnl", "sum"))
        .sort_values("trades", ascending=False)
    )
    option_type = (
        trades.groupby("option_type", as_index=False)
        .agg(trades=("symbol", "count"), pnl=("realised_pnl", "sum"))
        .sort_values("option_type")
    )
    expiry["expiry"] = pd.to_datetime(expiry["expiry"], errors="coerce")
    expiry = expiry.sort_values("expiry").reset_index(drop=True)

    return {
        "strategy": strategy_name,
        "folder": folder,
        "payload": trades_payload,
        "trades": trades,
        "daily": daily,
        "weekly": weekly,
        "monthly": monthly,
        "yearly": yearly,
        "expiry": expiry,
        "weekday": weekday,
        "exit_reason": exit_reason,
        "option_type": option_type,
    }


def build_kpis(bundle):
    trades = bundle["trades"]
    daily = bundle["daily"]
    wins = trades[trades["realised_pnl"] > 0]
    losses = trades[trades["realised_pnl"] < 0]
    profit_factor = (
        float(wins["realised_pnl"].sum()) / abs(float(losses["realised_pnl"].sum()))
        if not losses.empty and float(losses["realised_pnl"].sum()) != 0
        else None
    )
    peak_margin = (
        float(bundle["payload"].get("summary", {}).get("peak_concurrent_margin", daily["current_margin"].max()))
        if not daily.empty
        else 0.0
    )
    return {
        "Backtest Period": bundle["payload"].get("backtest_period", "NA"),
        "Net Realised P&L": format_inr(float(trades["realised_pnl"].sum())),
        "Total Trades": f"{len(trades):,}",
        "Win Rate": format_pct((len(wins) / len(trades) * 100) if len(trades) else 0.0),
        "Average Trade": format_inr(float(trades["realised_pnl"].mean())),
        "Average Winner": format_inr(float(wins["realised_pnl"].mean()) if not wins.empty else 0.0),
        "Average Loser": format_inr(float(losses["realised_pnl"].mean()) if not losses.empty else 0.0),
        "Profit Factor": f"{profit_factor:.2f}" if profit_factor is not None else "NA",
        "Peak Concurrent Margin": format_inr(peak_margin),
        "Max Drawdown": format_inr(float(daily["drawdown"].min()) if not daily.empty else 0.0),
        "Best Day": format_inr(float(daily["daily_realised_pnl"].max()) if not daily.empty else 0.0),
        "Worst Day": format_inr(float(daily["daily_realised_pnl"].min()) if not daily.empty else 0.0),
        "Best Week": format_inr(float(bundle["weekly"]["pnl"].max()) if not bundle["weekly"].empty else 0.0),
        "Worst Week": format_inr(float(bundle["weekly"]["pnl"].min()) if not bundle["weekly"].empty else 0.0),
        "Best Month": format_inr(float(bundle["monthly"]["pnl"].max()) if not bundle["monthly"].empty else 0.0),
        "Worst Month": format_inr(float(bundle["monthly"]["pnl"].min()) if not bundle["monthly"].empty else 0.0),
        "Average Holding Hours": f"{float(trades['holding_hours'].mean()):.2f}" if trades["holding_hours"].notna().any() else "NA",
    }


def build_best_worst_table(bundle):
    daily = bundle["daily"]
    weekly = bundle["weekly"]
    monthly = bundle["monthly"]
    yearly = bundle["yearly"]
    weekday = bundle["weekday"]
    rows = [
        {"Metric": "Best Day", "Bucket": daily.sort_values("daily_realised_pnl", ascending=False).head(1)["date"].dt.strftime("%Y-%m-%d").iloc[0], "P&L": daily["daily_realised_pnl"].max()},
        {"Metric": "Worst Day", "Bucket": daily.sort_values("daily_realised_pnl", ascending=True).head(1)["date"].dt.strftime("%Y-%m-%d").iloc[0], "P&L": daily["daily_realised_pnl"].min()},
        {"Metric": "Best Week", "Bucket": weekly.sort_values("pnl", ascending=False).head(1)["exit_week"].iloc[0], "P&L": weekly["pnl"].max()},
        {"Metric": "Worst Week", "Bucket": weekly.sort_values("pnl", ascending=True).head(1)["exit_week"].iloc[0], "P&L": weekly["pnl"].min()},
        {"Metric": "Best Month", "Bucket": monthly.sort_values("pnl", ascending=False).head(1)["exit_month"].iloc[0], "P&L": monthly["pnl"].max()},
        {"Metric": "Worst Month", "Bucket": monthly.sort_values("pnl", ascending=True).head(1)["exit_month"].iloc[0], "P&L": monthly["pnl"].min()},
        {"Metric": "Best Year", "Bucket": yearly.sort_values("pnl", ascending=False).head(1)["exit_year"].iloc[0], "P&L": yearly["pnl"].max()},
        {"Metric": "Worst Year", "Bucket": yearly.sort_values("pnl", ascending=True).head(1)["exit_year"].iloc[0], "P&L": yearly["pnl"].min()},
        {"Metric": "Best Weekday", "Bucket": weekday.sort_values("pnl", ascending=False).head(1)["weekday"].astype(str).iloc[0], "P&L": weekday["pnl"].max()},
        {"Metric": "Worst Weekday", "Bucket": weekday.sort_values("pnl", ascending=True).head(1)["weekday"].astype(str).iloc[0], "P&L": weekday["pnl"].min()},
    ]
    table = pd.DataFrame(rows)
    table["P&L"] = table["P&L"].map(format_inr)
    return table


def make_table(df, money_cols=None, date_cols=None, round_cols=None):
    frame = df.copy()
    for col in money_cols or []:
        if col in frame:
            frame[col] = frame[col].map(format_inr)
    for col in date_cols or []:
        if col in frame:
            frame[col] = pd.to_datetime(frame[col], errors="coerce").dt.strftime("%Y-%m-%d")
    for col in round_cols or []:
        if col in frame:
            frame[col] = frame[col].map(lambda v: f"{v:.2f}" if pd.notna(v) else "NA")
    return frame.fillna("NA").to_html(index=False, classes="data-table", border=0, justify="left")


def build_trade_journal(bundle):
    trades = bundle["trades"].copy()
    cols = [
        "symbol", "option_type", "qty", "entry_dt", "exit_dt", "entry_price", "exit_price",
        "realised_pnl", "margin_used", "nifty_at_entry", "exit_reason", "holding_hours"
    ]
    trades = trades[[col for col in cols if col in trades.columns]]
    trades["entry_dt"] = pd.to_datetime(trades["entry_dt"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M")
    trades["exit_dt"] = pd.to_datetime(trades["exit_dt"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M")
    for col in ["entry_price", "exit_price", "nifty_at_entry"]:
        if col in trades:
            trades[col] = trades[col].map(lambda v: f"{v:.2f}" if pd.notna(v) else "NA")
    for col in ["realised_pnl", "margin_used"]:
        if col in trades:
            trades[col] = trades[col].map(format_inr)
    if "holding_hours" in trades:
        trades["holding_hours"] = trades["holding_hours"].map(lambda v: f"{v:.2f}" if pd.notna(v) else "NA")
    return trades.rename(columns={
        "symbol": "Symbol",
        "option_type": "Type",
        "qty": "Qty",
        "entry_dt": "Entry Time",
        "exit_dt": "Exit Time",
        "entry_price": "Entry Px",
        "exit_price": "Exit Px",
        "realised_pnl": "P&L",
        "margin_used": "Margin",
        "nifty_at_entry": "NIFTY At Entry",
        "exit_reason": "Exit Reason",
        "holding_hours": "Holding Hrs",
    })


def build_figures(bundle):
    trades = bundle["trades"]
    daily = bundle["daily"]
    weekly = bundle["weekly"]
    monthly = bundle["monthly"]
    yearly = bundle["yearly"]
    expiry = bundle["expiry"]
    weekday = bundle["weekday"]
    exit_reason = bundle["exit_reason"]
    option_type = bundle["option_type"]

    return {
        "equity_curve": {
            "data": [
                {"type": "scatter", "mode": "lines", "name": "Equity", "x": ts_list(trades["exit_dt"]), "y": lakh_list(trades["cum_pnl"]), "line": {"color": "#0f766e", "width": 2.5}, "fill": "tozeroy"},
                {"type": "scatter", "mode": "lines", "name": "Peak", "x": ts_list(trades["exit_dt"]), "y": lakh_list(trades["equity_peak"]), "line": {"color": "#94a3b8", "width": 1.5, "dash": "dash"}},
            ],
            "layout": base_layout("Equity Curve", 420, "Rs Lakhs"),
        },
        "drawdown": {
            "data": [
                {"type": "scatter", "mode": "lines", "name": "Drawdown", "x": ts_list(trades["exit_dt"]), "y": lakh_list(trades["drawdown"]), "line": {"color": "#dc2626", "width": 2}, "fill": "tozeroy"},
            ],
            "layout": base_layout("Drawdown", 320, "Rs Lakhs"),
        },
        "daily_pnl": {
            "data": [
                {"type": "bar", "x": ts_list(daily["date"]), "y": lakh_list(daily["daily_realised_pnl"]), "name": "Daily P&L", "marker": {"color": ["#0f766e" if v >= 0 else "#dc2626" for v in daily["daily_realised_pnl"]]}},
                {"type": "scatter", "mode": "lines", "x": ts_list(daily["date"]), "y": lakh_list(daily["current_margin"]), "name": "Current Margin", "line": {"color": "#1d4ed8", "width": 2}, "yaxis": "y2"},
            ],
            "layout": {
                **base_layout("Day By Day P&L And Margin", 420, "Rs Lakhs"),
                "yaxis2": {"title": "Margin (Rs Lakhs)", "overlaying": "y", "side": "right", "showgrid": False},
            },
        },
        "weekly_pnl": {
            "data": [{"type": "scatter", "mode": "lines+markers", "x": weekly["exit_week"].tolist(), "y": lakh_list(weekly["pnl"]), "name": "Weekly P&L", "line": {"color": "#b45309", "width": 2}}],
            "layout": base_layout("Week By Week P&L", 380, "Rs Lakhs"),
        },
        "monthly_pnl": {
            "data": [{"type": "bar", "x": monthly["exit_month"].tolist(), "y": lakh_list(monthly["pnl"]), "name": "Monthly P&L", "marker": {"color": ["#0f766e" if v >= 0 else "#dc2626" for v in monthly["pnl"]]}}],
            "layout": base_layout("Month By Month P&L", 380, "Rs Lakhs"),
        },
        "yearly_pnl": {
            "data": [{"type": "bar", "x": yearly["exit_year"].tolist(), "y": lakh_list(yearly["pnl"]), "name": "Yearly P&L", "marker": {"color": "#0f766e"}}],
            "layout": base_layout("Year By Year P&L", 340, "Rs Lakhs"),
        },
        "weekday": {
            "data": [{"type": "bar", "x": weekday["weekday"].astype(str).tolist(), "y": lakh_list(weekday["pnl"]), "name": "Weekday P&L", "marker": {"color": ["#0f766e" if v >= 0 else "#dc2626" for v in weekday["pnl"]]}}],
            "layout": base_layout("Weekday Performance", 340, "Rs Lakhs"),
        },
        "expiry": {
            "data": [
                {"type": "bar", "x": ts_list(expiry["expiry"]), "y": lakh_list(expiry["realised_pnl"]), "name": "Expiry P&L", "marker": {"color": ["#0f766e" if v >= 0 else "#dc2626" for v in expiry["realised_pnl"]]}},
                {"type": "scatter", "mode": "lines+markers", "x": ts_list(expiry["expiry"]), "y": expiry["roi_on_peak_margin"].round(2).tolist(), "name": "ROI %", "line": {"color": "#7c3aed", "width": 2}, "yaxis": "y2"},
            ],
            "layout": {
                **base_layout("Expiry Performance", 390, "Rs Lakhs"),
                "yaxis2": {"title": "ROI %", "overlaying": "y", "side": "right", "showgrid": False},
            },
        },
        "exit_reason": {
            "data": [{"type": "pie", "labels": exit_reason["exit_reason"].tolist(), "values": exit_reason["trades"].tolist(), "hole": 0.58, "textinfo": "label+percent", "marker": {"colors": ["#0f766e", "#f59e0b", "#dc2626", "#1d4ed8", "#7c3aed", "#14b8a6"]}}],
            "layout": {**base_layout("Exit Reason Mix", 380, None), "showlegend": False, "hovermode": "closest"},
        },
        "option_mix": {
            "data": [{"type": "bar", "x": option_type["option_type"].tolist(), "y": lakh_list(option_type["pnl"]), "name": "Option Type P&L", "marker": {"color": "#1d4ed8"}}],
            "layout": base_layout("CE vs PE Contribution", 340, "Rs Lakhs"),
        },
        "holding": {
            "data": [{"type": "histogram", "x": trades["holding_hours"].dropna().round(2).tolist(), "name": "Holding Hrs", "marker": {"color": "#0f766e"}}],
            "layout": {**base_layout("Holding Time Distribution", 340, "Trades"), "xaxis": {"title": "Holding Hours", "gridcolor": "rgba(148,163,184,0.12)"}},
        },
    }


def build_single_dashboard(folder: Path, strategy_name: str, output_file: Path):
    bundle = load_strategy(folder, strategy_name)
    kpis = build_kpis(bundle)
    best_worst = build_best_worst_table(bundle)
    journal = build_trade_journal(bundle)
    figures = build_figures(bundle)

    cards_html = "".join(
        f'<div class="kpi-card"><div class="kpi-label">{k}</div><div class="kpi-value">{v}</div></div>'
        for k, v in kpis.items()
    )
    chart_ids = [
        ("equity_curve", "wide"), ("drawdown", "wide"), ("daily_pnl", "wide"),
        ("weekly_pnl", ""), ("monthly_pnl", ""), ("yearly_pnl", ""),
        ("weekday", ""), ("expiry", "wide"), ("exit_reason", ""), ("option_mix", ""), ("holding", "")
    ]
    chart_html = "".join(f'<div class="panel {size}"><div id="{cid}" class="chart"></div></div>' for cid, size in chart_ids)

    daily_table = make_table(
        bundle["daily"][["date", "daily_realised_pnl", "current_margin", "open_positions", "unrealised_pnl", "cumulative_rpnl", "drawdown"]].rename(
            columns={"date": "Date", "daily_realised_pnl": "Daily P&L", "current_margin": "Current Margin", "open_positions": "Open Positions", "unrealised_pnl": "Unrealised", "cumulative_rpnl": "Cumulative", "drawdown": "Drawdown"}
        ),
        money_cols=["Daily P&L", "Current Margin", "Unrealised", "Cumulative", "Drawdown"],
        date_cols=["Date"],
    )
    weekly_table = make_table(bundle["weekly"].rename(columns={"exit_week": "Week", "pnl": "P&L", "margin": "Peak Margin", "open_positions": "Open Positions"}), money_cols=["P&L", "Peak Margin"])
    monthly_table = make_table(bundle["monthly"].rename(columns={"exit_month": "Month", "pnl": "P&L", "margin": "Peak Margin", "open_positions": "Open Positions"}), money_cols=["P&L", "Peak Margin"])
    yearly_table = make_table(bundle["yearly"].rename(columns={"exit_year": "Year", "pnl": "P&L", "margin": "Peak Margin", "open_positions": "Open Positions"}), money_cols=["P&L", "Peak Margin"])
    expiry_table = make_table(bundle["expiry"].rename(columns={"expiry": "Expiry", "realised_pnl": "P&L", "peak_margin": "Peak Margin", "roi_on_peak_margin": "ROI %", "trades": "Trades"}), money_cols=["P&L", "Peak Margin"], date_cols=["Expiry"], round_cols=["ROI %"])
    journal_html = journal.to_html(index=False, classes="data-table searchable", border=0, justify="left", table_id="strategy-trades-table")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{strategy_name} Dashboard</title>
  <script src="https://cdn.plot.ly/plotly-3.1.0.min.js"></script>
  <style>
    :root {{ --panel: rgba(255,255,255,0.95); --ink: #0f172a; --muted: #64748b; --line: rgba(148,163,184,0.22); --shadow: 0 20px 50px rgba(15,23,42,0.08); --radius: 24px; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Arial, sans-serif; color: var(--ink); background: radial-gradient(circle at top left, rgba(20,184,166,0.12), transparent 24%), radial-gradient(circle at top right, rgba(29,78,216,0.10), transparent 20%), linear-gradient(180deg, #f8fafc 0%, #eef2ff 100%); }}
    .shell {{ width: min(1480px, calc(100vw - 32px)); margin: 0 auto; padding: 24px 0 40px; }}
    .hero, .panel, .kpi-card {{ background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); box-shadow: var(--shadow); }}
    .hero {{ padding: 28px; margin-bottom: 18px; background: linear-gradient(135deg, rgba(15,118,110,0.96), rgba(29,78,216,0.92)); color: white; }}
    .eyebrow {{ font-size: 12px; letter-spacing: 0.18em; text-transform: uppercase; opacity: 0.74; margin-bottom: 10px; }}
    h1 {{ margin: 0 0 10px; font-size: clamp(30px, 4vw, 48px); line-height: 0.98; }}
    .hero p {{ max-width: 920px; line-height: 1.6; margin: 0; color: rgba(255,255,255,0.88); }}
    .kpi-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; margin-bottom: 18px; }}
    .kpi-card {{ padding: 18px; }}
    .kpi-label {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); margin-bottom: 10px; }}
    .kpi-value {{ font-size: 23px; font-weight: 700; line-height: 1.2; }}
    .grid, .two-col, .three-col {{ display: grid; gap: 18px; }}
    .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .two-col {{ grid-template-columns: 1fr 1fr; }}
    .three-col {{ grid-template-columns: repeat(3, 1fr); }}
    .panel {{ padding: 12px; overflow: hidden; }}
    .panel.wide {{ grid-column: 1 / -1; }}
    .panel-title {{ padding: 8px 10px 2px; font-size: 14px; font-weight: 700; }}
    .chart {{ width: 100%; min-height: 340px; }}
    .section-title {{ margin: 20px 0 10px; font-size: 18px; font-weight: 700; }}
    .search {{ width: 100%; padding: 12px 14px; border: 1px solid var(--line); border-radius: 14px; font-size: 14px; margin: 4px 0 10px; }}
    .table-wrap {{ overflow: auto; max-height: 680px; padding: 8px 10px 12px; }}
    table.data-table {{ width: 100%; border-collapse: collapse; font-size: 13px; background: white; }}
    table.data-table th, table.data-table td {{ text-align: left; padding: 9px 8px; border-bottom: 1px solid rgba(148,163,184,0.16); white-space: nowrap; }}
    table.data-table th {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); position: sticky; top: 0; background: white; z-index: 2; }}
    @media (max-width: 980px) {{ .grid, .two-col, .three-col, .kpi-grid {{ grid-template-columns: 1fr; }} .panel.wide {{ grid-column: auto; }} .shell {{ width: min(100vw - 18px, 100%); padding-top: 10px; }} }}
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="eyebrow">Survivor Pro Package</div>
      <h1>{strategy_name} 360 Dashboard</h1>
      <p>
        Standalone analytics for {strategy_name}. This report shows trade quality, drawdown, day/week/month/year behavior,
        expiry performance, exit mix, option-type contribution and the full trade journal.
      </p>
    </section>

    <section class="kpi-grid">{cards_html}</section>

    <div class="section-title">Market Behavior And Performance</div>
    <section class="grid">{chart_html}</section>

    <div class="section-title">Best And Worst Zones</div>
    <section class="panel"><div class="table-wrap">{best_worst.to_html(index=False, classes="data-table", border=0, justify="left")}</div></section>

    <div class="section-title">Period Tables</div>
    <section class="three-col">
      <div class="panel"><div class="panel-title">Daily</div><div class="table-wrap">{daily_table}</div></div>
      <div class="panel"><div class="panel-title">Weekly</div><div class="table-wrap">{weekly_table}</div></div>
      <div class="panel"><div class="panel-title">Monthly</div><div class="table-wrap">{monthly_table}</div></div>
      <div class="panel"><div class="panel-title">Yearly</div><div class="table-wrap">{yearly_table}</div></div>
      <div class="panel wide"><div class="panel-title">Expiry</div><div class="table-wrap">{expiry_table}</div></div>
    </section>

    <div class="section-title">Trade Journal</div>
    <section class="panel">
      <input id="strategy-search" class="search" placeholder="Filter trades by symbol, date, reason, pnl, price or option type" />
      <div class="table-wrap">{journal_html}</div>
    </section>
  </div>
  <script>
    const figures = {json.dumps(figures)};
    const config = {{ responsive: true, displaylogo: false, modeBarButtonsToRemove: ["select2d", "lasso2d"] }};
    Object.entries(figures).forEach(([id, figure]) => Plotly.newPlot(id, figure.data, figure.layout, config));
    const input = document.getElementById('strategy-search');
    const table = document.getElementById('strategy-trades-table');
    input.addEventListener('input', () => {{
      const term = input.value.toLowerCase();
      Array.from(table.tBodies[0].rows).forEach((row) => {{
        row.style.display = row.innerText.toLowerCase().includes(term) ? '' : 'none';
      }});
    }});
  </script>
</body>
</html>
"""
    output_file.write_text(html)
    return output_file
