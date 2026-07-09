#!/usr/bin/env python3
"""
scan_enhanced_today.py
----------------------
Full TradeFinder Sector Scope scan using the TradingView Pine `enhancedMode`
state machine (active-zone suppression + SL-mode lockout + forced SL-flip),
so the per-stock signal list matches the chart 1:1.

This is the reference implementation for the landing-page port. It reuses the
data layer (TradeFinder sector scope + Upstox 5m candles) from
scan_today_signals.py and only swaps the signal engine.

Config (Pine published defaults, chart-aligned):
  single AND double breakouts, VWAP filter ON, ADX OFF, candle High/Low SL,
  no min-SL gate, enhancedMode ON, uniqueSignal OFF, session 0915-1525.

Usage:
    cd /Users/Shared/Project/openalgo
    /Users/bond7/.local/bin/uv run python strategies/scan_enhanced_today.py
"""

import os, sys, time, concurrent.futures
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
import pandas as pd
import strategies.scan_today_signals as base


def fetch_today_5m(symbol: str, token: str):
    """Fetch ONLY today's intraday 5m bars (half the requests vs intraday+historical).
    Sufficient for enhancedMode: candle-based SL + daily-reset VWAP, no ATR/ADX warmup."""
    key = base._resolve(symbol)
    if not key:
        return None
    enc = requests.utils.quote(key, safe="")
    url = f"{base.UPSTOX_V3}/historical-candle/intraday/{enc}/minutes/5"
    headers = {"accept": "application/json", "Authorization": f"Bearer {token}"}
    candles = None
    for attempt in range(6):
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 429:
            if attempt == 5:
                raise RuntimeError("upstox_rate_limited_429")
            time.sleep(2 ** attempt)
            continue
        if not r.ok:
            return None
        d = r.json()
        if d.get("status") != "success":
            return None
        candles = d.get("data", {}).get("candles", [])
        break
    if not candles:
        return None
    by_time = {}
    for c in candles:
        t = base._baked_sec(c[0])
        by_time[t] = {"timestamp": t, "open": float(c[1]), "high": float(c[2]),
                      "low": float(c[3]), "close": float(c[4]),
                      "volume": float(c[5]) if len(c) > 5 else 0}
    if not by_time:
        return None
    return pd.DataFrame(sorted(by_time.values(), key=lambda x: x["timestamp"]))


