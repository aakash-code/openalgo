#!/usr/bin/env python3
"""
Aakash 3m Signals -> Delta-Targeted Credit Spreads (single-day test)
====================================================================
Drives the ported "Intraday BUY/SELL & AUTO SL" signal engine (filter OFF,
full state machine: active-line blocking, zone suppression, SL-mode latch,
forced-opposite-on-SL) and turns each signal into a defined-risk credit
spread on the nearest weekly NIFTY expiry:

    BUY  signal -> Bull Put Spread : sell ~0.45-delta PE, buy ~0.20-delta PE
    SELL signal -> Bear Call Spread: sell ~0.45-delta CE, buy ~0.20-delta CE

Strikes are chosen by Black-Scholes delta (IV implied from each option's own
1m price). A spread is opened at the signal bar and closed at the NEXT signal
(the engine flips on SL) or squared off at session end. P&L is net of charges.

NOTE: June-2026 option data is not in the DB (latest expiry 2026-05-26, and the
June weekly has not expired yet). This test runs on 2026-05-22 (a Friday non-
expiry, nearest weekly 2026-05-26) — the structural twin of June 12 — to prove
the pipeline. Re-point with DAY=... once June option data is downloaded.

Run: uv run python backtesting/nifty_options_selling/aakash_options_spread_test.py
Env: DAY=YYYY-MM-DD  LOTS=1  SHORT_DELTA=0.45  LONG_DELTA=0.20  RATE=0.065
"""
from __future__ import annotations

import math
import os
from datetime import date, datetime, time as dtime
from pathlib import Path

import duckdb
import pandas as pd

from aakash_signal_3m_replay import load_3m, replay

# ---- Config ----------------------------------------------------------------
DAY         = os.getenv("DAY", "2026-05-22")
LOTS        = int(os.getenv("LOTS", "1"))
SHORT_DELTA = float(os.getenv("SHORT_DELTA", "0.45"))   # target short-leg |delta|
LONG_DELTA  = float(os.getenv("LONG_DELTA", "0.20"))    # target long-leg |delta|
RATE        = float(os.getenv("RATE", "0.065"))         # risk-free (India ~6.5%)
STRIKE_STEP = 50
SQUAREOFF   = dtime(15, 24)

# Charges (same model as the other backtests here)
BROKERAGE_PER_ORDER = 20
STT_SELL_PCT        = 0.001
TXN_CHARGE_PCT      = 0.0003553
SEBI_PER_CRORE      = 10
GST_PCT             = 0.18
STAMP_BUY_PCT       = 0.00003

_dir = Path(__file__).resolve().parent
DUCKDB_PATH = str(_dir / ".." / ".." / "db" / "historify.duckdb")


