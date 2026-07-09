#!/usr/bin/env python3
"""
Aakash 3m Signal -> Delta Credit Spread — MULTI-DAY BACKTEST (no look-ahead)
===========================================================================
Runs the validated signal engine (filter OFF, full state machine) across a
date range, turning each signal into a delta-targeted credit spread on the
nearest weekly expiry, and aggregates into daily / weekly / monthly / yearly
tables + an equity curve / drawdown / capital-margin verdict — NET of charges
(brokerage + STT + txn + SEBI + GST + stamp).

    BUY  -> Bull Put  (sell ~SHORT_DELTA PE, buy ~LONG_DELTA PE)
    SELL -> Bear Call (sell ~SHORT_DELTA CE, buy ~LONG_DELTA CE)

NO LOOK-AHEAD
  The 3m signal is confirmed at the candle's CLOSE (label + 3min). A trade
  therefore FILLS at the first option tick at-or-AFTER that close — never at the
  candle's start. Strike selection (delta) and entry/exit prices all use the
  post-close fill time. (Set OLD_FILL=1 to reproduce the old, look-ahead
  behaviour for comparison.)

LOT SIZE  Per-expiry from the MODE of the DB's lot_size (weeklies dominate, so
  the mode avoids stale long-dated contracts that MIN would catch). Falls back
  to the web-verified NIFTY schedule: 25 (pre-2024-11-20) -> 75 -> 65 (2026-01+).

Run:  uv run python backtesting/aakash_signal_spread/aakash_spread_backtest.py
Env:  START=YYYY-MM-DD END=YYYY-MM-DD LOTS=1 SHORT_DELTA=0.45 LONG_DELTA=0.20
      RATE=0.065 CAPITAL=300000 MARGIN_PER_LOT=40000 OLD_FILL=0
"""
from __future__ import annotations

import math
import os
from datetime import date, datetime, time as dtime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from aakash_signal_3m_replay import EMA_LENS, ATR_LEN, replay

# ---- Config ----------------------------------------------------------------
START        = os.getenv("START", "2021-04-29")
END          = os.getenv("END", "2026-06-09")
LOTS         = int(os.getenv("LOTS", "1"))
SHORT_DELTA  = float(os.getenv("SHORT_DELTA", "0.45"))
LONG_DELTA   = float(os.getenv("LONG_DELTA", "0.20"))
RATE         = float(os.getenv("RATE", "0.065"))
STRIKE_STEP  = 50
BAR_MIN      = 3                                  # 3-min signal candle
SQUAREOFF    = dtime(15, 25)                      # EOD square-off for the last open spread
WARMUP_DAYS  = 150
OLD_FILL     = os.getenv("OLD_FILL", "0") == "1"  # 1 = reproduce look-ahead (label-time fill)
HOLD_MODE    = os.getenv("HOLD_MODE", "0") == "1" # 1 = no flips; hold each spread to SL or EOD
SL_MULT      = float(os.getenv("SL_MULT", "2.0")) # hard SL = exit when loss >= SL_MULT x credit
MAX_DTE      = int(os.getenv("MAX_DTE", "999"))   # only trade when expiry is <= MAX_DTE days away
TREND_FILTER = os.getenv("TREND_FILTER", "0") == "1"  # 1 = only enter in trend (Pine VWAP+EMA filter)

# Capital / margin model (hedged spread, BUY-leg-placed-first sequencing).
CAPITAL        = float(os.getenv("CAPITAL", "300000"))
MARGIN_PER_LOT = float(os.getenv("MARGIN_PER_LOT", "40000"))
NAKED_PER_LOT  = float(os.getenv("NAKED_PER_LOT", "130000"))

BROKERAGE_PER_ORDER = 20
STT_SELL_PCT = 0.001
TXN_CHARGE_PCT = 0.0003553
SEBI_PER_CRORE = 10
GST_PCT = 0.18
STAMP_BUY_PCT = 0.00003

_dir = Path(__file__).resolve().parent
DUCKDB_PATH = str(_dir / ".." / ".." / "db" / "historify.duckdb")


# ---- Web-verified NIFTY lot-size schedule (fallback for missing metadata) ---
def schedule_lot(expiry: date) -> int:
    if expiry < date(2024, 11, 20):
        return 25
    if expiry < date(2026, 1, 1):
        return 75
    return 65


