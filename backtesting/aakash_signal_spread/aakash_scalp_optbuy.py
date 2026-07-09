#!/usr/bin/env python3
"""
Aakash 3m signal -> ITM OPTION BUYING scalp (fixed rupee TP / SL)
================================================================
Takes the Aakash 3m breakout signals (filter OFF state machine) and BUYS an ITM
option in the signal's direction, scalping with a FIXED RUPEE target and stop:

  BUY  signal -> buy ITM CALL    SELL signal -> buy ITM PUT
  Exit when: gross P&L >= +TP_RS  OR  <= -SL_RS  OR  next signal  OR  EOD.

TP/SL are checked on the option's own 1-minute prices (intrabar accuracy for a
scalp). NO look-ahead: signal known at candle close -> fill at first tick after.
Note: a credit spread can't make TP_RS=5000 on 1 lot (max ~credit); option
buying has open upside, so the rupee target is meaningful here.

Run: uv run python backtesting/aakash_signal_spread/aakash_scalp_optbuy.py
     DAY=2026-06-09 uv run python ... aakash_scalp_optbuy.py   (single-day detail)
Env: START END LOTS TP_RS SL_RS ITM_STRIKES SLIPPAGE_PTS
"""
from __future__ import annotations

import os
from datetime import date, datetime, time as dtime
from pathlib import Path

import duckdb
import pandas as pd

from aakash_signal_3m_replay import load_3m, replay

DAY          = os.getenv("DAY")
START        = os.getenv("START", "2024-08-01")
END          = os.getenv("END", "2026-06-12")
LOTS         = int(os.getenv("LOTS", "1"))
TP_RS        = float(os.getenv("TP_RS", "5000"))      # book profit per trade
SL_RS        = float(os.getenv("SL_RS", "2000"))      # risk per trade
ITM_STRIKES  = int(os.getenv("ITM_STRIKES", "1"))
SLIPPAGE_PTS = float(os.getenv("SLIPPAGE_PTS", "0"))
STRIKE_STEP  = 50
SQUAREOFF    = dtime(15, 25)
CAPITAL      = float(os.getenv("CAPITAL", "300000"))

BROKERAGE_PER_ORDER = 20
STT_SELL_PCT = 0.001
TXN_CHARGE_PCT = 0.0003553
SEBI_PER_CRORE = 10
GST_PCT = 0.18
STAMP_BUY_PCT = 0.00003

_dir = Path(__file__).resolve().parent
DUCKDB_PATH = str(_dir / ".." / ".." / "db" / "historify.duckdb")


def schedule_lot(expiry):
    return 25 if expiry < date(2024, 11, 20) else (75 if expiry < date(2026, 1, 1) else 65)


