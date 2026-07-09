#!/usr/bin/env python3
"""
backfill_today_signals.py
-------------------------
One-shot: recompute today's enhancedMode + ATR(14)x1.5 signals across the whole
TradeFinder Sector Scope and write them into the landing-page dashboard DB
(prisma/dev.db `Signal` table), replacing today's rows.

Use this only to make the dashboard reflect the new logic for a day the live
engine missed (e.g. after a code change, after hours). Going forward the engine
writes these itself during market hours.

    cd /Users/Shared/Project/openalgo
    /Users/bond7/.local/bin/uv run python strategies/backfill_today_signals.py
"""

import os, sys, time, sqlite3, secrets, concurrent.futures
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategies.scan_today_signals as base

DB_PATH = "/Users/Shared/Project/indicator/landing-page/prisma/dev.db"

# Match buildSectorScanConfig() in the landing page
BALANCE = 311000.0
MAX_LOSS_PCT = 1.0
CAPITAL_PER_TRADE = 50000.0
TARGET_RR = 2.0


def compute_qty(entry, sl):
    sl_dist = abs(entry - sl)
    if sl_dist < 0.01 or entry <= 0:
        return 0
    risk_budget = BALANCE * (MAX_LOSS_PCT / 100)
    qty_risk = int(risk_budget / sl_dist)
    qty_cap = int(CAPITAL_PER_TRADE / entry)
    return max(0, min(qty_risk, qty_cap))


def sim(symbol, token, today):
    """enhancedMode + ATR(14)x1.5 replay → list of full signal dicts."""
    df = base.fetch_candles_upstox(symbol, token, lookback_days=5)
    if df is None:
        return None
    d = base._normalise_upstox(df)
    if base.USE_ATR_SL:
        d = base.add_atr(d, base.ATR_LEN)
    d = d[(d["_date"] == today) &
          (d["_hm"] >= base.SESSION_START_MIN) & (d["_hm"] <= base.SESSION_END_MIN)]
    if len(d) < 4:
        return []
    d = base.add_vwap(d)
    d = base.add_adx(d, base.ADX_LEN)
    d = d.reset_index(drop=True)
    last_hm = int(d.iloc[-1]["_hm"])
    if base._cur_min() < last_hm + 5:
        d = d.iloc[:-1]
    if len(d) < 3:
        return []

    actives = []
    sl_mode = False
    out = []

    def mk(direction, kind, entry, sl, vw, adxv, ts_sec, t):
        sl_dist = abs(entry - sl)
        sl_pct = (sl_dist / entry * 100) if entry > 0 else 0
        target = (entry + TARGET_RR * sl_dist) if direction == "LONG" else (entry - TARGET_RR * sl_dist)
        return {
            "symbol": symbol, "exchange": "NSE", "direction": direction, "kind": kind,
            "entry": round(entry, 2), "sl": round(sl, 2), "slPct": round(sl_pct, 2),
            "target": round(target, 2), "qty": compute_qty(entry, sl),
            "adx": round(adxv or 0, 1), "signalTime": t,
            "signalTs": int(ts_sec) * 1000, "tradingDay": today,
        }

    for i in range(len(d)):
        b = d.iloc[i]
        hm = int(b["_hm"]); c = float(b["close"]); o = float(b["open"])
        h = float(b["high"]); lo = float(b["low"]); vw = float(b["vwap"])
        adxv = float(b["adx"]) if not base.pd.isna(b["adx"]) else 0.0
        a = float(b["atr"]) if (base.USE_ATR_SL and "atr" in d.columns and not base.pd.isna(b["atr"])) else None
        ad = a * base.ATR_MULT if a else None
        sl_long = (c - ad) if ad else None
        sl_short = (c + ad) if ad else None
        ts_sec = int(b["timestamp"]); t = b["_dt"].strftime("%H:%M")
        new_session = (i == 0)

        forced = False
        for z in actives:
            if not z["act"]:
                continue
            brk = (z["k"] == 1 and c < z["lvl"] and i > z["born"]) or \
                  (z["k"] == -1 and c > z["lvl"] and i > z["born"])
            if not brk:
                continue
            z["act"] = False
            if forced:
                continue
            if z["k"] == 1 and (not base.USE_VWAP or c < vw):
                lvl = sl_short if sl_short else h
                actives.append({"k": -1, "lvl": lvl, "entry": c, "born": i, "act": True})
                sl_mode = True; forced = True
                out.append(mk("SHORT", "double", c, lvl, vw, adxv, ts_sec, t))
            elif z["k"] == -1 and (not base.USE_VWAP or c > vw):
                lvl = sl_long if sl_long else lo
                actives.append({"k": 1, "lvl": lvl, "entry": c, "born": i, "act": True})
                sl_mode = True; forced = True
                out.append(mk("LONG", "double", c, lvl, vw, adxv, ts_sec, t))

        has_buy = any(z["act"] and z["k"] == 1 for z in actives)
        has_sell = any(z["act"] and z["k"] == -1 for z in actives)

        if i >= 2 and base.SESSION_START_MIN <= hm <= base.ENTRY_CUTOFF_MIN:
            p1 = d.iloc[i - 1]; p2 = d.iloc[i - 2]
            grn = c > o; red = c < o
            pg = p1["close"] > p1["open"]; pr = p1["close"] < p1["open"]
            p2r = p2["close"] < p2["open"]; p2g = p2["close"] > p2["open"]
            buy1 = base.ENABLE_SINGLE and base.ENABLE_BUY and grn and pr and c > p1["high"]
            buy2 = base.ENABLE_DOUBLE and base.ENABLE_BUY and grn and pg and p2r and c > p2["high"]
            sell1 = base.ENABLE_SINGLE and base.ENABLE_SELL and red and pg and c < p1["low"]
            sell2 = base.ENABLE_DOUBLE and base.ENABLE_SELL and red and pr and p2g and c < p2["low"]
            ins_buy = any(z["act"] and z["k"] == 1 and min(z["lvl"], z["entry"]) <= c <= max(z["lvl"], z["entry"]) for z in actives)
            ins_sell = any(z["act"] and z["k"] == -1 and min(z["lvl"], z["entry"]) <= c <= max(z["lvl"], z["entry"]) for z in actives)
            buy_cond = (buy1 or buy2) and not has_buy and not new_session
            sell_cond = (sell1 or sell2) and not has_sell and not new_session
            adx_ok = (not base.USE_ADX) or adxv >= base.ADX_THRESHOLD
            if buy_cond and not ins_sell and not sl_mode and (not base.USE_VWAP or c > vw) and adx_ok and not forced:
                lvl = sl_long if sl_long else (min(lo, p1["low"]) if buy1 else min(lo, p1["low"], p2["low"]))
                actives.append({"k": 1, "lvl": lvl, "entry": c, "born": i, "act": True})
                out.append(mk("LONG", "single" if buy1 else "double", c, lvl, vw, adxv, ts_sec, t))
            elif sell_cond and not ins_buy and not sl_mode and (not base.USE_VWAP or c < vw) and adx_ok and not forced:
                lvl = sl_short if sl_short else (max(h, p1["high"]) if sell1 else max(h, p1["high"], p2["high"]))
                actives.append({"k": -1, "lvl": lvl, "entry": c, "born": i, "act": True})
                out.append(mk("SHORT", "single" if sell1 else "double", c, lvl, vw, adxv, ts_sec, t))
    return out


