#!/usr/bin/env python3
"""
FULL VERIFICATION DUMP for breakout_intraday_strategy.

Covers EVERY scanned stock — not just the ones that traded — so you can verify
each section against your TradingView chart and quantify the "late entry" gap
(entry = next-bar open vs the Pine signal-candle close).

Outputs (in cwd):
  1. breakout_signals_<date>.csv  — one row per detected signal (ALL stocks),
       status = TAKEN | SKIPPED_OPEN (a later signal blocked by the open position)
       includes sig_close, entry_open, entry_gap_pts/pct  (the late-entry amount)
  2. breakout_allbars_<date>.csv  — one row per 5m bar per stock: OHLCV + vwap +
       adx + raw pattern flags (buy1/buy2/sell1/sell2) + filter pass flags
  3. console: 4 sections — scan tally, taken trades, skipped signals, no-signal list

Usage:
  TF_JWT_TOKEN=... OPENALGO_API_KEY=... \
      python strategies/breakout_report.py [--date YYYY-MM-DD] [--top N] [--symbols A,B,C]
"""

import argparse
import csv
import importlib.util
import math
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("strat", os.path.join(_HERE, "breakout_intraday_strategy.py"))
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)


def _today_ist():
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")


def _t(v):
    s = str(v)
    return s[11:16] if len(s) >= 16 else s


def prep(df, day):
    d = S._normalise(df)
    if d is None:
        return None
    # Compute ATR on ALL fetched data FIRST (warm the ewm) — then filter to today.
    # Computing only on today's ~30 bars gives a cold-start ATR that's 2x too large.
    if S.USE_ATR_SL:
        d = S.add_atr(d, S.ATR_LEN)
    d = d[(d["_date"] == day) & (d["_hm"] >= S.SESSION_START_MIN) & (d["_hm"] <= S.SESSION_END_MIN)]
    if len(d) < 4:
        return None
    d = S.add_vwap(d)
    d = S.add_adx(d, S.ADX_LEN)
    return d.reset_index(drop=True)


def eval_bar(d, i):
    """All raw pattern flags + filter values for bar i (i>=2). Independent of enable toggles."""
    b, p1, p2 = d.iloc[i], d.iloc[i - 1], d.iloc[i - 2]
    o, h, c, lo = float(b["open"]), float(b["high"]), float(b["close"]), float(b["low"])
    o1, h1, c1, l1 = float(p1["open"]), float(p1["high"]), float(p1["close"]), float(p1["low"])
    o2, h2, c2, l2 = float(p2["open"]), float(p2["high"]), float(p2["close"]), float(p2["low"])
    is_green, is_red = c > o, c < o
    pg, pr = c1 > o1, c1 < o1
    p2g, p2r = c2 > o2, c2 < o2
    vwap = float(b["vwap"]) if pd.notna(b["vwap"]) else None
    adx = float(b["adx"]) if pd.notna(b["adx"]) else 0.0
    atr = float(b["atr"]) if S.USE_ATR_SL and "atr" in b.index and pd.notna(b.get("atr")) else None
    return {
        "buy1": is_green and pr and c > h1,
        "sell1": is_red and pg and c < l1,
        "buy2": is_green and pg and p2r and c > h2,
        "sell2": is_red and pr and p2g and c < l2,
        "vwap": vwap, "adx": adx, "atr": atr,
        "vwap_buy_ok": (not S.USE_VWAP) or (vwap is not None and c > vwap),
        "vwap_sell_ok": (not S.USE_VWAP) or (vwap is not None and c < vwap),
        "adx_ok": (not S.USE_ADX) or (adx >= S.ADX_THRESHOLD),
        "o": o, "h": h, "l": lo, "c": c,
        "l1": l1, "l2": l2, "h1": h1, "h2": h2,
        "within": S.SESSION_START_MIN <= int(b["_hm"]) <= S.ENTRY_CUTOFF_MIN,
    }


