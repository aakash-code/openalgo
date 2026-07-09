#!/usr/bin/env python3
"""
Directional OPTION SELLING on the intraday 3m EMA-stack trend
=============================================================
The EMA-stack (ema8>ema21>ema50 = up, <<< = down) was the only signal with a
real, regime-robust directional edge on spot. Here we SELL options in its
direction, intraday:

    stack UP   -> Bull Put  spread (sell ~0.45Δ PE, buy ~0.20Δ LongPE)
    stack DOWN -> Bear Call spread (sell ~0.45Δ CE, buy ~0.20Δ LongCE)
    stack FLAT -> no position

Hold the spread WHILE the trend persists (close + reverse only when the stack
flips); square off at EOD. Direction + theta pull together. NO look-ahead: a
stack value known at a bar's close fills at the first option tick after that close.

Run: uv run python backtesting/aakash_signal_spread/directional_options_test.py
Env: START END LOTS SHORT_DELTA LONG_DELTA MAX_DTE SLIPPAGE_PTS CAPITAL MARGIN_PER_LOT
"""
from __future__ import annotations

import math
import os
from datetime import date, datetime, time as dtime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

START        = os.getenv("START", "2024-08-01")
END          = os.getenv("END", "2026-06-12")
LOTS         = int(os.getenv("LOTS", "1"))
SHORT_DELTA  = float(os.getenv("SHORT_DELTA", "0.45"))
LONG_DELTA   = float(os.getenv("LONG_DELTA", "0.20"))
MAX_DTE      = int(os.getenv("MAX_DTE", "999"))
SLIPPAGE_PTS = float(os.getenv("SLIPPAGE_PTS", "0"))
RATE         = float(os.getenv("RATE", "0.065"))
STRIKE_STEP  = 50
BAR_MIN      = 3
SQUAREOFF    = dtime(15, 25)
WARMUP_DAYS  = 60
CAPITAL        = float(os.getenv("CAPITAL", "300000"))
MARGIN_PER_LOT = float(os.getenv("MARGIN_PER_LOT", "40000"))

BROKERAGE_PER_ORDER = 20
STT_SELL_PCT = 0.001
TXN_CHARGE_PCT = 0.0003553
SEBI_PER_CRORE = 10
GST_PCT = 0.18
STAMP_BUY_PCT = 0.00003

_dir = Path(__file__).resolve().parent
DUCKDB_PATH = str(_dir / ".." / ".." / "db" / "historify.duckdb")


def schedule_lot(expiry: date) -> int:
    if expiry < date(2024, 11, 20):
        return 25
    if expiry < date(2026, 1, 1):
        return 75
    return 65


def _ncdf(x): return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(opt, S, K, T, r, sig):
    if T <= 0 or sig <= 0:
        return max(0.0, (S - K) if opt == "CE" else (K - S))
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    if opt == "CE":
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def implied_vol(opt, price, S, K, T, r):
    intrinsic = max(0.0, (S - K) if opt == "CE" else (K - S))
    if price <= intrinsic + 1e-6 or T <= 0:
        return None
    lo, hi = 1e-4, 5.0
    if bs_price(opt, S, K, T, r, hi) < price:
        return None
    for _ in range(50):
        mid = 0.5 * (lo + hi)
        lo, hi = (mid, hi) if bs_price(opt, S, K, T, r, mid) < price else (lo, mid)
    return 0.5 * (lo + hi)