# ---- Black-Scholes ---------------------------------------------------------
def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(opt: str, S: float, K: float, T: float, r: float, sig: float) -> float:
    if T <= 0 or sig <= 0:
        return max(0.0, (S - K) if opt == "CE" else (K - S))
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    if opt == "CE":
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def implied_vol(opt: str, price: float, S: float, K: float, T: float, r: float):
    """Bisection IV solve; None if price below intrinsic / no solution."""
    intrinsic = max(0.0, (S - K) if opt == "CE" else (K - S))
    if price <= intrinsic + 1e-6 or T <= 0:
        return None
    lo, hi = 1e-4, 5.0
    if bs_price(opt, S, K, T, r, hi) < price:
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if bs_price(opt, S, K, T, r, mid) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def bs_delta(opt: str, S: float, K: float, T: float, r: float, sig: float) -> float:
    if T <= 0 or sig <= 0:
        return (1.0 if S > K else 0.0) if opt == "CE" else (-1.0 if S < K else 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    return _ncdf(d1) if opt == "CE" else _ncdf(d1) - 1.0


# ---- Charges ---------------------------------------------------------------
def leg_charges(side: str, entry_px: float, exit_px: float, qty: int) -> float:
    brokerage = BROKERAGE_PER_ORDER * 2
    turnover  = (entry_px + exit_px) * qty
    txn       = TXN_CHARGE_PCT * turnover
    sebi      = SEBI_PER_CRORE * turnover / 1e7
    if side == "short":
        stt, stamp = STT_SELL_PCT * entry_px * qty, STAMP_BUY_PCT * exit_px * qty
    else:
        stt, stamp = STT_SELL_PCT * exit_px * qty, STAMP_BUY_PCT * entry_px * qty
    return brokerage + stt + txn + sebi + gst_of(brokerage, txn, sebi) + stamp


def gst_of(brokerage, txn, sebi):
    return GST_PCT * (brokerage + txn + sebi)


# ---- Main ------------------------------------------------------------------
def main():
    day_d = datetime.strptime(DAY, "%Y-%m-%d").date()
    conn = duckdb.connect(DUCKDB_PATH, read_only=True)

    # Nearest weekly expiry >= the day, plus lot size.
    exp_row = conn.execute("""
        SELECT expiry_date, MIN(lot_size) lot
        FROM expired_fno_contracts
        WHERE openalgo_symbol LIKE 'NIFTY%' AND contract_type IN ('CE','PE')
          AND expiry_date >= ? GROUP BY expiry_date ORDER BY expiry_date LIMIT 1
    """, [day_d]).fetchone()
    if not exp_row:
        print(f"No option expiry on/after {DAY} in DB — cannot test legs here.")
        conn.close()
        return
    expiry, lot = exp_row[0], int(exp_row[1])
    qty = lot * LOTS

    # Signals (filter OFF, full state machine).
    bars, day_df = load_3m(conn, DAY)
    signals = replay(day_df, use_trend_filter=False, use_atr_sl=False)

    # Option price cache + lookup.
    opt_cache: dict[str, pd.DataFrame] = {}

    def load_opt(sym: str) -> pd.DataFrame:
        if sym not in opt_cache:
            df = conn.execute("""
                SELECT timestamp, close FROM market_data
                WHERE symbol=? AND exchange='NFO' AND interval='1m' ORDER BY timestamp
            """, [sym]).df()
            if not df.empty:
                df["dt"] = (pd.to_datetime(df["timestamp"], unit="s", utc=True)
                              .dt.tz_convert("Asia/Kolkata").dt.tz_localize(None))
                df = df.set_index("dt").drop(columns=["timestamp"])
            opt_cache[sym] = df
        return opt_cache[sym]

    def opt_price(strike: int, ot: str, ts: pd.Timestamp):
        sym = f"NIFTY{expiry.strftime('%d%b%y').upper()}{int(strike)}{ot}"
        df = load_opt(sym)
        if df.empty:
            return None
        i = df.index.searchsorted(ts, side="right") - 1
        return float(df["close"].iloc[i]) if i >= 0 else None

    def tte_years(ts: pd.Timestamp) -> float:
        secs = (datetime.combine(expiry, dtime(15, 30)) - ts.to_pydatetime()).total_seconds()
        return max(secs, 60) / (365.0 * 24 * 3600)

    def pick_by_delta(spot, ts, ot, target):
        """Scan strikes, return (strike, price, delta) with |delta| nearest target."""
        T = tte_years(ts)
        atm = round(spot / STRIKE_STEP) * STRIKE_STEP
        best = None
        for off in range(-12, 13):  # +/- 600 pts around ATM
            K = atm + off * STRIKE_STEP
            px = opt_price(K, ot, ts)
            if px is None or px <= 0.05:
                continue
            iv = implied_vol(ot, px, spot, K, T, RATE)
            if iv is None:
                continue
            d = abs(bs_delta(ot, spot, K, T, RATE, iv))
            if best is None or abs(d - target) < abs(best[2] - target):
                best = (K, px, d)
        return best

    # Build spreads: each signal opens; the next signal (or EOD) closes.
    print("=" * 88)
    print(f"AAKASH 3m SIGNALS -> DELTA CREDIT SPREADS   day={DAY}  expiry={expiry}  "
          f"lot={lot} x{LOTS}={qty}")
    print(f"Targets: short |delta|~{SHORT_DELTA}, long |delta|~{LONG_DELTA}   "
          f"(BUY->BullPut PE, SELL->BearCall CE)")
    print("=" * 88)
    if not signals:
        print("No signals on this day.")
        conn.close()
        return

    eod_ts = day_df.index[day_df.index.searchsorted(
        pd.Timestamp(datetime.combine(day_d, SQUAREOFF)), side="right") - 1]

    trades = []
    for n, sig in enumerate(signals):
        entry_ts = pd.Timestamp(datetime.combine(day_d, datetime.strptime(sig["time"], "%H:%M").time()))
        spot = sig["spot"]
        ot = "PE" if sig["side"] == "BUY" else "CE"
        short = pick_by_delta(spot, entry_ts, ot, SHORT_DELTA)
        long  = pick_by_delta(spot, entry_ts, ot, LONG_DELTA)
        if short is None or long is None or short[0] == long[0]:
            trades.append({"time": sig["time"], "side": sig["side"], "rule": sig["rule"],
                           "note": "no valid strikes", "net": 0.0})
            continue

        exit_ts = (pd.Timestamp(datetime.combine(
            day_d, datetime.strptime(signals[n + 1]["time"], "%H:%M").time()))
            if n + 1 < len(signals) else eod_ts)
        if exit_ts <= entry_ts:
            exit_ts = eod_ts

        sK, sEntry, sDelta = short
        lK, lEntry, lDelta = long
        sExit = opt_price(sK, ot, exit_ts) or sEntry
        lExit = opt_price(lK, ot, exit_ts) or lEntry

        credit = sEntry - lEntry
        gross = (sEntry - sExit) * qty + (lExit - lEntry) * qty  # short + long
        ch = leg_charges("short", sEntry, sExit, qty) + leg_charges("long", lEntry, lExit, qty)
        net = gross - ch
        trades.append({
            "time": sig["time"], "side": sig["side"], "rule": sig["rule"],
            "spot": spot, "struct": "BullPut" if ot == "PE" else "BearCall",
            "sell_K": sK, "sellΔ": round(sDelta, 2), "buy_K": lK, "buyΔ": round(lDelta, 2),
            "credit": round(credit, 1), "exit": exit_ts.time().strftime("%H:%M"),
            "gross": round(gross, 0), "charges": round(ch, 0), "net": round(net, 0),
        })

    conn.close()

    # Report
    hdr = (f"{'Time':<6}{'Side':<5}{'Rule':<14}{'Struct':<9}"
           f"{'SellK':>7}{'Δ':>6}{'BuyK':>7}{'Δ':>6}{'Cred':>7}{'Exit':>6}{'Net':>10}")
    print(hdr)
    print("-" * 88)
    tot = 0.0
    for t in trades:
        if "sell_K" not in t:
            print(f"{t['time']:<6}{t['side']:<5}{t['rule']:<14}{t.get('note',''):<40}")
            continue
        tot += t["net"]
        print(f"{t['time']:<6}{t['side']:<5}{t['rule']:<14}{t['struct']:<9}"
              f"{t['sell_K']:>7}{t['sellΔ']:>6.2f}{t['buy_K']:>7}{t['buyΔ']:>6.2f}"
              f"{t['credit']:>7.1f}{t['exit']:>6}{t['net']:>10,.0f}")
    print("-" * 88)
    valid = [t for t in trades if 'sell_K' in t]
    wins = sum(1 for t in valid if t["net"] > 0)
    print(f"Spreads: {len(valid)}  | wins: {wins}/{len(valid)}  | "
          f"NET P&L (per {LOTS} lot, after charges): Rs {tot:,.0f}")
    print(f"Credit collected per spread averages Rs {sum(t['credit'] for t in valid)/max(1,len(valid))*qty:,.0f} "
          f"(x{qty} qty).  Defined risk = (SellK-BuyK width - credit) x qty per spread.")

    out = _dir / "results" / f"aakash_spreads_{DAY}.csv"
    out.parent.mkdir(exist_ok=True)
    pd.DataFrame(valid).to_csv(out, index=False)
    print(f"\nDetail: {out}")


if __name__ == "__main__":
    main()