def long_charges(en, ex, qty):
    brk = BROKERAGE_PER_ORDER * 2
    turn = (en + ex) * qty
    txn = TXN_CHARGE_PCT * turn
    sebi = SEBI_PER_CRORE * turn / 1e7
    return brk + STT_SELL_PCT * ex * qty + txn + sebi + GST_PCT * (brk + txn + sebi) + STAMP_BUY_PCT * en * qty


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

    def px_after(df, ts):
        if df.empty:
            return None
        i = df.index.searchsorted(ts, side="left")
        return float(df["close"].iloc[min(i, len(df) - 1)])

    dd = conn.execute("""
        SELECT DISTINCT to_timestamp(timestamp)::DATE d FROM market_data
        WHERE exchange='NFO' AND symbol LIKE 'NIFTY%' AND interval='1m' AND timestamp BETWEEN ? AND ?
        ORDER BY d
    """, [int(datetime.combine(start_d, dtime()).timestamp()), int(datetime.combine(end_d, dtime(23, 59)).timestamp())]).df()
    dd["d"] = pd.to_datetime(dd["d"]).dt.date
    days = [d for d in dd["d"].tolist() if start_d <= d <= end_d]

    trades = []
    daily = []
    for d in days:
        expiry = next_expiry(d)
        if expiry is None:
            continue
        qty = lotmap.get(expiry, schedule_lot(expiry)) * LOTS
        try:
            _, day_df = load_3m(conn, str(d))
        except Exception:
            continue
        day_df = day_df[day_df.index.date == d]
        if len(day_df) < 5:
            continue
        sigs = replay(day_df, use_trend_filter=False, use_atr_sl=False)
        if not sigs:
            daily.append({"day": d, "net": 0.0}); continue
        eod_ts = pd.Timestamp(datetime.combine(d, SQUAREOFF))

        day_net = 0.0
        for n, sg in enumerate(sigs):
            entry_ts = pd.Timestamp(datetime.combine(d, datetime.strptime(sg["time"], "%H:%M").time())) + pd.Timedelta(minutes=3)
            spot = sg["spot"]
            atm = round(spot / STRIKE_STEP) * STRIKE_STEP
            ot, K = ("CE", atm - ITM_STRIKES * STRIKE_STEP) if sg["side"] == "BUY" else ("PE", atm + ITM_STRIKES * STRIKE_STEP)
            df = load_opt(opt_symbol(expiry, K, ot))
            en = px_after(df, entry_ts)
            if en is None or en <= 0.05:
                continue
            win_end = (pd.Timestamp(datetime.combine(d, datetime.strptime(sigs[n + 1]["time"], "%H:%M").time())) + pd.Timedelta(minutes=3)) if n + 1 < len(sigs) else eod_ts
            seg = df[(df.index > entry_ts) & (df.index <= win_end)]
            exit_px, exit_ts, reason = (px_after(df, win_end), win_end, "signal" if n + 1 < len(sigs) else "eod")
            for ts, px in seg["close"].items():
                pnl = (px - en) * qty
                if pnl >= TP_RS:
                    exit_px, exit_ts, reason = px, ts, "TP"; break
                if pnl <= -SL_RS:
                    exit_px, exit_ts, reason = px, ts, "SL"; break
            if exit_px is None:
                exit_px = en
            gross = ((exit_px - SLIPPAGE_PTS) - (en + SLIPPAGE_PTS)) * qty
            net = gross - long_charges(en, exit_px, qty)
            day_net += net
            trades.append({"day": str(d), "side": sg["side"], "entry": entry_ts.strftime("%H:%M"),
                           "exit": exit_ts.strftime("%H:%M"), "reason": reason, "ot": ot, "K": K,
                           "entry_opt": round(en, 2), "exit_opt": round(exit_px, 2), "qty": qty,
                           "net": round(net, 0)})
        daily.append({"day": d, "net": day_net})

    conn.close()
    _report(trades, daily)


def _report(trades, daily):
    dft = pd.DataFrame(trades); dfd = pd.DataFrame(daily)
    if DAY:
        print("=" * 90)
        print(f"AAKASH 3m SCALP — ITM OPTION BUYING — {DAY}  (TP Rs{TP_RS:.0f} / SL Rs{SL_RS:.0f}, ITM={ITM_STRIKES})")
        print("=" * 90)
        if dft.empty:
            print("No trades."); return
        print(dft[["side", "entry", "exit", "reason", "ot", "K", "entry_opt", "exit_opt", "net"]].to_string(index=False))
        print(f"\nDay net: Rs {dft.net.sum():,.0f} | trades {len(dft)} | wins {(dft.net>0).sum()}")
        return
    total = float(dfd["net"].sum()); nt = len(dft)
    wins = int((dft["net"] > 0).sum()) if nt else 0
    dfd["cum"] = dfd["net"].cumsum(); maxdd = float((dfd["cum"] - dfd["cum"].cummax()).min())
    print("=" * 92)
    print(f"AAKASH 3m SCALP ITM OPTION BUYING  {START}..{END} | TP Rs{TP_RS:.0f}/SL Rs{SL_RS:.0f} "
          f"ITM={ITM_STRIKES} slip={SLIPPAGE_PTS}")
    print("=" * 92)
    print(f"Trades {nt} ({nt/max(1,len(dfd)):.1f}/day) | win {100*wins/max(1,nt):.1f}% | "
          f"NET Rs {total:,.0f} | MaxDD Rs {maxdd:,.0f} | ROI {100*total/CAPITAL:.0f}%")
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
        print("\nExit reasons:", dft.reason.value_counts().to_dict())
        out = _dir / "results"; out.mkdir(exist_ok=True)
        dft.drop(columns=["date"]).to_csv(out / f"aakash_scalp_optbuy_{START}_{END}.csv", index=False)
        print(f"Saved -> results/aakash_scalp_optbuy_{START}_{END}.csv")


if __name__ == "__main__":
    main()