def bs_delta(opt, S, K, T, r, sig):
    if T <= 0 or sig <= 0:
        return (1.0 if S > K else 0.0) if opt == "CE" else (-1.0 if S < K else 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    return _ncdf(d1) if opt == "CE" else _ncdf(d1) - 1.0


def leg_charges(side, en, ex, qty):
    brk = BROKERAGE_PER_ORDER * 2
    turn = (en + ex) * qty
    txn = TXN_CHARGE_PCT * turn
    sebi = SEBI_PER_CRORE * turn / 1e7
    if side == "short":
        stt, stamp = STT_SELL_PCT * en * qty, STAMP_BUY_PCT * ex * qty
    else:
        stt, stamp = STT_SELL_PCT * ex * qty, STAMP_BUY_PCT * en * qty
    return brk + stt + txn + sebi + GST_PCT * (brk + txn + sebi) + stamp


def opt_symbol(expiry, K, ot):
    return f"NIFTY{expiry.strftime('%d%b%y').upper()}{int(K)}{ot}"


def main():
    start_d = datetime.strptime(START, "%Y-%m-%d").date()
    end_d = datetime.strptime(END, "%Y-%m-%d").date()
    conn = duckdb.connect(DUCKDB_PATH, read_only=True)

    em = conn.execute("""
        SELECT expiry_date, lot_size, COUNT(*) n FROM expired_fno_contracts
        WHERE openalgo_symbol LIKE 'NIFTY%' AND contract_type IN ('CE','PE') AND lot_size IS NOT NULL
        GROUP BY expiry_date, lot_size
    """).df()
    em["expiry_date"] = pd.to_datetime(em["expiry_date"]).dt.date
    lotmap = {}
    for exp, g in em.groupby("expiry_date"):
        lotmap[exp] = int(g.sort_values("n", ascending=False)["lot_size"].iloc[0])
    expiries = sorted(lotmap.keys())

    def next_expiry(d):
        for e in expiries:
            if e >= d:
                return e
        return None

    s0 = int((datetime.combine(start_d, dtime()) - pd.Timedelta(days=WARMUP_DAYS)).timestamp())
    s1 = int(datetime.combine(end_d, dtime(23, 59)).timestamp())
    spot = conn.execute("""
        SELECT timestamp, open, high, low, close FROM market_data
        WHERE symbol='NIFTY' AND exchange='NSE_INDEX' AND interval='1m'
          AND timestamp BETWEEN ? AND ? ORDER BY timestamp
    """, [s0, s1]).df()
    spot["dt"] = (pd.to_datetime(spot["timestamp"], unit="s", utc=True)
                    .dt.tz_convert("Asia/Kolkata").dt.tz_localize(None))
    spot = spot.set_index("dt").drop(columns=["timestamp"]).between_time("09:15", "15:29")
    b3 = (spot.resample("3min", closed="left", label="left")
              .agg(open=("open", "first"), high=("high", "max"),
                   low=("low", "min"), close=("close", "last")).dropna()
              .between_time("09:15", "15:25"))
    for ln in (8, 21, 50):
        b3[f"e{ln}"] = b3["close"].ewm(span=ln, adjust=False).mean()
    b3["stack"] = np.where((b3.e8 > b3.e21) & (b3.e21 > b3.e50), 1,
                   np.where((b3.e8 < b3.e21) & (b3.e21 < b3.e50), -1, 0))

    opt_cache: dict[str, pd.DataFrame] = {}

    def load_opt(sym):
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

    def px_after(expiry, K, ot, ts):
        df = load_opt(opt_symbol(expiry, K, ot))
        if df.empty:
            return None
        i = df.index.searchsorted(ts, side="left")
        if i >= len(df):
            return float(df["close"].iloc[-1])
        return float(df["close"].iloc[i])

    def pick(expiry, spot_px, ts, ot, target):
        T = max((datetime.combine(expiry, dtime(15, 30)) - ts.to_pydatetime()).total_seconds(), 60) / (365 * 86400)
        atm = round(spot_px / STRIKE_STEP) * STRIKE_STEP
        best = None
        for off in range(-14, 15):
            K = atm + off * STRIKE_STEP
            px = px_after(expiry, K, ot, ts)
            if px is None or px <= 0.05:
                continue
            iv = implied_vol(ot, px, spot_px, K, T, RATE)
            if iv is None:
                continue
            d = abs(bs_delta(ot, spot_px, K, T, RATE, iv))
            if best is None or abs(d - target) < abs(best[2] - target):
                best = (K, px, d)
        return best

    dd = conn.execute("""
        SELECT DISTINCT to_timestamp(timestamp)::DATE d FROM market_data
        WHERE exchange='NFO' AND symbol LIKE 'NIFTY%' AND interval='1m' AND timestamp BETWEEN ? AND ?
        ORDER BY d
    """, [int(datetime.combine(start_d, dtime()).timestamp()), s1]).df()
    dd["d"] = pd.to_datetime(dd["d"]).dt.date
    days = [d for d in dd["d"].tolist() if start_d <= d <= end_d]

    print("=" * 90)
    print(f"DIRECTIONAL OPTION SELLING (3m EMA-stack)  {START}..{END} | short~{SHORT_DELTA}Δ "
          f"long~{LONG_DELTA}Δ | lots={LOTS} | slip={SLIPPAGE_PTS}pt"
          f"{' | DTE<=' + str(MAX_DTE) if MAX_DTE < 999 else ''}")
    print("=" * 90)

    trades = []
    daily = []
    s = SLIPPAGE_PTS
    bar_delta = pd.Timedelta(minutes=BAR_MIN)
    for d in days:
        expiry = next_expiry(d)
        if expiry is None or (expiry - d).days > MAX_DTE:
            daily.append({"day": d, "net": 0.0, "n": 0}); continue
        qty = lotmap.get(expiry, schedule_lot(expiry)) * LOTS
        dayb = b3[b3.index.date == d]
        if len(dayb) < 5:
            daily.append({"day": d, "net": 0.0, "n": 0}); continue
        eod_ts = pd.Timestamp(datetime.combine(d, SQUAREOFF))

        pos = 0           # current held direction
        leg = None        # (ot, sK, sEn, lK, lEn, entry_ts, spot_entry)
        day_net = 0.0; n = 0
        st = dayb["stack"].values; idx = dayb.index; cl = dayb["close"].values

        def close_leg(leg, ts, reason):
            nonlocal day_net, n
            ot, sK, sEn, lK, lEn, ets, spe = leg
            sEx = px_after(expiry, sK, ot, ts) or sEn
            lEx = px_after(expiry, lK, ot, ts) or lEn
            gross = ((sEn - s) - (sEx + s)) * qty + ((lEx - s) - (lEn + s)) * qty
            ch = leg_charges("short", sEn, sEx, qty) + leg_charges("long", lEn, lEx, qty)
            net = gross - ch
            day_net += net; n += 1
            trades.append({"day": str(d), "dir": "BULL" if ot == "PE" else "BEAR",
                           "entry": ets.strftime("%H:%M"), "exit": ts.strftime("%H:%M"),
                           "sell_K": sK, "buy_K": lK, "credit": round(sEn - lEn, 1),
                           "qty": qty, "net": round(net, 0), "reason": reason})

        for i in range(len(dayb)):
            si = int(st[i])
            if si == pos:
                continue
            close_ts = idx[i] + bar_delta            # bar close (no look-ahead)
            if leg is not None:                      # trend changed -> close
                close_leg(leg, close_ts, "flip"); leg = None
            if si != 0:                              # open new directional spread
                ot = "PE" if si == 1 else "CE"
                spx = float(cl[i])
                sh = pick(expiry, spx, close_ts, ot, SHORT_DELTA)
                lg = pick(expiry, spx, close_ts, ot, LONG_DELTA)
                if sh and lg and sh[0] != lg[0]:
                    leg = (ot, sh[0], sh[1], lg[0], lg[1], close_ts, spx)
            pos = si
        if leg is not None:
            close_leg(leg, eod_ts, "eod")
        daily.append({"day": d, "net": day_net, "n": n})

    conn.close()

    dfd = pd.DataFrame(daily); dft = pd.DataFrame(trades)
    total = float(dfd["net"].sum())
    dfd["cum"] = dfd["net"].cumsum(); maxdd = float((dfd["cum"] - dfd["cum"].cummax()).min())
    nt = len(dft); wins = int((dft["net"] > 0).sum()) if nt else 0
    n_days = int((dfd["n"] > 0).sum())

    print(f"Trades: {nt} over {n_days} active days ({nt/max(1,n_days):.1f}/day) | "
          f"win {100*wins/max(1,nt):.1f}% | Net Rs {total:,.0f} | MaxDD Rs {maxdd:,.0f}")
    if nt:
        dft["date"] = pd.to_datetime(dft["day"])
        yr = dft.groupby(dft.date.dt.year).agg(trades=("net", "size"),
              wins=("net", lambda x: (x > 0).sum()), net=("net", "sum")).reset_index()
        print("\nYEARLY")
        print(f"{'Year':<7}{'Trades':>8}{'Win%':>7}{'Net':>14}")
        for _, r in yr.iterrows():
            print(f"{int(r.date):<7}{int(r.trades):>8}{100*r.wins/r.trades:>6.0f}%{r.net:>14,.0f}")
        mo = dft.groupby(dft.date.dt.strftime('%Y-%m')).net.sum()
        print("\nMONTHLY net:", " ".join(f"{k}:{v:,.0f}" for k, v in mo.items()))
    roi = 100 * total / CAPITAL
    print(f"\nReturn on Rs {CAPITAL:,.0f} capital: {roi:.0f}%  | margin/lot ~Rs {MARGIN_PER_LOT:,.0f}")
    out = _dir / "results"; out.mkdir(exist_ok=True)
    if nt:
        dft.drop(columns=["date"]).to_csv(out / f"directional_opt_{START}_{END}.csv", index=False)
        print(f"Saved -> results/directional_opt_{START}_{END}.csv")


if __name__ == "__main__":
    main()