def _scan(args):
    sym, token, today = args
    try:
        return sym, sim(sym, token, today), None
    except Exception as e:
        return sym, None, str(e)


def main():
    print("Fetching sector scope...")
    sd = base.fetch_sector_scope()
    syms = sorted({s for stocks in sd.values() for s in stocks})
    today = base._now_ist().strftime("%Y-%m-%d")
    token = base._get_upstox_token()
    print(f"Date {today} | {len(syms)} symbols | config ATR{base.ATR_LEN}x{base.ATR_MULT} USE_ATR_SL={base.USE_ATR_SL}")

    all_sigs = []
    pending = list(syms)
    for rnd in range(1, 7):
        if not pending:
            break
        rl = []; done = 0
        print(f"[round {rnd}] {len(pending)} symbols")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            futs = {ex.submit(_scan, (s, token, today)): s for s in pending}
            for fut in concurrent.futures.as_completed(futs):
                sym, sigs, err = fut.result(); done += 1
                if err and "429" in err:
                    rl.append(sym)
                elif sigs:
                    all_sigs.extend(sigs)
                print(f"  {done}/{len(pending)} | sigs={len(all_sigs)} | rl={len(rl)}   ", end="\r")
        pending = rl
        if pending and rnd < 6:
            print(f"\n  {len(pending)} rate-limited, cooling 60s..."); time.sleep(60)
    print()

    # Write to DB: replace today's rows
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    deleted = cur.execute("DELETE FROM Signal WHERE tradingDay=?", (today,)).rowcount
    rows = [(
        "bf" + secrets.token_hex(12), s["symbol"], s["exchange"], s["direction"], s["kind"],
        s["entry"], s["sl"], s["slPct"], s["target"], s["qty"], s["adx"],
        s["signalTime"], s["signalTs"], s["tradingDay"],
    ) for s in all_sigs]
    cur.executemany(
        "INSERT OR IGNORE INTO Signal "
        "(id,symbol,exchange,direction,kind,entry,sl,slPct,target,qty,adx,signalTime,signalTs,tradingDay) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    inserted = cur.execute("SELECT count(*) FROM Signal WHERE tradingDay=?", (today,)).fetchone()[0]
    con.close()
    print(f"Deleted {deleted} old rows | computed {len(all_sigs)} signals | DB now has {inserted} rows for {today}")


if __name__ == "__main__":
    main()
