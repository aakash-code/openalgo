import json
import warnings
from pathlib import Path

import pandas as pd

warnings.filterwarnings(
    "ignore",
    message="The behavior of DataFrame concatenation with empty or all-NA entries is deprecated.",
    category=FutureWarning,
)


BASE_DIR = Path(__file__).resolve().parent
NAKED_DIR = BASE_DIR / "1_NAKED_ORIGINAL"
HEDGED_DIR = BASE_DIR / "2_HEDGED_PRO"
OUTPUT_FILE = BASE_DIR / "strategy_360_dashboard.html"


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


def load_strategy(strategy_name, folder: Path):
    payload = json.loads((folder / "results" / "trades.json").read_text())
    trades = pd.DataFrame(payload["trades"])

    if trades.empty:
        return {
            "strategy": strategy_name,
            "payload": payload,
            "trades": trades,
            "daily": pd.DataFrame(),
            "weekly": pd.DataFrame(),
            "monthly": pd.DataFrame(),
            "yearly": pd.DataFrame(),
            "expiry": pd.DataFrame(),
            "weekday": pd.DataFrame(),
            "exit_reason": pd.DataFrame(),
            "option_type": pd.DataFrame(),
            "entry_available": False,
        }

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
    if "expiry" not in trades:
        trades["expiry"] = None
    if "option_type" not in trades:
        trades["option_type"] = "NA"

    entry_available = trades["entry_dt"].notna().any()
    if entry_available:
        trades["holding_minutes"] = (
            trades["exit_dt"] - trades["entry_dt"]
        ).dt.total_seconds() / 60.0
        trades["holding_hours"] = trades["holding_minutes"] / 60.0
        trades["entry_hour"] = trades["entry_dt"].dt.strftime("%H:%M")
    else:
        trades["holding_minutes"] = None
        trades["holding_hours"] = None
        trades["entry_hour"] = "NA"

    trades["exit_date"] = trades["exit_dt"].dt.tz_localize(None).dt.normalize()
    trades["exit_week"] = trades["exit_dt"].dt.tz_localize(None).dt.to_period("W-MON").astype(str)
    trades["exit_month"] = trades["exit_dt"].dt.tz_localize(None).dt.to_period("M").astype(str)
    trades["exit_year"] = trades["exit_dt"].dt.tz_localize(None).dt.year.astype(str)
    trades["weekday"] = trades["exit_dt"].dt.day_name()
    trades["exit_hour"] = trades["exit_dt"].dt.strftime("%H:%M")
    trades["expiry"] = pd.to_datetime(trades["expiry"], errors="coerce")
    trades = trades.sort_values("exit_dt").reset_index(drop=True)
    trades["cum_pnl"] = trades["realised_pnl"].cumsum()
    trades["equity_peak"] = trades["cum_pnl"].cummax()
    trades["drawdown"] = trades["cum_pnl"] - trades["equity_peak"]

    daily = (
        trades.groupby("exit_date", as_index=False)
        .agg(
            pnl=("realised_pnl", "sum"),
            trades=("symbol", "count"),
            margin=("margin_used", "sum"),
            avg_pnl=("realised_pnl", "mean"),
            winners=("realised_pnl", lambda s: int((s > 0).sum())),
            losers=("realised_pnl", lambda s: int((s < 0).sum())),
        )
        .sort_values("exit_date")
    )
    daily["cum_pnl"] = daily["pnl"].cumsum()
    daily["equity_peak"] = daily["cum_pnl"].cummax()
    daily["drawdown"] = daily["cum_pnl"] - daily["equity_peak"]

    weekly = (
        trades.groupby("exit_week", as_index=False)
        .agg(
            pnl=("realised_pnl", "sum"),
            trades=("symbol", "count"),
            margin=("margin_used", "sum"),
            avg_pnl=("realised_pnl", "mean"),
        )
        .sort_values("exit_week")
    )
    monthly = (
        trades.groupby("exit_month", as_index=False)
        .agg(
            pnl=("realised_pnl", "sum"),
            trades=("symbol", "count"),
            margin=("margin_used", "sum"),
            avg_pnl=("realised_pnl", "mean"),
        )
        .sort_values("exit_month")
    )
    yearly = (
        trades.groupby("exit_year", as_index=False)
        .agg(
            pnl=("realised_pnl", "sum"),
            trades=("symbol", "count"),
            margin=("margin_used", "sum"),
            avg_pnl=("realised_pnl", "mean"),
        )
        .sort_values("exit_year")
    )
    expiry = (
        trades.dropna(subset=["expiry"])
        .groupby("expiry", as_index=False)
        .agg(
            pnl=("realised_pnl", "sum"),
            trades=("symbol", "count"),
            margin=("margin_used", "sum"),
            avg_pnl=("realised_pnl", "mean"),
        )
        .sort_values("expiry")
    )
    weekday = (
        trades.groupby("weekday", as_index=False)
        .agg(
            pnl=("realised_pnl", "sum"),
            trades=("symbol", "count"),
            avg_pnl=("realised_pnl", "mean"),
        )
    )
    weekday["weekday"] = pd.Categorical(
        weekday["weekday"],
        categories=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
        ordered=True,
    )
    weekday = weekday.sort_values("weekday")
    exit_reason = (
        trades.groupby("exit_reason", as_index=False)
        .agg(pnl=("realised_pnl", "sum"), trades=("symbol", "count"))
        .sort_values("trades", ascending=False)
    )
    option_type = (
        trades.groupby("option_type", as_index=False)
        .agg(pnl=("realised_pnl", "sum"), trades=("symbol", "count"))
        .sort_values("option_type")
    )

    return {
        "strategy": strategy_name,
        "payload": payload,
        "trades": trades,
        "daily": daily,
        "weekly": weekly,
        "monthly": monthly,
        "yearly": yearly,
        "expiry": expiry,
        "weekday": weekday,
        "exit_reason": exit_reason,
        "option_type": option_type,
        "entry_available": entry_available,
    }


