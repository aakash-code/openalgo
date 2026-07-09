#!/usr/bin/env python3
"""
EMA Zigzag (candle fully above/below EMA21 & EMA63) -> ITM OPTION BUYING
=======================================================================
Ports the entry + trailing-stop logic of "EMA Buy/Sell & Smart Zones" and trades
it by BUYING options (the right instrument for a directional trend-ride):

  BUY  signal (green, low > EMA21 & EMA63) -> buy ITM CALL
  SELL signal (red,  high < EMA21 & EMA63) -> buy ITM PUT
  (zigzag: signals alternate buy/sell)

Trade management (per the indicator, defaults):
  initial SL = min(close-10, low)  [buy] / max(close+10, high) [sell]  (smart-SL)
  T1 = close +/- 30 ; then TRAIL SL & TP by 20 per step as price advances.
  Exit on: trailing-SL hit, opposite signal, or EOD square-off.

ITM = 1 strike in-the-money (CALL strike = ATM-50, PUT strike = ATM+50).
Expiry = nearest weekly (any DTE). Long option P&L = (exit-entry) x qty - charges.
NO look-ahead: signal known at candle close -> fill at first option tick after.

Run:  uv run python backtesting/aakash_signal_spread/ema_zigzag_option_buying.py
      DAY=2026-06-12 uv run python ... ema_zigzag_option_buying.py   (single-day detail)
Env:  START END LOTS EMA1 EMA2 SL_PTS TP_PTS TRAIL_PTS ITM_STRIKES TF_MIN SLIPPAGE_PTS
"""
from __future__ import annotations

import math
import os
from datetime import date, datetime, time as dtime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

DAY          = os.getenv("DAY")                       # set -> single-day detail
START        = os.getenv("START", "2024-08-01")
END          = os.getenv("END", "2026-06-12")
LOTS         = int(os.getenv("LOTS", "1"))
EMA1         = int(os.getenv("EMA1", "21"))
EMA2         = int(os.getenv("EMA2", "63"))
SL_PTS       = float(os.getenv("SL_PTS", "10"))
TP_PTS       = float(os.getenv("TP_PTS", "30"))
TRAIL_PTS    = float(os.getenv("TRAIL_PTS", "20"))
ITM_STRIKES  = int(os.getenv("ITM_STRIKES", "1"))     # strikes in-the-money
TF_MIN       = int(os.getenv("TF_MIN", "3"))
SLIPPAGE_PTS = float(os.getenv("SLIPPAGE_PTS", "0"))
STRIKE_STEP  = 50
SQUAREOFF    = dtime(15, 25)
WARMUP_DAYS  = 30
CAPITAL      = float(os.getenv("CAPITAL", "300000"))

BROKERAGE_PER_ORDER = 20
STT_SELL_PCT = 0.001
TXN_CHARGE_PCT = 0.0003553
SEBI_PER_CRORE = 10
GST_PCT = 0.18
STAMP_BUY_PCT = 0.00003

_dir = Path(__file__).resolve().parent
DUCKDB_PATH = str(_dir / ".." / ".." / "db" / "historify.duckdb")


def schedule_lot(expiry: date) -> int:
    return 25 if expiry < date(2024, 11, 20) else (75 if expiry < date(2026, 1, 1) else 65)


def long_charges(entry, exit_, qty):
    """Buy at entry, sell at exit. STT on sell side, stamp on buy side."""
    brk = BROKERAGE_PER_ORDER * 2
    turn = (entry + exit_) * qty
    txn = TXN_CHARGE_PCT * turn
    sebi = SEBI_PER_CRORE * turn / 1e7
    stt = STT_SELL_PCT * exit_ * qty
    stamp = STAMP_BUY_PCT * entry * qty
    return brk + stt + txn + sebi + GST_PCT * (brk + txn + sebi) + stamp


def opt_symbol(expiry, K, ot):
    return f"NIFTY{expiry.strftime('%d%b%y').upper()}{int(K)}{ot}"