def config_signal(f):
    """Does bar (given its flags) fire a signal under the CURRENT config? Returns dir/kind/sl or None."""
    if not f["within"]:
        return None
    buy1 = S.ENABLE_SINGLE and S.ENABLE_BUY and f["buy1"]
    sell1 = S.ENABLE_SINGLE and S.ENABLE_SELL and f["sell1"]
    buy2 = S.ENABLE_DOUBLE and S.ENABLE_BUY and f["buy2"]
    sell2 = S.ENABLE_DOUBLE and S.ENABLE_SELL and f["sell2"]
    if not f["adx_ok"]:
        return None
    atr = f.get("atr")
    def _sl_long(candle_sl):
        if S.USE_ATR_SL and atr is not None:
            return round(f["c"] - atr * S.ATR_MULT, 2)
        return candle_sl
    def _sl_short(candle_sl):
        if S.USE_ATR_SL and atr is not None:
            return round(f["c"] + atr * S.ATR_MULT, 2)
        return candle_sl

    if (buy1 or buy2) and f["vwap_buy_ok"]:
        candle_sl = min(f["l"], f["l1"]) if buy1 else min(f["l"], f["l1"], f["l2"])
        sl_src = ("ATR_SL" if S.USE_ATR_SL else ("min(L,L1)" if buy1 else "min(L,L1,L2)"))
        return ("LONG",  "single" if buy1 else "double", _sl_long(candle_sl),  sl_src)
    if (sell1 or sell2) and f["vwap_sell_ok"]:
        candle_sl = max(f["h"], f["h1"]) if sell1 else max(f["h"], f["h1"], f["h2"])
        sl_src = ("ATR_SL" if S.USE_ATR_SL else ("max(H,H1)" if sell1 else "max(H,H1,H2)"))
        return ("SHORT", "single" if sell1 else "double", _sl_short(candle_sl), sl_src)
    return None