def strategy_kpis(bundle):
    trades = bundle["trades"]
    daily = bundle["daily"]
    pnl = float(trades["realised_pnl"].sum())
    total = int(len(trades))
    wins = trades[trades["realised_pnl"] > 0]
    losses = trades[trades["realised_pnl"] < 0]
    margin_peak = float(daily["margin"].max()) if not daily.empty else float(trades["margin_used"].max())
    max_dd = float(daily["drawdown"].min()) if not daily.empty else float(trades["drawdown"].min())
    best_day = float(daily["pnl"].max()) if not daily.empty else 0.0
    worst_day = float(daily["pnl"].min()) if not daily.empty else 0.0
    best_week = float(bundle["weekly"]["pnl"].max()) if not bundle["weekly"].empty else 0.0
    worst_week = float(bundle["weekly"]["pnl"].min()) if not bundle["weekly"].empty else 0.0
    best_month = float(bundle["monthly"]["pnl"].max()) if not bundle["monthly"].empty else 0.0
    worst_month = float(bundle["monthly"]["pnl"].min()) if not bundle["monthly"].empty else 0.0
    avg_hold = float(trades["holding_hours"].mean()) if bundle["entry_available"] else None
    profit_factor = (
        float(wins["realised_pnl"].sum()) / abs(float(losses["realised_pnl"].sum()))
        if not losses.empty and float(losses["realised_pnl"].sum()) != 0
        else None
    )
    return {
        "Strategy": bundle["strategy"],
        "Trades": total,
        "Net P&L": pnl,
        "Win Rate": (len(wins) / total * 100) if total else 0.0,
        "Avg Trade": (pnl / total) if total else 0.0,
        "Avg Winner": float(wins["realised_pnl"].mean()) if not wins.empty else 0.0,
        "Avg Loser": float(losses["realised_pnl"].mean()) if not losses.empty else 0.0,
        "Profit Factor": profit_factor,
        "Peak Margin Day": margin_peak,
        "Max Drawdown": max_dd,
        "Best Day": best_day,
        "Worst Day": worst_day,
        "Best Week": best_week,
        "Worst Week": worst_week,
        "Best Month": best_month,
        "Worst Month": worst_month,
        "Avg Holding Hours": avg_hold,
    }


def make_table(df, money_cols=None, pct_cols=None, round_cols=None, date_cols=None):
    money_cols = money_cols or []
    pct_cols = pct_cols or []
    round_cols = round_cols or []
    date_cols = date_cols or []
    frame = df.copy()
    for col in money_cols:
        if col in frame:
            frame[col] = frame[col].map(format_inr)
    for col in pct_cols:
        if col in frame:
            frame[col] = frame[col].map(format_pct)
    for col in round_cols:
        if col in frame:
            frame[col] = frame[col].map(lambda v: f"{v:.2f}" if pd.notna(v) else "NA")
    for col in date_cols:
        if col in frame:
            frame[col] = pd.to_datetime(frame[col], errors="coerce").dt.strftime("%Y-%m-%d")
    frame = frame.fillna("NA")
    return frame.to_html(index=False, classes="data-table", border=0, justify="left", table_id=None)