# ---- Black-Scholes ---------------------------------------------------------
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
        if bs_price(opt, S, K, T, r, mid) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def bs_delta(opt, S, K, T, r, sig):
    if T <= 0 or sig <= 0:
        return (1.0 if S > K else 0.0) if opt == "CE" else (-1.0 if S < K else 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    return _ncdf(d1) if opt == "CE" else _ncdf(d1) - 1.0


def leg_charges(side, entry_px, exit_px, qty):
    brokerage = BROKERAGE_PER_ORDER * 2
    turnover = (entry_px + exit_px) * qty
    txn = TXN_CHARGE_PCT * turnover
    sebi = SEBI_PER_CRORE * turnover / 1e7
    if side == "short":
        stt, stamp = STT_SELL_PCT * entry_px * qty, STAMP_BUY_PCT * exit_px * qty
    else:
        stt, stamp = STT_SELL_PCT * exit_px * qty, STAMP_BUY_PCT * entry_px * qty
    return brokerage + stt + txn + sebi + GST_PCT * (brokerage + txn + sebi) + stamp


def opt_symbol(expiry, K, ot):
    return f"NIFTY{expiry.strftime('%d%b%y').upper()}{int(K)}{ot}"


# ---- Main ------------------------------------------------------------------
def main():
    start_d = datetime.strptime(START, "%Y-%m-%d").date()
    end_d = datetime.strptime(END, "%Y-%m-%d").date()
    conn = duckdb.connect(DUCKDB_PATH, read_only=True)

    # Expiry list + per-expiry lot size via MODE (robust to stale long-dated lots).
    em = conn.execute("""
        SELECT expiry_date, lot_size, COUNT(*) n
        FROM expired_fno_contracts
        WHERE openalgo_symbol LIKE 'NIFTY%' AND contract_type IN ('CE','PE')
          AND lot_size IS NOT NULL
        GROUP BY expiry_date, lot_size
    """).df()
    em["expiry_date"] = pd.to_datetime(em["expiry_date"]).dt.date
    lotmap = {}
    for exp, grp in em.groupby("expiry_date"):
        lotmap[exp] = int(grp.sort_values("n", ascending=False)["lot_size"].iloc[0])  # mode
    expiries = sorted(lotmap.keys())

    def lot_for(expiry):
        return lotmap.get(expiry, schedule_lot(expiry))

    def next_expiry(d):
        for e in expiries:
            if e >= d:
                return e
        return None

    # Full 3m NIFTY spot with continuous EMAs/ATR (one load for whole range).
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
    bars = (spot.resample("3min", closed="left", label="left")
                .agg(open=("open", "first"), high=("high", "max"),
                     low=("low", "min"), close=("close", "last")).dropna()
                .between_time("09:15", "15:24"))
    for ln in EMA_LENS:
        bars[f"ema{ln}"] = bars["close"].ewm(span=ln, adjust=False).mean()
    pc = bars["close"].shift(1)
    tr = pd.concat([bars["high"] - bars["low"], (bars["high"] - pc).abs(),
                    (bars["low"] - pc).abs()], axis=1).max(axis=1)
    bars["atr"] = tr.ewm(alpha=1.0 / ATR_LEN, adjust=False).mean()
    bars["vwap"] = 0.0  # filter OFF -> unused

    # Option cache + lookups.
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

    def price_before(expiry, K, ot, ts):
        """Last option close at-or-BEFORE ts (used for OLD_FILL + EOD fallback)."""
        df = load_opt(opt_symbol(expiry, K, ot))
        if df.empty:
            return None
        i = df.index.searchsorted(ts, side="right") - 1
        return float(df["close"].iloc[i]) if i >= 0 else None

    def price_after(expiry, K, ot, ts):
        """First option close at-or-AFTER ts (no-look-ahead fill at candle close)."""
        df = load_opt(opt_symbol(expiry, K, ot))
        if df.empty:
            return None
        i = df.index.searchsorted(ts, side="left")
        if i >= len(df):                       # nothing after -> last available
            return float(df["close"].iloc[-1])
        return float(df["close"].iloc[i])

    def fill_price(expiry, K, ot, ts):
        if OLD_FILL:
            return price_before(expiry, K, ot, ts)
        p = price_after(expiry, K, ot, ts)
        return p if p is not None else price_before(expiry, K, ot, ts)

    def pick(expiry, spot_px, ts, ot, target):
        """Scan strikes; return (K, fill_price, |delta|) nearest target delta at ts."""
        T = max((datetime.combine(expiry, dtime(15, 30)) - ts.to_pydatetime()).total_seconds(), 60) / (365 * 86400)
        atm = round(spot_px / STRIKE_STEP) * STRIKE_STEP
        best = None
        for off in range(-14, 15):
            K = atm + off * STRIKE_STEP
            px = fill_price(expiry, K, ot, ts)
            if px is None or px <= 0.05:
                continue
            iv = implied_vol(ot, px, spot_px, K, T, RATE)
            if iv is None:
                continue
            dlt = abs(bs_delta(ot, spot_px, K, T, RATE, iv))
            if best is None or abs(dlt - target) < abs(best[2] - target):
                best = (K, px, dlt)
        return best

    # Trading days with option data in range.
    dd = conn.execute("""
        SELECT DISTINCT to_timestamp(timestamp)::DATE d FROM market_data
        WHERE exchange='NFO' AND symbol LIKE 'NIFTY%' AND interval='1m'
          AND timestamp BETWEEN ? AND ? ORDER BY d
    """, [int(datetime.combine(start_d, dtime()).timestamp()), s1]).df()
    dd["d"] = pd.to_datetime(dd["d"]).dt.date
    days = [d for d in dd["d"].tolist() if start_d <= d <= end_d]

    print("=" * 96)
    print(f"AAKASH SPREAD BACKTEST  {START}..{END}  | short~{SHORT_DELTA}Δ long~{LONG_DELTA}Δ "
          f"| lots={LOTS} | fill={'OLD/look-ahead' if OLD_FILL else 'candle-close'} "
          f"| exit={'HOLD (no-flip, SL=' + str(SL_MULT) + 'xcr or EOD)' if HOLD_MODE else 'flip-on-opposite-signal'}"
          f"{'' if MAX_DTE >= 999 else ' | DTE<=' + str(MAX_DTE)}"
          f"{' | TREND-FILTER ON' if TREND_FILTER else ''}")
    print("=" * 96)

    bar_delta = pd.Timedelta(minutes=0 if OLD_FILL else BAR_MIN)
    all_trades = []
    daily = []
    tid = 0
    for d in days:
        expiry = next_expiry(d)
        if expiry is None:
            continue
        if (expiry - d).days > MAX_DTE:           # only trade near expiry (theta window)
            daily.append({"day": d, "spreads": 0, "gross": 0.0, "charges": 0.0, "net": 0.0})
            continue
        lot = lot_for(expiry)
        qty = lot * LOTS
        day_df = bars[bars.index.date == d]
        if len(day_df) < 5:
            continue
        sigs = replay(day_df, use_trend_filter=TREND_FILTER, use_atr_sl=False)
        if not sigs:
            daily.append({"day": d, "spreads": 0, "gross": 0.0, "charges": 0.0, "net": 0.0})
            continue
        eod_ts = pd.Timestamp(datetime.combine(d, SQUAREOFF))

        def spot_at(ts):
            sub = day_df[day_df.index <= ts]
            return float(sub["close"].iloc[-1]) if len(sub) else None

        day_gross = day_charges = 0.0
        nsp = 0
        hold_until = None
        for n, sg in enumerate(sigs):
            label_ts = pd.Timestamp(datetime.combine(d, datetime.strptime(sg["time"], "%H:%M").time()))
            entry_ts = label_ts + bar_delta            # candle close (no look-ahead)
            if HOLD_MODE and hold_until is not None and entry_ts < hold_until:
                continue                                # already in a position -> no flip
            sp = sg["spot"]
            ot = "PE" if sg["side"] == "BUY" else "CE"
            sh = pick(expiry, sp, entry_ts, ot, SHORT_DELTA)
            lg = pick(expiry, sp, entry_ts, ot, LONG_DELTA)
            if sh is None or lg is None or sh[0] == lg[0]:
                continue
            sK, sEn, sD = sh
            lK, lEn, lD = lg

            if HOLD_MODE:
                # Hold to EOD unless a hard SL (loss >= SL_MULT x credit) trips first.
                credit_pts = sEn - lEn
                sl_pts = SL_MULT * credit_pts
                exit_ts, exit_reason = eod_ts, "eod"
                sEx = fill_price(expiry, sK, ot, eod_ts) or sEn
                lEx = fill_price(expiry, lK, ot, eod_ts) or lEn
                fwd = day_df[(day_df.index > entry_ts) & (day_df.index <= eod_ts)]
                for bts in fwd.index:
                    cts = bts + bar_delta               # bar close (no look-ahead)
                    s_now = fill_price(expiry, sK, ot, cts)
                    if s_now is None:
                        continue
                    l_now = fill_price(expiry, lK, ot, cts)
                    if l_now is None:
                        l_now = lEn
                    if (s_now - l_now) - credit_pts >= sl_pts:   # loss exceeds hard SL
                        exit_ts, exit_reason = cts, "SL"
                        sEx, lEx = s_now, l_now
                        break
                hold_until = exit_ts
            else:
                if n + 1 < len(sigs):
                    nxt_label = pd.Timestamp(datetime.combine(d, datetime.strptime(sigs[n + 1]["time"], "%H:%M").time()))
                    exit_ts = nxt_label + bar_delta
                    exit_reason = "flip"
                else:
                    exit_ts = eod_ts
                    exit_reason = "eod"
                if exit_ts <= entry_ts:
                    exit_ts, exit_reason = eod_ts, "eod"
                sEx = fill_price(expiry, sK, ot, exit_ts) or sEn
                lEx = fill_price(expiry, lK, ot, exit_ts) or lEn

            gross = (sEn - sEx) * qty + (lEx - lEn) * qty
            ch = leg_charges("short", sEn, sEx, qty) + leg_charges("long", lEn, lEx, qty)
            net = gross - ch
            day_gross += gross
            day_charges += ch
            nsp += 1
            tid += 1
            all_trades.append({
                "trade_id": tid, "day": str(d), "weekday": d.strftime("%a"),
                "signal_time": sg["time"], "signal_confirm_time": entry_ts.strftime("%H:%M"),
                "entry_fill_time": entry_ts.strftime("%H:%M"), "side": sg["side"], "rule": sg["rule"],
                "structure": "BullPut" if ot == "PE" else "BearCall",
                "expiry": str(expiry), "dte": (expiry - d).days,
                "sell_symbol": opt_symbol(expiry, sK, ot), "sell_strike": sK,
                "sell_delta": round(sD, 3), "sell_entry": round(sEn, 2), "sell_exit": round(sEx, 2),
                "buy_symbol": opt_symbol(expiry, lK, ot), "buy_strike": lK,
                "buy_delta": round(lD, 3), "buy_entry": round(lEn, 2), "buy_exit": round(lEx, 2),
                "width": int(abs(sK - lK)), "credit": round(sEn - lEn, 2),
                "debit_at_exit": round(sEx - lEx, 2), "lot_size": lot, "lots": LOTS, "qty": qty,
                "margin_used": MARGIN_PER_LOT * LOTS, "gross": round(gross, 0),
                "charges": round(ch, 0), "net": round(net, 0),
                "exit_time": exit_ts.strftime("%H:%M"), "exit_reason": exit_reason,
                "hold_min": int((exit_ts - entry_ts).total_seconds() // 60),
                "spot_entry": round(sp, 2), "spot_exit": round(spot_at(exit_ts) or sp, 2),
            })
        daily.append({"day": d, "spreads": nsp, "gross": round(day_gross, 0),
                      "charges": round(day_charges, 0), "net": round(day_gross - day_charges, 0)})

    conn.close()
    _report(daily, all_trades, lotmap)


# ---- Reporting -------------------------------------------------------------
def _report(daily, all_trades, lotmap):
    dfd = pd.DataFrame(daily)
    dft = pd.DataFrame(all_trades)
    dfd["cum"] = dfd["net"].cumsum()
    maxdd = float((dfd["cum"] - dfd["cum"].cummax()).min())
    total = float(dfd["net"].sum())
    nsp = len(dft)
    wins = int((dft["net"] > 0).sum()) if nsp else 0

    print(f"\nTrading days: {len(dfd)} ({int((dfd['net']>0).sum())} up / {int((dfd['net']<0).sum())} down "
          f"/ {int((dfd['spreads']==0).sum())} no-trade) | spreads: {nsp} | win {100*wins/max(1,nsp):.1f}%")
    print(f"NET P&L (per {LOTS} lot, after charges): Rs {total:,.0f}")
    if nsp:
        w = dft[dft.net > 0].net; l = dft[dft.net <= 0].net
        payoff = abs(w.mean() / l.mean()) if len(l) and l.mean() != 0 else float("nan")
        print(f"Avg win +{w.mean():,.0f} | avg loss {l.mean():,.0f} | payoff {payoff:.2f}x "
              f"| Best day {dfd['net'].max():,.0f} | Worst day {dfd['net'].min():,.0f} | Max DD {maxdd:,.0f}")

    if nsp:
        dft["date"] = pd.to_datetime(dft["day"])
        # Yearly
        dft["Y"] = dft.date.dt.year
        yr = dft.groupby("Y").agg(spreads=("net", "size"), wins=("net", lambda s: (s > 0).sum()),
                                  net=("net", "sum")).reset_index()
        print("\nYEARLY")
        print(f"{'Year':<8}{'Spreads':>8}{'Win%':>7}{'Net':>14}")
        for _, r in yr.iterrows():
            print(f"{int(r.Y):<8}{int(r.spreads):>8}{100*r.wins/r.spreads:>6.0f}%{r.net:>14,.0f}")
        # Monthly
        dft["YM"] = dft.date.dt.strftime("%Y-%m")
        mo = dft.groupby("YM").agg(spreads=("net", "size"), wins=("net", lambda s: (s > 0).sum()),
                                   net=("net", "sum")).reset_index()
        print("\nMONTHLY")
        print(f"{'Month':<9}{'Spreads':>8}{'Win%':>7}{'Net':>14}")
        for _, r in mo.iterrows():
            print(f"{r.YM:<9}{int(r.spreads):>8}{100*r.wins/r.spreads:>6.0f}%{r.net:>14,.0f}")
        # Weekly (ISO) -> CSV only (can be long); print count
        dft["YW"] = dft.date.dt.strftime("%G-W%V")
        wk = dft.groupby("YW").agg(spreads=("net", "size"), wins=("net", lambda s: (s > 0).sum()),
                                   net=("net", "sum")).reset_index()
        print(f"\nWeekly buckets: {len(wk)} (saved to CSV)")

    # Lot-size timeline (verify 25 -> 75 -> 65)
    if lotmap:
        tl = pd.Series(lotmap).sort_index()
        changes = tl[tl.ne(tl.shift())]
        print("\nLot-size timeline (expiry : lot):",
              "  ".join(f"{k}:{v}" for k, v in changes.items()))

    # Capital / margin verdict
    margin_used = MARGIN_PER_LOT * LOTS
    naked_risk = NAKED_PER_LOT * LOTS
    n_months = max(1, pd.to_datetime(dfd["day"]).dt.strftime("%Y-%m").nunique())
    roi = 100 * total / CAPITAL
    print("\n" + "=" * 60)
    print("CAPITAL & MARGIN  (hedged, BUY-leg-placed-FIRST)")
    print("=" * 60)
    print(f"  Capital              : Rs {CAPITAL:,.0f}")
    print(f"  Margin/spread @{LOTS}lot : Rs {margin_used:,.0f} (Rs {MARGIN_PER_LOT:,.0f}/lot, 1 spread at a time)")
    print(f"  Utilisation          : {100*margin_used/CAPITAL:.0f}%  | max fundable lots {int(CAPITAL//MARGIN_PER_LOT)}")
    print(f"  Return on capital    : {roi:.0f}% over {n_months} mo (~{roi*12/n_months:.0f}%/yr)")
    print(f"  Max drawdown         : Rs {maxdd:,.0f} ({100*abs(maxdd)/CAPITAL:.0f}% of capital)")
    if margin_used > CAPITAL:
        print(f"  >> INSUFFICIENT: {LOTS} lots needs Rs {margin_used:,.0f} > capital.")
    elif maxdd + CAPITAL < margin_used:
        print(f"  >> TIGHT: after max DD free capital Rs {maxdd+CAPITAL:,.0f} < margin Rs {margin_used:,.0f}.")
    else:
        print(f"  >> OK: {LOTS} lots fits with buffer (even after max drawdown).")
    print(f"  !! NAKED RISK if SELL fills before hedge: Rs {naked_risk:,.0f}"
          + ("  > capital -> REJECTION!" if naked_risk > CAPITAL else "  (within capital)"))
    print("     -> Live MUST place the BUY (hedge) leg first, every entry & flip.")

    # Reconciliation
    recon = abs(round(dfd["net"].sum()) - (round(dft["net"].sum()) if nsp else 0))
    print(f"\nReconciliation (daily-sum vs trade-sum): diff Rs {recon:.0f}  "
          + ("OK (rounding)" if recon <= nsp + 5 else "<< MISMATCH!"))

    # Save
    out = _dir / "results"
    out.mkdir(exist_ok=True)
    tag = f"{START}_{END}" + ("_OLDFILL" if OLD_FILL else "")
    if nsp:
        dft.drop(columns=["date", "Y", "YM", "YW"]).to_csv(out / f"backtest_trades_full_{tag}.csv", index=False)
        mo.to_csv(out / f"backtest_monthly_{tag}.csv", index=False)
        yr.to_csv(out / f"backtest_yearly_{tag}.csv", index=False)
        wk.to_csv(out / f"backtest_weekly_{tag}.csv", index=False)
    dfd.to_csv(out / f"backtest_daily_{tag}.csv", index=False)
    print(f"Saved -> results/backtest_(trades_full|daily|weekly|monthly|yearly)_{tag}.csv")


if __name__ == "__main__":
    main()