def sim_enhanced(symbol: str, token: str, today: str):
    """Replay today's session under Pine enhancedMode. Returns list of signals.
    SL basis = ATR(ATR_LEN)×ATR_MULT (chart setting), warmed on prior-day bars."""
    # ATR needs warmup → fetch intraday + prior days (2 calls), then warm ATR on all of it.
    df = base.fetch_candles_upstox(symbol, token, lookback_days=5)
    if df is None:
        return None  # unresolved / no data
    d = base._normalise_upstox(df)
    if base.USE_ATR_SL:
        d = base.add_atr(d, base.ATR_LEN)
    d = d[(d["_date"] == today) &
          (d["_hm"] >= base.SESSION_START_MIN) & (d["_hm"] <= base.SESSION_END_MIN)]
    if len(d) < 4:
        return []
    d = base.add_vwap(d)
    d = d.reset_index(drop=True)

    # drop forming bar only if the clock says it's still in progress
    last_hm = int(d.iloc[-1]["_hm"])
    if base._cur_min() < last_hm + 5:
        d = d.iloc[:-1]
    if len(d) < 3:
        return []

    actives = []          # {k, lvl, entry, born, act}
    sl_mode = False
    out = []

    for i in range(len(d)):
        b = d.iloc[i]
        hm = int(b["_hm"]); c = float(b["close"]); o = float(b["open"])
        h = float(b["high"]); lo = float(b["low"]); vw = float(b["vwap"])
        new_session = (i == 0)
        # ATR-based stop distance (chart: Entry ± ATR×mult). Falls back to candle level.
        _atr = float(b["atr"]) if (base.USE_ATR_SL and "atr" in d.columns and not pd.isna(b["atr"])) else None
        _ad = _atr * base.ATR_MULT if _atr else None
        sl_long = (c - _ad) if _ad else None      # stop below entry for a LONG
        sl_short = (c + _ad) if _ad else None      # stop above entry for a SHORT

        # 1) break detection + one forced-opposite per bar
        forced = False
        for z in actives:
            if not z["act"]:
                continue
            brk = (z["k"] == 1 and c < z["lvl"] and i > z["born"]) or \
                  (z["k"] == -1 and c > z["lvl"] and i > z["born"])
            if brk:
                z["act"] = False
                if not forced:
                    if z["k"] == 1 and c < vw:        # long SL broke → forced SHORT
                        lvl = sl_short if sl_short else h
                        actives.append({"k": -1, "lvl": lvl, "entry": c, "born": i, "act": True})
                        sl_mode = True; forced = True
                        out.append((b["_dt"].strftime("%H:%M"), "SHORT", "SL-flip", round(c, 2), round(vw, 2)))
                    elif z["k"] == -1 and c > vw:     # short SL broke → forced LONG
                        lvl = sl_long if sl_long else lo
                        actives.append({"k": 1, "lvl": lvl, "entry": c, "born": i, "act": True})
                        sl_mode = True; forced = True
                        out.append((b["_dt"].strftime("%H:%M"), "LONG", "SL-flip", round(c, 2), round(vw, 2)))

        has_buy = any(z["act"] and z["k"] == 1 for z in actives)
        has_sell = any(z["act"] and z["k"] == -1 for z in actives)

        # 2) normal signals (only within entry window)
        if i >= 2 and base.SESSION_START_MIN <= hm <= base.ENTRY_CUTOFF_MIN:
            p1 = d.iloc[i - 1]; p2 = d.iloc[i - 2]
            grn = c > o; red = c < o
            pg = p1["close"] > p1["open"]; pr = p1["close"] < p1["open"]
            p2r = p2["close"] < p2["open"]; p2g = p2["close"] > p2["open"]
            buy1 = grn and pr and c > p1["high"]
            buy2 = grn and pg and p2r and c > p2["high"]
            sell1 = red and pg and c < p1["low"]
            sell2 = red and pr and p2g and c < p2["low"]

            ins_buy = any(z["act"] and z["k"] == 1 and
                          min(z["lvl"], z["entry"]) <= c <= max(z["lvl"], z["entry"]) for z in actives)
            ins_sell = any(z["act"] and z["k"] == -1 and
                           min(z["lvl"], z["entry"]) <= c <= max(z["lvl"], z["entry"]) for z in actives)

            buy_cond = (buy1 or buy2) and (not has_buy) and not new_session
            sell_cond = (sell1 or sell2) and (not has_sell) and not new_session

            if buy_cond and not ins_sell and not sl_mode and c > vw and not forced:
                lvl = sl_long if sl_long else (min(lo, p1["low"]) if buy1 else min(lo, p1["low"], p2["low"]))
                actives.append({"k": 1, "lvl": lvl, "entry": c, "born": i, "act": True})
                out.append((b["_dt"].strftime("%H:%M"), "LONG", "single" if buy1 else "double", round(c, 2), round(vw, 2)))
            elif sell_cond and not ins_buy and not sl_mode and c < vw and not forced:
                lvl = sl_short if sl_short else (max(h, p1["high"]) if sell1 else max(h, p1["high"], p2["high"]))
                actives.append({"k": -1, "lvl": lvl, "entry": c, "born": i, "act": True})
                out.append((b["_dt"].strftime("%H:%M"), "SHORT", "single" if sell1 else "double", round(c, 2), round(vw, 2)))

    return out