def build_trade_journal(trades):
    cols = [
        "strategy",
        "symbol",
        "option_type",
        "qty",
        "entry_dt",
        "exit_dt",
        "entry_price",
        "exit_price",
        "realised_pnl",
        "margin_used",
        "nifty_at_entry",
        "exit_reason",
        "holding_hours",
    ]
    available = [col for col in cols if col in trades.columns]
    journal = trades[available].copy()
    if "entry_dt" in journal:
        journal["entry_dt"] = pd.to_datetime(journal["entry_dt"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M")
    if "exit_dt" in journal:
        journal["exit_dt"] = pd.to_datetime(journal["exit_dt"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M")
    for col in ["entry_price", "exit_price", "realised_pnl", "margin_used", "nifty_at_entry"]:
        if col in journal:
            journal[col] = journal[col].map(format_inr if col in ["realised_pnl", "margin_used"] else lambda v: f"{v:.2f}" if pd.notna(v) else "NA")
    if "holding_hours" in journal:
        journal["holding_hours"] = journal["holding_hours"].map(lambda v: f"{v:.2f}" if pd.notna(v) else "NA")
    journal = journal.rename(
        columns={
            "strategy": "Strategy",
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
        }
    )
    return journal


def build_figures(naked, hedged, combined):
    kpi_rows = pd.DataFrame([strategy_kpis(naked), strategy_kpis(hedged)])

    figures = {
        "compare_pnl": {
            "data": [
                {
                    "type": "bar",
                    "x": kpi_rows["Strategy"].tolist(),
                    "y": lakh_list(kpi_rows["Net P&L"]),
                    "marker": {"color": ["#b45309", "#0f766e"]},
                    "hovertemplate": "%{x}<br>Net P&L: Rs %{y:.2f}L<extra></extra>",
                }
            ],
            "layout": base_layout("Net P&L Comparison", 350, "Rs Lakhs"),
        },
        "compare_drawdown": {
            "data": [
                {
                    "type": "bar",
                    "x": kpi_rows["Strategy"].tolist(),
                    "y": lakh_list(kpi_rows["Max Drawdown"]),
                    "marker": {"color": ["#f59e0b", "#dc2626"]},
                    "hovertemplate": "%{x}<br>Max Drawdown: Rs %{y:.2f}L<extra></extra>",
                }
            ],
            "layout": base_layout("Max Drawdown Comparison", 350, "Rs Lakhs"),
        },
        "equity_curves": {
            "data": [
                {
                    "type": "scatter",
                    "mode": "lines",
                    "name": "Naked",
                    "x": ts_list(naked["daily"]["exit_date"]),
                    "y": lakh_list(naked["daily"]["cum_pnl"]),
                    "line": {"color": "#b45309", "width": 2.2},
                    "hovertemplate": "%{x}<br>Naked: Rs %{y:.2f}L<extra></extra>",
                },
                {
                    "type": "scatter",
                    "mode": "lines",
                    "name": "Hedged",
                    "x": ts_list(hedged["daily"]["exit_date"]),
                    "y": lakh_list(hedged["daily"]["cum_pnl"]),
                    "line": {"color": "#0f766e", "width": 2.2},
                    "hovertemplate": "%{x}<br>Hedged: Rs %{y:.2f}L<extra></extra>",
                },
            ],
            "layout": base_layout("Daily Equity Curves", 430, "Rs Lakhs"),
        },
        "daily_pnl": {
            "data": [
                {
                    "type": "bar",
                    "name": "Naked",
                    "x": ts_list(naked["daily"]["exit_date"]),
                    "y": lakh_list(naked["daily"]["pnl"]),
                    "marker": {"color": "#f59e0b"},
                },
                {
                    "type": "bar",
                    "name": "Hedged",
                    "x": ts_list(hedged["daily"]["exit_date"]),
                    "y": lakh_list(hedged["daily"]["pnl"]),
                    "marker": {"color": "#14b8a6"},
                },
            ],
            "layout": {**base_layout("Day By Day P&L", 430, "Rs Lakhs"), "barmode": "group"},
        },
        "weekly_pnl": {
            "data": [
                {
                    "type": "scatter",
                    "mode": "lines+markers",
                    "name": "Naked",
                    "x": naked["weekly"]["exit_week"].tolist(),
                    "y": lakh_list(naked["weekly"]["pnl"]),
                    "line": {"color": "#b45309", "width": 2},
                },
                {
                    "type": "scatter",
                    "mode": "lines+markers",
                    "name": "Hedged",
                    "x": hedged["weekly"]["exit_week"].tolist(),
                    "y": lakh_list(hedged["weekly"]["pnl"]),
                    "line": {"color": "#0f766e", "width": 2},
                },
            ],
            "layout": base_layout("Week By Week P&L", 400, "Rs Lakhs"),
        },
        "monthly_pnl": {
            "data": [
                {
                    "type": "bar",
                    "name": "Naked",
                    "x": naked["monthly"]["exit_month"].tolist(),
                    "y": lakh_list(naked["monthly"]["pnl"]),
                    "marker": {"color": "#f59e0b"},
                },
                {
                    "type": "bar",
                    "name": "Hedged",
                    "x": hedged["monthly"]["exit_month"].tolist(),
                    "y": lakh_list(hedged["monthly"]["pnl"]),
                    "marker": {"color": "#14b8a6"},
                },
            ],
            "layout": {**base_layout("Month By Month P&L", 430, "Rs Lakhs"), "barmode": "group"},
        },
        "yearly_pnl": {
            "data": [
                {
                    "type": "bar",
                    "name": "Naked",
                    "x": naked["yearly"]["exit_year"].tolist(),
                    "y": lakh_list(naked["yearly"]["pnl"]),
                    "marker": {"color": "#b45309"},
                },
                {
                    "type": "bar",
                    "name": "Hedged",
                    "x": hedged["yearly"]["exit_year"].tolist(),
                    "y": lakh_list(hedged["yearly"]["pnl"]),
                    "marker": {"color": "#0f766e"},
                },
            ],
            "layout": {**base_layout("Year By Year P&L", 360, "Rs Lakhs"), "barmode": "group"},
        },
        "weekday_compare": {
            "data": [
                {
                    "type": "bar",
                    "name": "Naked",
                    "x": naked["weekday"]["weekday"].astype(str).tolist(),
                    "y": lakh_list(naked["weekday"]["pnl"]),
                    "marker": {"color": "#f59e0b"},
                },
                {
                    "type": "bar",
                    "name": "Hedged",
                    "x": hedged["weekday"]["weekday"].astype(str).tolist(),
                    "y": lakh_list(hedged["weekday"]["pnl"]),
                    "marker": {"color": "#14b8a6"},
                },
            ],
            "layout": {**base_layout("Weekday Performance", 360, "Rs Lakhs"), "barmode": "group"},
        },
        "exit_reason_compare": {
            "data": [
                {
                    "type": "pie",
                    "labels": combined["exit_reason"]["exit_reason"].tolist(),
                    "values": combined["exit_reason"]["trades"].tolist(),
                    "hole": 0.58,
                    "textinfo": "label+percent",
                    "marker": {"colors": ["#0f766e", "#f59e0b", "#dc2626", "#1d4ed8", "#7c3aed", "#14b8a6"]},
                }
            ],
            "layout": {**base_layout("Combined Exit Reason Mix", 390, None), "showlegend": False, "hovermode": "closest"},
        },
        "option_mix": {
            "data": [
                {
                    "type": "bar",
                    "name": "Naked",
                    "x": naked["option_type"]["option_type"].tolist(),
                    "y": lakh_list(naked["option_type"]["pnl"]),
                    "marker": {"color": "#b45309"},
                },
                {
                    "type": "bar",
                    "name": "Hedged",
                    "x": hedged["option_type"]["option_type"].tolist(),
                    "y": lakh_list(hedged["option_type"]["pnl"]),
                    "marker": {"color": "#0f766e"},
                },
            ],
            "layout": {**base_layout("CE vs PE Contribution", 360, "Rs Lakhs"), "barmode": "group"},
        },
        "holding_compare": {
            "data": [
                {
                    "type": "histogram",
                    "name": "Hedged Holding Hrs",
                    "x": hedged["trades"]["holding_hours"].dropna().round(2).tolist(),
                    "marker": {"color": "#0f766e"},
                    "opacity": 0.85,
                }
            ],
            "layout": {
                **base_layout("Holding Time Distribution", 360, "Trades"),
                "xaxis": {"title": "Holding Hours", "gridcolor": "rgba(148,163,184,0.12)"},
            },
        },
    }
    return figures, kpi_rows


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
    }
    if y_title:
        layout["yaxis"] = {"title": y_title, "gridcolor": "rgba(148,163,184,0.18)"}
    else:
        layout["yaxis"] = {"gridcolor": "rgba(148,163,184,0.18)"}
    return layout


def build_best_worst_table(bundle):
    daily = bundle["daily"]
    weekly = bundle["weekly"]
    monthly = bundle["monthly"]
    yearly = bundle["yearly"]
    weekday = bundle["weekday"]
    best_weekday = weekday.sort_values("pnl", ascending=False).head(1)
    worst_weekday = weekday.sort_values("pnl", ascending=True).head(1)
    rows = [
        {"Metric": "Best Day", "Value": daily.sort_values("pnl", ascending=False).head(1)["exit_date"].dt.strftime("%Y-%m-%d").iloc[0], "P&L": daily["pnl"].max()},
        {"Metric": "Worst Day", "Value": daily.sort_values("pnl", ascending=True).head(1)["exit_date"].dt.strftime("%Y-%m-%d").iloc[0], "P&L": daily["pnl"].min()},
        {"Metric": "Best Week", "Value": weekly.sort_values("pnl", ascending=False).head(1)["exit_week"].iloc[0], "P&L": weekly["pnl"].max()},
        {"Metric": "Worst Week", "Value": weekly.sort_values("pnl", ascending=True).head(1)["exit_week"].iloc[0], "P&L": weekly["pnl"].min()},
        {"Metric": "Best Month", "Value": monthly.sort_values("pnl", ascending=False).head(1)["exit_month"].iloc[0], "P&L": monthly["pnl"].max()},
        {"Metric": "Worst Month", "Value": monthly.sort_values("pnl", ascending=True).head(1)["exit_month"].iloc[0], "P&L": monthly["pnl"].min()},
        {"Metric": "Best Year", "Value": yearly.sort_values("pnl", ascending=False).head(1)["exit_year"].iloc[0], "P&L": yearly["pnl"].max()},
        {"Metric": "Worst Year", "Value": yearly.sort_values("pnl", ascending=True).head(1)["exit_year"].iloc[0], "P&L": yearly["pnl"].min()},
        {"Metric": "Best Weekday", "Value": best_weekday["weekday"].astype(str).iloc[0], "P&L": best_weekday["pnl"].iloc[0]},
        {"Metric": "Worst Weekday", "Value": worst_weekday["weekday"].astype(str).iloc[0], "P&L": worst_weekday["pnl"].iloc[0]},
    ]
    return pd.DataFrame(rows)


def build_dashboard():
    naked = load_strategy("Naked Original", NAKED_DIR)
    hedged = load_strategy("Hedged Pro", HEDGED_DIR)
    all_cols = sorted(set(naked["trades"].columns).union(set(hedged["trades"].columns)))
    combined_trades = pd.concat(
        [naked["trades"].reindex(columns=all_cols), hedged["trades"].reindex(columns=all_cols)],
        ignore_index=True,
        sort=False,
    )
    combined = {
        "trades": combined_trades,
        "exit_reason": (
            combined_trades.groupby("exit_reason", as_index=False)
            .agg(trades=("symbol", "count"), pnl=("realised_pnl", "sum"))
            .sort_values("trades", ascending=False)
        ),
    }
    figures, kpi_rows = build_figures(naked, hedged, combined)

    kpi_display = kpi_rows.copy()
    for col in ["Net P&L", "Avg Trade", "Avg Winner", "Avg Loser", "Peak Margin Day", "Max Drawdown", "Best Day", "Worst Day", "Best Week", "Worst Week", "Best Month", "Worst Month"]:
        kpi_display[col] = kpi_display[col].map(format_inr)
    kpi_display["Win Rate"] = kpi_display["Win Rate"].map(format_pct)
    kpi_display["Profit Factor"] = kpi_display["Profit Factor"].map(lambda v: f"{v:.2f}" if pd.notna(v) else "NA")
    kpi_display["Avg Holding Hours"] = kpi_display["Avg Holding Hours"].map(lambda v: f"{v:.2f}" if pd.notna(v) else "NA")

    naked_best_worst = build_best_worst_table(naked)
    hedged_best_worst = build_best_worst_table(hedged)
    for frame in [naked_best_worst, hedged_best_worst]:
        frame["P&L"] = frame["P&L"].map(format_inr)

    naked_trade_journal = build_trade_journal(naked["trades"])
    hedged_trade_journal = build_trade_journal(hedged["trades"])
    combined_trade_journal = build_trade_journal(combined_trades)

    tables = {
        "kpis": kpi_display.to_html(index=False, classes="data-table", border=0, justify="left"),
        "naked_best_worst": naked_best_worst.to_html(index=False, classes="data-table", border=0, justify="left"),
        "hedged_best_worst": hedged_best_worst.to_html(index=False, classes="data-table", border=0, justify="left"),
        "naked_daily": make_table(naked["daily"].rename(columns={"exit_date": "Date", "pnl": "P&L", "trades": "Trades", "margin": "Margin", "avg_pnl": "Avg P&L", "winners": "Winners", "losers": "Losers", "cum_pnl": "Cumulative", "drawdown": "Drawdown"}), money_cols=["P&L", "Margin", "Avg P&L", "Cumulative", "Drawdown"], date_cols=["Date"]),
        "hedged_daily": make_table(hedged["daily"].rename(columns={"exit_date": "Date", "pnl": "P&L", "trades": "Trades", "margin": "Margin", "avg_pnl": "Avg P&L", "winners": "Winners", "losers": "Losers", "cum_pnl": "Cumulative", "drawdown": "Drawdown"}), money_cols=["P&L", "Margin", "Avg P&L", "Cumulative", "Drawdown"], date_cols=["Date"]),
        "naked_weekly": make_table(naked["weekly"].rename(columns={"exit_week": "Week", "pnl": "P&L", "trades": "Trades", "margin": "Margin", "avg_pnl": "Avg P&L"}), money_cols=["P&L", "Margin", "Avg P&L"]),
        "hedged_weekly": make_table(hedged["weekly"].rename(columns={"exit_week": "Week", "pnl": "P&L", "trades": "Trades", "margin": "Margin", "avg_pnl": "Avg P&L"}), money_cols=["P&L", "Margin", "Avg P&L"]),
        "naked_monthly": make_table(naked["monthly"].rename(columns={"exit_month": "Month", "pnl": "P&L", "trades": "Trades", "margin": "Margin", "avg_pnl": "Avg P&L"}), money_cols=["P&L", "Margin", "Avg P&L"]),
        "hedged_monthly": make_table(hedged["monthly"].rename(columns={"exit_month": "Month", "pnl": "P&L", "trades": "Trades", "margin": "Margin", "avg_pnl": "Avg P&L"}), money_cols=["P&L", "Margin", "Avg P&L"]),
        "naked_yearly": make_table(naked["yearly"].rename(columns={"exit_year": "Year", "pnl": "P&L", "trades": "Trades", "margin": "Margin", "avg_pnl": "Avg P&L"}), money_cols=["P&L", "Margin", "Avg P&L"]),
        "hedged_yearly": make_table(hedged["yearly"].rename(columns={"exit_year": "Year", "pnl": "P&L", "trades": "Trades", "margin": "Margin", "avg_pnl": "Avg P&L"}), money_cols=["P&L", "Margin", "Avg P&L"]),
        "naked_expiry": make_table(naked["expiry"].rename(columns={"expiry": "Expiry", "pnl": "P&L", "trades": "Trades", "margin": "Margin", "avg_pnl": "Avg P&L"}), money_cols=["P&L", "Margin", "Avg P&L"], date_cols=["Expiry"]),
        "hedged_expiry": make_table(hedged["expiry"].rename(columns={"expiry": "Expiry", "pnl": "P&L", "trades": "Trades", "margin": "Margin", "avg_pnl": "Avg P&L"}), money_cols=["P&L", "Margin", "Avg P&L"], date_cols=["Expiry"]),
        "combined_trades": combined_trade_journal.to_html(index=False, classes="data-table searchable", border=0, justify="left", table_id="combined-trades-table"),
        "naked_trades": naked_trade_journal.to_html(index=False, classes="data-table searchable", border=0, justify="left", table_id="naked-trades-table"),
        "hedged_trades": hedged_trade_journal.to_html(index=False, classes="data-table searchable", border=0, justify="left", table_id="hedged-trades-table"),
    }

    figure_targets = [
        ("compare_pnl", ""),
        ("compare_drawdown", ""),
        ("equity_curves", "wide"),
        ("daily_pnl", "wide"),
        ("weekly_pnl", "wide"),
        ("monthly_pnl", "wide"),
        ("yearly_pnl", ""),
        ("weekday_compare", ""),
        ("exit_reason_compare", ""),
        ("option_mix", ""),
        ("holding_compare", "wide"),
    ]
    chart_html = "".join(
        f'<div class="panel {size}"><div id="{chart_id}" class="chart"></div></div>'
        for chart_id, size in figure_targets
    )

    limitations = [
        "Both strategy folders now include the latest exported trade, daily and expiry data through the current backtest window ending on April 30, 2026.",
        "Candle drilldown for NIFTY and option legs still needs DuckDB access. The local Python runtime currently does not have the duckdb package installed, so this report stops at strategy analytics and trade journals.",
    ]
    limitations_html = "".join(f"<li>{item}</li>" for item in limitations)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Strategy 360 Dashboard</title>
  <script src="https://cdn.plot.ly/plotly-3.1.0.min.js"></script>
  <style>
    :root {{
      --panel: rgba(255,255,255,0.95);
      --ink: #0f172a;
      --muted: #64748b;
      --line: rgba(148,163,184,0.22);
      --shadow: 0 20px 50px rgba(15,23,42,0.08);
      --radius: 24px;
      --accent1: #0f766e;
      --accent2: #b45309;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Arial, sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(20,184,166,0.12), transparent 24%),
        radial-gradient(circle at top right, rgba(245,158,11,0.12), transparent 20%),
        linear-gradient(180deg, #f8fafc 0%, #eef2ff 100%);
    }}
    .shell {{
      width: min(1520px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 24px 0 42px;
    }}
    .hero, .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
    }}
    .hero {{
      padding: 28px;
      margin-bottom: 18px;
      background:
        linear-gradient(135deg, rgba(15,118,110,0.96), rgba(180,83,9,0.92));
      color: white;
    }}
    .eyebrow {{
      font-size: 12px;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      opacity: 0.74;
      margin-bottom: 10px;
    }}
    h1 {{
      margin: 0 0 10px;
      font-size: clamp(30px, 4vw, 50px);
      line-height: 0.96;
    }}
    .hero p {{
      max-width: 960px;
      line-height: 1.6;
      margin: 0;
      color: rgba(255,255,255,0.88);
    }}
    .note-panel {{
      margin-top: 16px;
      padding: 16px 18px;
      border-radius: 18px;
      background: rgba(255,255,255,0.12);
    }}
    .note-panel ul {{ margin: 8px 0 0 18px; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 18px;
    }}
    .panel {{
      padding: 12px;
      overflow: hidden;
    }}
    .panel.wide {{
      grid-column: 1 / -1;
    }}
    .chart {{
      width: 100%;
      min-height: 340px;
    }}
    .panel-title {{
      padding: 8px 10px 2px;
      font-size: 14px;
      font-weight: 700;
    }}
    .table-wrap {{
      overflow: auto;
      max-height: 640px;
      padding: 8px 10px 12px;
    }}
    table.data-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
      background: white;
    }}
    table.data-table th,
    table.data-table td {{
      text-align: left;
      padding: 9px 8px;
      border-bottom: 1px solid rgba(148,163,184,0.16);
      white-space: nowrap;
    }}
    table.data-table th {{
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      position: sticky;
      top: 0;
      background: white;
      z-index: 2;
    }}
    .two-col, .three-col {{
      display: grid;
      gap: 18px;
      margin-top: 18px;
    }}
    .two-col {{ grid-template-columns: 1fr 1fr; }}
    .three-col {{ grid-template-columns: repeat(3, 1fr); }}
    .section-title {{
      margin: 20px 0 10px;
      font-size: 18px;
      font-weight: 700;
    }}
    .search {{
      width: 100%;
      padding: 12px 14px;
      border: 1px solid var(--line);
      border-radius: 14px;
      font-size: 14px;
      margin: 4px 0 10px;
    }}
    @media (max-width: 980px) {{
      .grid, .two-col, .three-col {{ grid-template-columns: 1fr; }}
      .panel.wide {{ grid-column: auto; }}
      .shell {{ width: min(100vw - 18px, 100%); padding-top: 10px; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="eyebrow">Survivor Pro Package</div>
      <h1>Strategy 360 Dashboard</h1>
      <p>
        Side-by-side analytics for <strong>1_NAKED_ORIGINAL</strong> and <strong>2_HEDGED_PRO</strong>.
        This report compares trade quality, day/week/month/year behavior, drawdowns, exit patterns, expiry performance and the full trade journal.
      </p>
      <div class="note-panel">
        <strong>Current data limitations</strong>
        <ul>{limitations_html}</ul>
      </div>
    </section>

    <div class="section-title">Strategy KPI Comparison</div>
    <div class="panel">
      <div class="table-wrap">{tables["kpis"]}</div>
    </div>

    <div class="section-title">Comparison Charts</div>
    <section class="grid">
      {chart_html}
    </section>

    <div class="section-title">Best And Worst Zones</div>
    <section class="two-col">
      <div class="panel">
        <div class="panel-title">Naked Original</div>
        <div class="table-wrap">{tables["naked_best_worst"]}</div>
      </div>
      <div class="panel">
        <div class="panel-title">Hedged Pro</div>
        <div class="table-wrap">{tables["hedged_best_worst"]}</div>
      </div>
    </section>

    <div class="section-title">Period Tables</div>
    <section class="three-col">
      <div class="panel">
        <div class="panel-title">Naked Daily</div>
        <div class="table-wrap">{tables["naked_daily"]}</div>
      </div>
      <div class="panel">
        <div class="panel-title">Naked Weekly</div>
        <div class="table-wrap">{tables["naked_weekly"]}</div>
      </div>
      <div class="panel">
        <div class="panel-title">Naked Monthly</div>
        <div class="table-wrap">{tables["naked_monthly"]}</div>
      </div>
      <div class="panel">
        <div class="panel-title">Naked Yearly</div>
        <div class="table-wrap">{tables["naked_yearly"]}</div>
      </div>
      <div class="panel">
        <div class="panel-title">Naked Expiry</div>
        <div class="table-wrap">{tables["naked_expiry"]}</div>
      </div>
      <div class="panel">
        <div class="panel-title">Hedged Daily</div>
        <div class="table-wrap">{tables["hedged_daily"]}</div>
      </div>
      <div class="panel">
        <div class="panel-title">Hedged Weekly</div>
        <div class="table-wrap">{tables["hedged_weekly"]}</div>
      </div>
      <div class="panel">
        <div class="panel-title">Hedged Monthly</div>
        <div class="table-wrap">{tables["hedged_monthly"]}</div>
      </div>
      <div class="panel">
        <div class="panel-title">Hedged Yearly</div>
        <div class="table-wrap">{tables["hedged_yearly"]}</div>
      </div>
      <div class="panel">
        <div class="panel-title">Hedged Expiry</div>
        <div class="table-wrap">{tables["hedged_expiry"]}</div>
      </div>
    </section>

    <div class="section-title">Trade Journals</div>
    <section class="panel">
      <div class="panel-title">Combined All Trades</div>
      <input id="combined-search" class="search" placeholder="Filter combined trades by strategy, symbol, date, reason, price or pnl" />
      <div class="table-wrap">{tables["combined_trades"]}</div>
    </section>

    <section class="two-col">
      <div class="panel">
        <div class="panel-title">Naked Original Trade Journal</div>
        <input id="naked-search" class="search" placeholder="Filter naked trades" />
        <div class="table-wrap">{tables["naked_trades"]}</div>
      </div>
      <div class="panel">
        <div class="panel-title">Hedged Pro Trade Journal</div>
        <input id="hedged-search" class="search" placeholder="Filter hedged trades" />
        <div class="table-wrap">{tables["hedged_trades"]}</div>
      </div>
    </section>
  </div>

  <script>
    const figures = {json.dumps(figures)};
    const config = {{
      responsive: true,
      displaylogo: false,
      modeBarButtonsToRemove: ["select2d", "lasso2d"]
    }};
    Object.entries(figures).forEach(([id, figure]) => {{
      Plotly.newPlot(id, figure.data, figure.layout, config);
    }});

    function wireSearch(inputId, tableId) {{
      const input = document.getElementById(inputId);
      const table = document.getElementById(tableId);
      if (!input || !table) return;
      input.addEventListener('input', () => {{
        const term = input.value.toLowerCase();
        Array.from(table.tBodies[0].rows).forEach((row) => {{
          const text = row.innerText.toLowerCase();
          row.style.display = text.includes(term) ? '' : 'none';
        }});
      }});
    }}
    wireSearch('combined-search', 'combined-trades-table');
    wireSearch('naked-search', 'naked-trades-table');
    wireSearch('hedged-search', 'hedged-trades-table');
  </script>
</body>
</html>
"""
    OUTPUT_FILE.write_text(html)
    return OUTPUT_FILE


if __name__ == "__main__":
    out = build_dashboard()
    print(f"Dashboard created: {out}")
