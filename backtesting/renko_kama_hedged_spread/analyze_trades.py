#!/usr/bin/env python3
"""
Deep-dive analysis of the winning Renko+KAMA+CHOP config's trades:
per-day P&L, drawdown with dates, gap-up/gap-down behavior, and what
characterized the best/worst days. Produces two CSVs:
  - trades_detail.csv : every trade, enriched with the day's gap info
  - daily_summary.csv : one row per trading day (P&L, gap, drawdown, etc.)

Run: uv run python backtesting/renko_kama_hedged_spread/analyze_trades.py
"""
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

_dir = Path(__file__).resolve().parent
DUCKDB_PATH = str(_dir / ".." / ".." / "db" / "historify.duckdb")
TRADES_CSV = _dir / "results" / "trades_A_renkoKAMA_5min_fixed15_chop38.2.csv"


def load_daily_spot():
    conn = duckdb.connect(DUCKDB_PATH, read_only=True)
    df = conn.execute(
        """
        SELECT timestamp, open, high, low, close FROM market_data
        WHERE symbol='NIFTY' AND exchange='NSE_INDEX' AND interval='1m'
        ORDER BY timestamp
        """
    ).df()
    conn.close()
    df["dt"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    df = df.set_index("dt").drop(columns=["timestamp"])
    daily = df.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    daily["prev_close"] = daily["close"].shift(1)
    daily["gap_pts"] = daily["open"] - daily["prev_close"]
    daily["gap_pct"] = 100 * daily["gap_pts"] / daily["prev_close"]
    daily["day_range_pts"] = daily["high"] - daily["low"]
    daily["day_return_pct"] = 100 * (daily["close"] - daily["open"]) / daily["open"]

    def classify(pct):
        if pd.isna(pct):
            return "n/a"
        if pct >= 0.3:
            return "gap_up"
        if pct <= -0.3:
            return "gap_down"
        return "flat"

    daily["gap_type"] = daily["gap_pct"].apply(classify)
    # Did the day's move continue in the gap's direction, or fade/reverse it?
    def behavior(row):
        if row["gap_type"] == "gap_up":
            return "gap_and_go (continued up)" if row["day_return_pct"] > 0 else "faded (reversed down)"
        if row["gap_type"] == "gap_down":
            return "gap_and_go (continued down)" if row["day_return_pct"] < 0 else "faded (reversed up)"
        return "n/a"

    daily["post_gap_behavior"] = daily.apply(behavior, axis=1)
    return daily


def main():
    trades = pd.read_csv(TRADES_CSV, parse_dates=["entry_ts", "exit_ts"])
    trades["date"] = trades["entry_ts"].dt.date

    daily_spot = load_daily_spot()
    daily_spot_reset = daily_spot.reset_index().rename(columns={"dt": "date_ts"})
    daily_spot_reset["date"] = daily_spot_reset["date_ts"].dt.date

    # ---- Trade-level enrichment ----
    detail = trades.merge(
        daily_spot_reset[["date", "gap_pts", "gap_pct", "gap_type", "day_return_pct", "post_gap_behavior"]],
        on="date", how="left",
    )
    detail = detail.sort_values("entry_ts").reset_index(drop=True)
    detail["cum_net"] = detail["net"].cumsum()
    detail["running_max"] = detail["cum_net"].cummax()
    detail["drawdown"] = detail["cum_net"] - detail["running_max"]
    detail.to_csv(_dir / "results" / "trades_detail.csv", index=False)

    # ---- Daily summary ----
    day_group = detail.groupby("date").agg(
        trades=("net", "count"),
        net_pnl=("net", "sum"),
        wins=("net", lambda s: (s > 0).sum()),
        gross=("gross", "sum"),
        charges=("charges", "sum"),
        avg_margin=("margin", "mean"),
        peak_margin=("margin", "max"),
    ).reset_index()
    day_group = day_group.merge(
        daily_spot_reset[["date", "open", "close", "gap_pts", "gap_pct", "gap_type", "day_return_pct",
                            "day_range_pts", "post_gap_behavior"]],
        on="date", how="left",
    )
    day_group = day_group.sort_values("date").reset_index(drop=True)
    day_group["cum_pnl"] = day_group["net_pnl"].cumsum()
    day_group["running_max"] = day_group["cum_pnl"].cummax()
    day_group["drawdown"] = day_group["cum_pnl"] - day_group["running_max"]
    day_group["win_rate_pct"] = 100 * day_group["wins"] / day_group["trades"]
    day_group.to_csv(_dir / "results" / "daily_summary.csv", index=False)

    # ---- Console report ----
    print("=" * 100)
    print("OVERALL")
    print("=" * 100)
    net_total = detail["net"].sum()
    max_dd = detail["drawdown"].min()
    max_dd_idx = detail["drawdown"].idxmin()
    max_dd_date = detail.loc[max_dd_idx, "exit_ts"]
    print(f"Net P&L: Rs {net_total:,.0f}  |  Trades: {len(detail)}  |  Win rate: {(detail['net']>0).mean()*100:.1f}%")
    print(f"Max drawdown: Rs {max_dd:,.0f}  (trough reached on {max_dd_date})")

    print("\n" + "=" * 100)
    print("TOP 5 BEST DAYS")
    print("=" * 100)
    best = day_group.sort_values("net_pnl", ascending=False).head(5)
    print(best[["date", "trades", "net_pnl", "gap_pct", "gap_type", "day_return_pct", "post_gap_behavior"]].to_string(index=False))

    print("\n" + "=" * 100)
    print("TOP 5 WORST DAYS")
    print("=" * 100)
    worst = day_group.sort_values("net_pnl").head(5)
    print(worst[["date", "trades", "net_pnl", "gap_pct", "gap_type", "day_return_pct", "post_gap_behavior"]].to_string(index=False))

    print("\n" + "=" * 100)
    print("P&L BY GAP TYPE")
    print("=" * 100)
    by_gap = day_group.groupby("gap_type").agg(
        days=("net_pnl", "count"), total_pnl=("net_pnl", "sum"), avg_pnl=("net_pnl", "mean"),
        win_days=("net_pnl", lambda s: (s > 0).sum()),
    ).reset_index()
    by_gap["win_day_rate_pct"] = 100 * by_gap["win_days"] / by_gap["days"]
    print(by_gap.to_string(index=False))

    print("\n" + "=" * 100)
    print("POST-GAP BEHAVIOR (does the day continue the gap or fade it?)")
    print("=" * 100)
    behavior_counts = day_group[day_group["gap_type"] != "flat"].groupby(["gap_type", "post_gap_behavior"]).agg(
        days=("net_pnl", "count"), total_pnl=("net_pnl", "sum"), avg_pnl=("net_pnl", "mean"),
    ).reset_index()
    print(behavior_counts.to_string(index=False))

    print(f"\nFiles written:")
    print(f"  {_dir / 'results' / 'trades_detail.csv'}")
    print(f"  {_dir / 'results' / 'daily_summary.csv'}")


if __name__ == "__main__":
    main()