def _scan(args):
    sym, sector, token, today = args
    try:
        sigs = sim_enhanced(sym, token, today)
        return sym, sector, sigs, None
    except Exception as e:
        return sym, sector, None, str(e)


def main():
    print("Fetching TradeFinder Sector Scope...")
    sector_data = base.fetch_sector_scope()
    all_syms = {}
    for sector, stocks in sector_data.items():
        for sym in stocks:
            all_syms.setdefault(sym, sector)
    today = base._now_ist().strftime("%Y-%m-%d")
    print(f"Date (IST): {today}")
    print(f"Universe  : {len(all_syms)} stocks across {len(sector_data)} sectors")
    print("Engine    : enhancedMode (active-zone + SL-mode + SL-flip), VWAP on, ADX off, candle SL")
    print("-" * 96)

    token = base._get_upstox_token()
    sym_list = sorted(all_syms)
    WORKERS = 2   # low concurrency to respect Upstox/Cloudflare rate limit

    results = {}; nodata = []
    pending = list(sym_list)
    MAX_ROUNDS = 6
    for rnd in range(1, MAX_ROUNDS + 1):
        if not pending:
            break
        rate_limited = []
        done = 0
        print(f"\n[round {rnd}] scanning {len(pending)} symbol(s)...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(_scan, (s, all_syms[s], token, today)): s for s in pending}
            for fut in concurrent.futures.as_completed(futs):
                sym, sector, sigs, err = fut.result()
                done += 1
                if err and "429" in err:
                    rate_limited.append(sym)
                elif err:
                    nodata.append(sym)            # treat other errors as no-data
                elif sigs is None:
                    nodata.append(sym)
                else:
                    results[sym] = (sector, sigs)
                print(f"  {done}/{len(pending)} | ok={len(results)} | rl={len(rate_limited)} | nodata={len(nodata)}   ", end="\r")
        pending = rate_limited
        if pending and rnd < MAX_ROUNDS:
            print(f"\n  {len(pending)} rate-limited → cooling down 60s before retry...")
            time.sleep(60)

    errors = [(s, "rate_limited") for s in pending]
    print()
    # Flat signal table sorted by time
    rows = []
    for sym, (sector, sigs) in results.items():
        for (t, dirn, kind, px, vw) in sigs:
            rows.append((t, sym, dirn, kind, px, vw, sector))
    rows.sort(key=lambda r: (r[0], r[1]))

    print("\n===== ALL SIGNALS (sorted by time) =====")
    hdr = f"{'TIME':<6} {'SYMBOL':<14} {'DIR':<6} {'KIND':<8} {'ENTRY':>10} {'VWAP':>10}  SECTOR"
    print(hdr); print("-" * len(hdr))
    for t, sym, dirn, kind, px, vw, sector in rows:
        print(f"{t:<6} {sym:<14} {dirn:<6} {kind:<8} {px:>10.2f} {vw:>10.2f}  {sector}")

    # Per-symbol counts (stocks with >0 signals)
    print("\n===== PER-STOCK SIGNAL COUNT =====")
    counts = sorted(((sym, len(sigs), sector) for sym, (sector, sigs) in results.items() if sigs),
                    key=lambda x: (-x[1], x[0]))
    for sym, n, sector in counts:
        times = " ".join(f"{t}{d[0]}" for (t, d, k, p, v) in results[sym][1])
        print(f"  {sym:<14} {n:>2}  [{sector}]  {times}")

    total_sigs = sum(len(sigs) for _, (_, sigs) in results.items())
    stocks_with = sum(1 for _, (_, sigs) in results.items() if sigs)
    print("-" * 96)
    print(f"Scanned {len(results)} ok | {len(nodata)} no-data | {len(errors)} errors")
    print(f"Stocks with >=1 signal: {stocks_with} | Total signals: {total_sigs}")
    if errors[:5]:
        print("Sample errors:", errors[:5])


if __name__ == "__main__":
    main()