def main():
    start_d = datetime.strptime(DAY, "%Y-%m-%d").date() if DAY else datetime.strptime(START, "%Y-%m-%d").date()
    end_d = start_d if DAY else datetime.strptime(END, "%Y-%m-%d").date()
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
    b = (spot.resample(f"{TF_MIN}min", closed="left", label="left")
             .agg(open=("open", "first"), high=("high", "max"),
                  low=("low", "min"), close=("close", "last")).dropna()
             .between_time("09:15", "15:25"))
    b["ema1"] = b["close"].ewm(span=EMA1, adjust=False).mean()
    b["ema2"] = b["close"].ewm(span=EMA2, adjust=False).mean()

    opt_cache: dict[str, pd.DataFrame] = {}

    def px_after(expiry, K, ot, ts):
        sym = opt_symbol(expiry, K, ot)
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
        df = opt_cache[sym]
        if df.empty:
            return None
        i = df.index.searchsorted(ts, side="left")
        return float(df["close"].iloc[min(i, len(df) - 1)])

    dd = conn.execute("""
        SELECT DISTINCT to_timestamp(timestamp)::DATE d FROM market_data
        WHERE exchange='NFO' AND symbol LIKE 'NIFTY%' AND interval='1m' AND timestamp BETWEEN ? AND ?
        ORDER BY d
    """, [int(datetime.combine(start_d, dtime()).timestamp()), s1]).df()
    dd["d"] = pd.to_datetime(dd["d"]).dt.date
    days = [d for d in dd["d"].tolist() if start_d <= d <= end_d]

    bar_delta = pd.Timedelta(minutes=TF_MIN)
    trades = []
    daily = []
    for d in days:
        expiry = next_expiry(d)
        if expiry is None:
            continue
        qty = lotmap.get(expiry, schedule_lot(expiry)) * LOTS
        bd = b[b.index.date == d]
        if len(bd) < 5:
            continue
        o = bd["open"].values; h = bd["high"].values; lo = bd["low"].values
        c = bd["close"].values; e1 = bd["ema1"].values; e2 = bd["ema2"].values
        idx = bd.index; nb = len(bd)
        eod_ts = pd.Timestamp(datetime.combine(d, SQUAREOFF))

        last_buy = last_sell = None     # zigzag bar indices
        tr = None                        # open trade dict
        day_net = 0.0

        def close_trade(tr, ts, reason):
            nonlocal day_net
            ex = px_after(expiry, tr["K"], tr["ot"], ts)
            if ex is None:
                ex = tr["entry_opt"]
            en = tr["entry_opt"]
            gross = ((ex - SLIPPAGE_PTS) - (en + SLIPPAGE_PTS)) * qty   # long option
            ch = long_charges(en, ex, qty)
            net = gross - ch
            day_net += net
            trades.append({"day": str(d), "side": tr["side"], "entry": tr["ets"].strftime("%H:%M"),
                           "exit": ts.strftime("%H:%M"), "reason": reason, "K": tr["K"], "ot": tr["ot"],
                           "entry_opt": round(en, 2), "exit_opt": round(ex, 2),
                           "spot_in": round(tr["spot"], 1), "tp_hits": tr["tp_idx"],
                           "qty": qty, "net": round(net, 0)})

        for i in range(nb):
            green = c[i] > o[i]; red = c[i] < o[i]
            raw_buy = green and lo[i] > e1[i] and lo[i] > e2[i]
            raw_sell = red and h[i] < e1[i] and h[i] < e2[i]
            sell_state = last_buy is None or (last_sell is not None and last_sell > last_buy)
            buy_state = last_sell is None or (last_buy is not None and last_buy > last_sell)
            buy_sig = raw_buy and sell_state
            sell_sig = raw_sell and buy_state
            close_ts = idx[i] + bar_delta            # candle close (no look-ahead)

            # 1) Manage open trade with THIS bar's range (trade was opened on a PRIOR bar)
            if tr is not None and i > tr["bar"]:
                if tr["side"] == "Buy":
                    if lo[i] <= tr["sl"]:
                        close_trade(tr, close_ts, "SL"); tr = None
                    elif h[i] >= tr["tp"]:
                        steps = 1 + math.floor((h[i] - tr["tp"]) / TRAIL_PTS)
                        tr["sl"] += steps * TRAIL_PTS; tr["tp"] += steps * TRAIL_PTS; tr["tp_idx"] += steps
                else:
                    if h[i] >= tr["sl"]:
                        close_trade(tr, close_ts, "SL"); tr = None
                    elif lo[i] <= tr["tp"]:
                        steps = 1 + math.floor((tr["tp"] - lo[i]) / TRAIL_PTS)
                        tr["sl"] -= steps * TRAIL_PTS; tr["tp"] -= steps * TRAIL_PTS; tr["tp_idx"] += steps

            # 2) Signals (open / reverse)
            if buy_sig:
                if tr is not None and tr["side"] == "Sell":
                    close_trade(tr, close_ts, "reverse"); tr = None
                if tr is None:
                    atm = round(c[i] / STRIKE_STEP) * STRIKE_STEP
                    K = atm - ITM_STRIKES * STRIKE_STEP        # ITM call
                    en = px_after(expiry, K, "CE", close_ts)
                    if en and en > 0.05:
                        sl = min(c[i] - SL_PTS, lo[i])
                        tr = {"side": "Buy", "ot": "CE", "K": K, "entry_opt": en, "ets": close_ts,
                              "bar": i, "sl": sl, "tp": c[i] + TP_PTS, "spot": c[i], "tp_idx": 0}
                last_buy, last_sell = i, None
            elif sell_sig:
                if tr is not None and tr["side"] == "Buy":
                    close_trade(tr, close_ts, "reverse"); tr = None
                if tr is None:
                    atm = round(c[i] / STRIKE_STEP) * STRIKE_STEP
                    K = atm + ITM_STRIKES * STRIKE_STEP        # ITM put
                    en = px_after(expiry, K, "PE", close_ts)
                    if en and en > 0.05:
                        sl = max(c[i] + SL_PTS, h[i])
                        tr = {"side": "Sell", "ot": "PE", "K": K, "entry_opt": en, "ets": close_ts,
                              "bar": i, "sl": sl, "tp": c[i] - TP_PTS, "spot": c[i], "tp_idx": 0}
                last_sell, last_buy = i, None

        if tr is not None:
            close_trade(tr, eod_ts, "eod")
        daily.append({"day": d, "net": day_net, "n": sum(1 for t in trades if t["day"] == str(d))})

    conn.close()
    _report(trades, daily)


