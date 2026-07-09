#!/usr/bin/env python3
"""
NIFTY Expiry-Day (0DTE) Premium-Selling Backtest
================================================
Sells ATM premium on each weekly NIFTY expiry day and harvests intraday
theta decay. The trade is *non-directional* — the bet is that NIFTY ends the
day near where it started, not that it goes a particular way. The enemy is a
TREND day (a one-way move), defended by (a) an optional entry-time regime
filter and (b) a per-position stop loss.

Runs a 2x2 matrix on the same weekly expiries so each effect is isolated:

    STRUCTURE                 ENTRY GATE
    ---------                 ----------
    Naked straddle    x   No filter   (sell every expiry)
    Iron fly          x   Drive filter (skip trend-opening days)

  * Naked straddle = sell ATM CE + ATM PE.            Uncapped tail, SL-defended.
  * Iron fly       = sell ATM CE+PE, buy ATM+/-WING.  Tail capped by the wings.
  * Drive filter   = at entry time, skip the day if NIFTY has already moved
                     > DRIVE_THRESHOLD off the open (an opening trend = danger).

ENTRY / EXIT
  Entry : ENTRY_TIME on expiry day. ATM = round(spot/50)*50 at that bar.
  Exit  : per-position stop (combined buy-back >= credit*(1+SL_MULT)),
          else square off at SQUAREOFF_TIME (realistic intraday close, not
          theoretical settlement).

NO LOOKAHEAD
  ATM fixed from spot at ENTRY_TIME only. Entry fills at the entry-bar close.
  SL is checked forward on each 1m bar; exit fills at that bar's close.

CHARGES  Per leg: brokerage Rs 20/order + STT (sell side) + txn + SEBI + GST + stamp.
DATA     Historify DuckDB — NIFTY 1m (NSE_INDEX) + option 1m (NFO, w/ lot sizes).

Run:  uv run python backtesting/nifty_options_selling/expiry_day_0dte_backtest.py
Env knobs: ENTRY_TIME, OR_WINDOW_MIN, DRIVE_THRESHOLD, WING_PTS, SL_MULT,
           SQUAREOFF_TIME, LOTS, START_DATE, END_DATE
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, time as dtime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

# ============================================================================
# CONFIG  (env-overridable)
# ============================================================================
LOTS            = int(os.getenv("LOTS", "1"))            # multiples of the lot size
STRIKE_INTERVAL = 50

ENTRY_TIME      = os.getenv("ENTRY_TIME", "09:30")       # decision/entry time (both arms)
OR_WINDOW_MIN   = int(os.getenv("OR_WINDOW_MIN", "15"))  # opening window for the drive read
DRIVE_THRESHOLD = float(os.getenv("DRIVE_THRESHOLD", "0.004"))  # 0.4% open->entry move = skip
WING_PTS        = int(os.getenv("WING_PTS", "200"))      # iron-fly wing distance (points)
SL_MULT         = float(os.getenv("SL_MULT", "1.0"))     # stop when loss >= SL_MULT * credit
SQUAREOFF_TIME  = os.getenv("SQUAREOFF_TIME", "15:15")   # forced intraday exit

START_DATE      = os.getenv("START_DATE")                # optional YYYY-MM-DD filter
END_DATE        = os.getenv("END_DATE")

# Charges (matches the other backtests in this folder; STT 0.1% sell = post-Oct-2024)
BROKERAGE_PER_ORDER = 20
STT_SELL_PCT        = 0.001
TXN_CHARGE_PCT      = 0.0003553
SEBI_PER_CRORE      = 10
GST_PCT             = 0.18
STAMP_BUY_PCT       = 0.00003

_script_dir = Path(__file__).resolve().parent
DUCKDB_PATH = str(_script_dir / ".." / ".." / "db" / "historify.duckdb")


def _parse_t(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


ENTRY_T     = _parse_t(ENTRY_TIME)
SQUAREOFF_T = _parse_t(SQUAREOFF_TIME)


# ============================================================================
# Symbol + charges
# ============================================================================
def build_option_symbol(expiry: date, strike: int, opt_type: str) -> str:
    return f"NIFTY{expiry.strftime('%d%b%y').upper()}{int(strike)}{opt_type.upper()}"


def leg_charges(side: str, entry_px: float, exit_px: float, qty: int) -> float:
    """Per-leg full charge model. side = 'short' (sell-to-open) or 'long' (buy-to-open)."""
    brokerage = BROKERAGE_PER_ORDER * 2                 # one open + one close order
    turnover  = (entry_px + exit_px) * qty
    txn       = TXN_CHARGE_PCT * turnover
    sebi      = SEBI_PER_CRORE * turnover / 1e7
    if side == "short":
        stt   = STT_SELL_PCT * entry_px * qty           # STT on the sell (entry)
        stamp = STAMP_BUY_PCT * exit_px * qty           # stamp on the buy (exit)
    else:
        stt   = STT_SELL_PCT * exit_px * qty            # STT on the sell (exit)
        stamp = STAMP_BUY_PCT * entry_px * qty          # stamp on the buy (entry)
    gst = GST_PCT * (brokerage + txn + sebi)
    return brokerage + stt + txn + sebi + gst + stamp


# ============================================================================
# Main
# ============================================================================
def main():
    if not Path(DUCKDB_PATH).exists():
        print(f"ERROR: DuckDB not found at {DUCKDB_PATH}")
        sys.exit(1)

    conn = duckdb.connect(DUCKDB_PATH, read_only=True)

    # -- 1. Expiries + lot sizes ------------------------------------------
    meta = conn.execute("""
        SELECT expiry_date, MIN(lot_size) AS lot_size
        FROM expired_fno_contracts
        WHERE openalgo_symbol LIKE 'NIFTY%'
          AND contract_type IN ('CE','PE') AND lot_size IS NOT NULL
        GROUP BY expiry_date ORDER BY expiry_date
    """).df()
    meta["expiry_date"] = pd.to_datetime(meta["expiry_date"]).dt.date
    lotmap = dict(zip(meta["expiry_date"], meta["lot_size"]))
    all_expiries = meta["expiry_date"].tolist()

    # Monthly = last expiry of its calendar month; weekly = everything else.
    last_of_month: dict[tuple[int, int], date] = {}
    for e in all_expiries:
        last_of_month[(e.year, e.month)] = e  # ascending -> ends on the last one
    monthly = set(last_of_month.values())
    weekly = [e for e in all_expiries if e not in monthly]

    if START_DATE:
        sd = datetime.strptime(START_DATE, "%Y-%m-%d").date()
        weekly = [e for e in weekly if e >= sd]
    if END_DATE:
        ed = datetime.strptime(END_DATE, "%Y-%m-%d").date()
        weekly = [e for e in weekly if e <= ed]

    print("=" * 78)
    print("NIFTY EXPIRY-DAY (0DTE) PREMIUM SELLING — 2x2 backtest")
    print("=" * 78)
    print(f"  Weekly expiries : {len(weekly)}  ({weekly[0]} -> {weekly[-1]})")
    print(f"  Entry/exit      : {ENTRY_TIME} -> {SQUAREOFF_TIME}   | Lots={LOTS}")
    print(f"  Drive filter    : skip if |open->{ENTRY_TIME}| move > {DRIVE_THRESHOLD*100:.2f}% "
          f"(OR window {OR_WINDOW_MIN}m)")
    print(f"  Iron-fly wings  : ATM +/- {WING_PTS}   | Stop loss = {SL_MULT:.2f}x credit")
    print("=" * 78)

    # -- 2. NIFTY 1m spot --------------------------------------------------
    end_ts = int(datetime.combine(weekly[-1], datetime.max.time()).timestamp())
    df1m = conn.execute("""
        SELECT timestamp, open, high, low, close
        FROM market_data
        WHERE symbol='NIFTY' AND exchange='NSE_INDEX' AND interval='1m'
          AND timestamp <= ?
        ORDER BY timestamp
    """, [end_ts]).df()
    df1m["dt"] = (pd.to_datetime(df1m["timestamp"], unit="s", utc=True)
                    .dt.tz_convert("Asia/Kolkata").dt.tz_localize(None))
    df1m = df1m.set_index("dt").drop(columns=["timestamp"])

    # -- 3. Option cache ---------------------------------------------------
    opt_cache: dict[str, pd.DataFrame] = {}

    def load_option(sym: str) -> pd.DataFrame:
        if sym in opt_cache:
            return opt_cache[sym]
        df = conn.execute("""
            SELECT timestamp, close FROM market_data
            WHERE symbol=? AND exchange='NFO' AND interval='1m'
            ORDER BY timestamp
        """, [sym]).df()
        if not df.empty:
            df["dt"] = (pd.to_datetime(df["timestamp"], unit="s", utc=True)
                          .dt.tz_convert("Asia/Kolkata").dt.tz_localize(None))
            df = df.set_index("dt").drop(columns=["timestamp"])
        opt_cache[sym] = df
        return df

    def price_at(sym: str, ts: pd.Timestamp):
        """Last option close at-or-before ts, or None if no quote yet."""
        df = load_option(sym)
        if df.empty:
            return None
        idx = df.index.searchsorted(ts, side="right") - 1
        if idx < 0:
            return None
        return float(df["close"].iloc[idx])

    # -- 4. Simulate one expiry for a given structure ----------------------
    def simulate_day(exp: date, structure: str, use_filter: bool):
        """Return dict(traded, skipped_reason, pnl, ...) for one expiry."""
        day = df1m[df1m.index.date == exp]
        if day.empty:
            return {"traded": False, "reason": "no_spot"}
        day = day.between_time("09:15", "15:29")
        if day.empty:
            return {"traded": False, "reason": "no_spot"}

        day_open = float(day.iloc[0]["open"])
        entry_dt = datetime.combine(exp, ENTRY_T)
        entry_bars = day[day.index <= entry_dt]
        if entry_bars.empty:
            return {"traded": False, "reason": "no_entry_bar"}
        spot_entry = float(entry_bars.iloc[-1]["close"])
        entry_ts = entry_bars.index[-1]

        # Regime gate: opening drive off the day's open.
        drive = abs(spot_entry - day_open) / day_open
        if use_filter and drive > DRIVE_THRESHOLD:
            return {"traded": False, "reason": "trend_skip", "drive": drive}

        atm = int(round(spot_entry / STRIKE_INTERVAL) * STRIKE_INTERVAL)
        qty = int(lotmap.get(exp, 75)) * LOTS

        # Build legs: (symbol, side)
        legs = [(build_option_symbol(exp, atm, "CE"), "short"),
                (build_option_symbol(exp, atm, "PE"), "short")]
        if structure == "ironfly":
            legs += [(build_option_symbol(exp, atm + WING_PTS, "CE"), "long"),
                     (build_option_symbol(exp, atm - WING_PTS, "PE"), "long")]

        # Entry fills.
        entry_px = {}
        for sym, _ in legs:
            px = price_at(sym, entry_ts)
            if px is None or px <= 0:
                return {"traded": False, "reason": f"no_entry_px:{sym}"}
            entry_px[sym] = px

        # Net credit received (short premium - long premium), in points.
        credit = sum((entry_px[s] if side == "short" else -entry_px[s])
                     for s, side in legs)
        if credit <= 0:
            return {"traded": False, "reason": "nonpos_credit"}

        # Forward walk: entry+1min .. squareoff. SL on combined buy-back cost.
        squareoff_dt = datetime.combine(exp, SQUAREOFF_T)
        fwd = day[(day.index > entry_ts) & (day.index <= squareoff_dt)]
        exit_ts = fwd.index[-1] if len(fwd) else entry_ts
        exit_reason = "squareoff"
        for ts in fwd.index:
            cost = 0.0
            for sym, side in legs:
                px = price_at(sym, ts)
                if px is None:
                    px = entry_px[sym]
                cost += px if side == "short" else -px
            # cost = current buy-back cost of the position; loss = cost - credit
            if cost - credit >= SL_MULT * credit:
                exit_ts, exit_reason = ts, "stop_loss"
                break

        # Exit fills + P&L + charges.
        gross = 0.0
        charges = 0.0
        for sym, side in legs:
            ex = price_at(sym, exit_ts)
            if ex is None:
                ex = entry_px[sym]
            en = entry_px[sym]
            gross += (en - ex) * qty if side == "short" else (ex - en) * qty
            charges += leg_charges(side, en, ex, qty)
        net = gross - charges

        return {
            "traded": True, "expiry": exp, "structure": structure,
            "filter": use_filter, "atm": atm, "qty": qty, "credit": credit,
            "drive": drive, "exit_reason": exit_reason,
            "exit_time": exit_ts.time().strftime("%H:%M"),
            "gross": gross, "charges": charges, "net": net,
        }

    # -- 5. Run the 2x2 ----------------------------------------------------
    variants = [
        ("naked",   False, "Naked straddle / no filter"),
        ("naked",   True,  "Naked straddle / drive filter"),
        ("ironfly", False, "Iron fly / no filter"),
        ("ironfly", True,  "Iron fly / drive filter"),
    ]

    all_rows = []
    summaries = []
    for structure, use_filter, label in variants:
        trades = []
        skips = 0
        for exp in weekly:
            r = simulate_day(exp, structure, use_filter)
            if r.get("traded"):
                r["variant"] = label
                trades.append(r)
                all_rows.append(r)
            elif r.get("reason") == "trend_skip":
                skips += 1
        summaries.append(_summarize(label, trades, skips, len(weekly)))

    conn.close()

    # -- 6. Report ---------------------------------------------------------
    _print_comparison(summaries)

    out = _script_dir / "results" / "expiry_day_0dte_trades.csv"
    out.parent.mkdir(exist_ok=True)
    if all_rows:
        cols = ["variant", "expiry", "structure", "filter", "atm", "qty",
                "credit", "drive", "exit_reason", "exit_time",
                "gross", "charges", "net"]
        pd.DataFrame(all_rows)[cols].to_csv(out, index=False)
        print(f"\nPer-trade detail written: {out}")


def _summarize(label, trades, skips, n_expiries):
    if not trades:
        return {"label": label, "n": 0, "skips": skips}
    df = pd.DataFrame(trades).sort_values("expiry").reset_index(drop=True)
    net = df["net"].values
    eq = np.cumsum(net)
    peak = np.maximum.accumulate(eq)
    max_dd = float((eq - peak).min())
    wins = int((net > 0).sum())
    daily = net  # one trade per expiry
    sharpe = float(np.mean(daily) / np.std(daily) * np.sqrt(50)) if np.std(daily) > 0 else 0.0
    sl_hits = int((df["exit_reason"] == "stop_loss").sum())
    return {
        "label": label, "n": len(df), "skips": skips, "n_expiries": n_expiries,
        "total": float(net.sum()), "avg": float(net.mean()),
        "win_rate": 100.0 * wins / len(df), "max_dd": max_dd,
        "worst": float(net.min()), "best": float(net.max()),
        "sl_hits": sl_hits, "avg_credit": float(df["credit"].mean()),
        "total_charges": float(df["charges"].sum()),
    }


def _print_comparison(summaries):
    print("\n" + "=" * 78)
    print("COMPARISON  (per 1 lot, net of charges)")
    print("=" * 78)
    hdr = (f"{'Variant':<32}{'Trades':>7}{'Skip':>5}{'Win%':>6}"
           f"{'NetPnL':>11}{'Avg/exp':>9}{'MaxDD':>10}{'Worst':>9}{'SLhit':>6}")
    print(hdr)
    print("-" * 78)
    for s in summaries:
        if s["n"] == 0:
            print(f"{s['label']:<32}{'0':>7}{s.get('skips',0):>5}  (no trades)")
            continue
        print(f"{s['label']:<32}{s['n']:>7}{s['skips']:>5}{s['win_rate']:>6.0f}"
              f"{s['total']:>11,.0f}{s['avg']:>9,.0f}{s['max_dd']:>10,.0f}"
              f"{s['worst']:>9,.0f}{s['sl_hits']:>6}")
    print("-" * 78)
    print("Avg/exp = avg net P&L per traded expiry. Worst = single worst expiry.")
    print("Drive-filter rows trade fewer expiries (Skip = trend-opening days avoided).")


if __name__ == "__main__":
    main()