def simulate(d, i, direction, sl):
    """Enter at signal-bar close (matches Pine/TV chart), walk to SL/target/trail/EOD."""
    if i + 1 >= len(d):
        return None
    entry = float(d.iloc[i]["close"])   # signal bar close = TV chart entry price
    dist = abs(entry - sl)
    if dist < 0.01 or (direction == "LONG" and entry <= sl) or (direction == "SHORT" and entry >= sl):
        return None
    risk_budget = S.BALANCE * (S.MAX_LOSS_PCT / 100.0)
    qty_risk = math.floor(risk_budget / dist)
    qty_cap = math.floor(S.CAPITAL_PER_TRADE / entry)
    qty = max(0, min(qty_risk, qty_cap))
    if qty <= 0:
        return None
    target = entry + S.TARGET_RR * dist if direction == "LONG" else entry - S.TARGET_RR * dist
    cur_sl, reached_1r = sl, False
    exit_p, exit_i, reason = float(d.iloc[-1]["close"]), len(d) - 1, "EOD"
    for j in range(i + 1, len(d)):
        bar = d.iloc[j]
        hi, lo, cl = float(bar["high"]), float(bar["low"]), float(bar["close"])
        if direction == "LONG":
            if lo <= cur_sl:
                exit_p, exit_i, reason = cur_sl, j, "SL"; break
            if hi >= target:
                exit_p, exit_i, reason = target, j, "TARGET"; break
            if hi >= entry + dist:
                reached_1r = True
            if (S.TRAIL_MODE == "full" and cl > entry) or (S.TRAIL_MODE == "after_1r" and reached_1r):
                cur_sl = max(cur_sl, lo)
        else:
            if hi >= cur_sl:
                exit_p, exit_i, reason = cur_sl, j, "SL"; break
            if lo <= target:
                exit_p, exit_i, reason = target, j, "TARGET"; break
            if lo <= entry - dist:
                reached_1r = True
            if (S.TRAIL_MODE == "full" and cl < entry) or (S.TRAIL_MODE == "after_1r" and reached_1r):
                cur_sl = min(cur_sl, hi)
    gross = (exit_p - entry) * qty * (1 if direction == "LONG" else -1)
    ch = S.compute_charges(entry, exit_p, qty)
    return {"entry": entry, "entry_time": str(d.iloc[i]["_dt"]), "sl_dist": dist,
            "qty": qty, "qty_risk": qty_risk, "qty_cap": qty_cap, "target": target,
            "exit": exit_p, "exit_time": str(d.iloc[exit_i]["_dt"]), "reason": reason,
            "R": (exit_p - entry) / dist * (1 if direction == "LONG" else -1),
            "gross": gross, "charges": ch, "net": gross - ch,
            "_exit_bar_idx": exit_i}   # internal — used for re-entry scan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=_today_ist())
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--symbols", default="")
    args = ap.parse_args()
    day = args.date

    if args.symbols.strip():
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        stocks = S.fetch_intraday_boost()
        if not stocks:
            print("No TradeFinder stocks (JWT expired?). Use --symbols A,B,C.")
            sys.exit(1)
        symbols = [s["symbol"] for s in stocks[:args.top]]

    print("=" * 100)
    print(f"  FULL VERIFICATION DUMP — {day}  interval={S.INTERVAL}")
    print(f"  Config: single={S.ENABLE_SINGLE} double={S.ENABLE_DOUBLE} VWAP={S.USE_VWAP} "
          f"ADX_on={S.USE_ADX}(>={S.ADX_THRESHOLD}) ATR_SL={S.USE_ATR_SL}(x{S.ATR_MULT}) "
          f"Chop={S.USE_VWAP_CHOP} target={S.TARGET_RR}R trail={S.TRAIL_MODE}")
    print(f"  Sizing: risk_budget=Rs.{S.BALANCE*S.MAX_LOSS_PCT/100:,.0f}  notional_cap=Rs.{S.CAPITAL_PER_TRADE:,.0f}")
    print("=" * 100)

    all_signals, all_bars = [], []
    nodata, nosignal = [], []

    for sym in symbols:
        df = S.fetch_history(sym, S.INTERVAL, 10)  # 10 days to warm ATR(14) ewm
        d = prep(df, day) if df is not None else None
        if d is None:
            nodata.append(sym)
            continue

        sym_has_signal = False
        next_entry_from = 0   # re-entry: scan from this bar after previous trade exits
        sl_mode = False        # Pine enhanced mode: True after first SL hit — blocks classic signals

        for i in range(len(d)):
            b = d.iloc[i]
            rec = {"symbol": sym, "time": str(b["_dt"]), "open": float(b["open"]),
                   "high": float(b["high"]), "low": float(b["low"]), "close": float(b["close"]),
                   "volume": float(b["volume"]),
                   "vwap": round(float(b["vwap"]), 2) if pd.notna(b["vwap"]) else "",
                   "adx": round(float(b["adx"]), 2) if pd.notna(b["adx"]) else ""}
            if i >= 2:
                f = eval_bar(d, i)
                rec.update({"buy1": int(f["buy1"]), "buy2": int(f["buy2"]),
                            "sell1": int(f["sell1"]), "sell2": int(f["sell2"]),
                            "vwap_buy_ok": int(f["vwap_buy_ok"]), "vwap_sell_ok": int(f["vwap_sell_ok"]),
                            "adx_ok": int(f["adx_ok"])})
                # chop filter
                if S.USE_VWAP_CHOP and "vwap" in d.columns:
                    start = max(0, i - S.VWAP_CHOP_LOOKBACK + 1)
                    w = d.iloc[start:i + 1]
                    touches = int(((w["high"] >= w["vwap"]) & (w["low"] <= w["vwap"])).sum())
                    chop_pass = touches <= S.VWAP_CHOP_MAX_TOUCHES
                else:
                    chop_pass = True
                # Pine enhanced mode: once first SL hit, classic signals blocked for rest of day
                sig = config_signal(f) if (chop_pass and not sl_mode) else None
                rec["fires"] = "" if sig is None else f"{sig[0]}_{sig[1]}"
                if sig is not None:
                    sym_has_signal = True
                    direction, kind, sl, sl_src = sig
                    sim = simulate(d, i, direction, sl)
                    if sim:
                        # TAKEN if this bar is after the previous trade's exit bar
                        status = "TAKEN" if i >= next_entry_from else "SKIPPED_OPEN"
                        sig_close = float(b["close"])
                        gap = sim["entry"] - sig_close
                        all_signals.append({
                            "symbol": sym, "status": status, "dir": direction, "kind": kind,
                            "sig_time": str(b["_dt"]), "sig_o": float(b["open"]),
                            "sig_h": float(b["high"]), "sig_l": float(b["low"]),
                            "sig_close": sig_close, "vwap": rec["vwap"], "adx": rec["adx"],
                            "entry_open": sim["entry"], "entry_time": sim["entry_time"],
                            "entry_gap_pts": round(gap, 2),
                            "entry_gap_pct": round(gap / sig_close * 100, 3) if sig_close else 0,
                            "sl": sl, "sl_src": sl_src, "sl_dist": round(sim["sl_dist"], 2),
                            "qty": sim["qty"], "qty_risk": sim["qty_risk"], "qty_cap": sim["qty_cap"],
                            "target": round(sim["target"], 2), "exit": round(sim["exit"], 2),
                            "exit_time": sim["exit_time"], "reason": sim["reason"],
                            "R": round(sim["R"], 2),
                            "gross": round(sim["gross"], 0), "charges": round(sim["charges"], 0),
                            "net": round(sim["net"], 0)})
                        if status == "TAKEN":
                            next_entry_from = sim["_exit_bar_idx"] + 1  # allow re-entry after exit
                            if sim["reason"] == "SL":
                                sl_mode = True  # Pine: after first SL hit, no more classic signals
            all_bars.append(rec)
        if not sym_has_signal:
            nosignal.append(sym)

    # ── write CSVs ──
    sig_csv = f"breakout_signals_{day}.csv"
    if all_signals:
        cols = list(all_signals[0].keys())
        with open(sig_csv, "w", newline="") as fp:
            w = csv.DictWriter(fp, fieldnames=cols)
            w.writeheader()
            w.writerows(all_signals)
    bars_csv = f"breakout_allbars_{day}.csv"
    if all_bars:
        cols = ["symbol", "time", "open", "high", "low", "close", "volume", "vwap", "adx",
                "buy1", "buy2", "sell1", "sell2", "vwap_buy_ok", "vwap_sell_ok", "adx_ok", "fires"]
        with open(bars_csv, "w", newline="") as fp:
            w = csv.DictWriter(fp, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(all_bars)

    # ── SECTION 1: scan tally ──
    n = len(symbols)
    with_sig = len({s["symbol"] for s in all_signals})
    print(f"\n── SECTION 1 · SCAN TALLY ──────────────────────────────────────────")
    print(f"  scanned={n}  with_signal={with_sig}  no_signal={len(nosignal)}  no_data={len(nodata)}")
    print(f"  bars dumped: {len(all_bars)} rows -> {bars_csv}")
    print(f"  signals dumped: {len(all_signals)} rows -> {sig_csv}")

    # ── SECTION 2: taken trades ──
    taken_rows = [s for s in all_signals if s["status"] == "TAKEN"]
    taken_rows.sort(key=lambda x: x["sig_time"])
    print(f"\n── SECTION 2 · TAKEN TRADES ({len(taken_rows)}) ─────────────────────────────")
    print(f"{'SYM':<11}{'DIR':<6}{'SIG':<7}{'sigClose':>9}{'entry':>9}{'gap':>6}{'SL':>9}"
          f"{'TGT':>9}{'EXIT':>9}{'QTY':>6}{'R':>6}{'NET':>8} reason")
    tn = tg = 0.0; wins = 0
    for s in taken_rows:
        tg += s["gross"]; tn += s["net"]; wins += 1 if s["net"] > 0 else 0
        print(f"{s['symbol']:<11}{s['dir']:<6}{_t(s['sig_time']):<7}{s['sig_close']:>9.2f}"
              f"{s['entry_open']:>9.2f}{s['entry_gap_pts']:>+6.2f}{s['sl']:>9.2f}{s['target']:>9.2f}"
              f"{s['exit']:>9.2f}{s['qty']:>6}{s['R']:>+6.2f}{s['net']:>+8.0f} {s['reason']}")
    if taken_rows:
        m = len(taken_rows)
        avg_gap = sum(s["entry_gap_pct"] for s in taken_rows) / m
        print(f"  TOTAL trades={m} wins={wins} ({100*wins/m:.0f}%) gross={tg:+,.0f} "
              f"charges={sum(s['charges'] for s in taken_rows):,.0f} NET={tn:+,.0f}")
        print(f"  avg late-entry gap = {avg_gap:+.3f}% of signal close")

    # ── SECTION 3: skipped signals (would-be blocked by open position) ──
    skipped = [s for s in all_signals if s["status"] == "SKIPPED_OPEN"]
    print(f"\n── SECTION 3 · SIGNALS SKIPPED (one-position-per-symbol) : {len(skipped)} ──")
    for s in sorted(skipped, key=lambda x: x["sig_time"])[:25]:
        print(f"  {s['symbol']:<11}{s['dir']:<6}{_t(s['sig_time'])}  (already in {s['symbol']})")
    if len(skipped) > 25:
        print(f"  ... and {len(skipped)-25} more (see {sig_csv})")

    # ── SECTION 4: no-signal / no-data ──
    print(f"\n── SECTION 4 · NO SIGNAL ({len(nosignal)}) / NO DATA ({len(nodata)}) ──")
    print("  no-signal:", ", ".join(nosignal) if nosignal else "(none)")
    print("  no-data  :", ", ".join(nodata) if nodata else "(none)")
    print("=" * 100)
    print(f"Verify: open {bars_csv} (every bar+flags) and {sig_csv} (every signal) next to your chart.")


if __name__ == "__main__":
    main()