def _report(trades, daily):
    dft = pd.DataFrame(trades); dfd = pd.DataFrame(daily)
    if DAY:
        print("=" * 96)
        print(f"EMA-ZIGZAG ITM OPTION BUYING — {DAY}  (ITM={ITM_STRIKES} strike, EMA{EMA1}/{EMA2}, {TF_MIN}m)")
        print("=" * 96)
        if dft.empty:
            print("No trades."); return
        cols = ["side", "entry", "exit", "reason", "ot", "K", "entry_opt", "exit_opt", "tp_hits", "spot_in", "net"]
        print(dft[cols].to_string(index=False))
        print(f"\nDay net: Rs {dft.net.sum():,.0f} | trades {len(dft)} | wins {(dft.net>0).sum()}")
        return

    total = float(dfd["net"].sum()); nt = len(dft)
    wins = int((dft["net"] > 0).sum()) if nt else 0
    dfd["cum"] = dfd["net"].cumsum(); maxdd = float((dfd["cum"] - dfd["cum"].cummax()).min())
    print("=" * 92)
    print(f"EMA-ZIGZAG ITM OPTION BUYING  {START}..{END} | ITM={ITM_STRIKES} EMA{EMA1}/{EMA2} {TF_MIN}m "
          f"SL{SL_PTS:.0f}/TP{TP_PTS:.0f}/trail{TRAIL_PTS:.0f} slip={SLIPPAGE_PTS}")
    print("=" * 92)
    print(f"Trades {nt} ({nt/max(1,len(dfd)):.1f}/day) | win {100*wins/max(1,nt):.1f}% | "
          f"NET Rs {total:,.0f} | MaxDD Rs {maxdd:,.0f} | ROI {100*total/CAPITAL:.0f}% on Rs {CAPITAL:,.0f}")
    if nt:
        w = dft[dft.net > 0].net; l = dft[dft.net <= 0].net
        print(f"avg win +{w.mean():,.0f} | avg loss {l.mean():,.0f} | "
              f"payoff {abs(w.mean()/l.mean()) if len(l) and l.mean() else float('nan'):.2f}x")
        dft["date"] = pd.to_datetime(dft["day"])
        yr = dft.groupby(dft.date.dt.year).agg(t=("net", "size"),
              wins=("net", lambda x: (x > 0).sum()), net=("net", "sum")).reset_index()
        print("\nYEARLY")
        for _, r in yr.iterrows():
            print(f"  {int(r.date)}: {int(r.t):>5} trades  win {100*r.wins/r.t:>3.0f}%  net Rs {r.net:>12,.0f}")
        mo = dft.groupby(dft.date.dt.strftime("%Y-%m")).net.sum()
        print("\nMONTHLY:", " ".join(f"{k}:{v:,.0f}" for k, v in mo.items()))
        exits = dft.reason.value_counts().to_dict()
        print("\nExit reasons:", exits)
        out = _dir / "results"; out.mkdir(exist_ok=True)
        dft.drop(columns=["date"]).to_csv(out / f"ema_zigzag_optbuy_{START}_{END}.csv", index=False)
        print(f"Saved -> results/ema_zigzag_optbuy_{START}_{END}.csv")


if __name__ == "__main__":
    main()
