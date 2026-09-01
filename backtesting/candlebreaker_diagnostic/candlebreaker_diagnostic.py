#!/usr/bin/env python3
"""
Candlebreaker SL-hit diagnostic backtest.

Ports the exact trade state machine from openalgo-chart's
src/utils/indicators/candlebreaker.ts line for line (as of the version with
the daily-resistance filter + trade-completeness fixes), running with
CANDLEBREAKER_DEFAULTS. Every entry/exit decision at bar i reads only
bars[0..i] — no lookahead, matching what the live indicator actually sees
in real time.

Setup, per side:
  BUY : close breaks above previous day's high (PDH) -> wait for the first
        RED candle (the breakout bar itself can BE that candle) -> that
        candle's high/low becomes the reference -> entry when close breaks
        the reference high, SL = reference low, target = entry + R:R*risk.
  SELL: mirror image off PDL with a GREEN reference.
One trade per symbol per day. Once price reaches target, SL jumps straight
to breakeven (no partial exit) and then trails behind the most recent
opposite-colour candle's high/low once price closes back through it.

For every SL_HIT trade this script ALSO computes (strictly after the fact,
for diagnosis only -- this is never fed back into the entry/exit decision
above) whether price went on to reach the original target later the same
day. That answers the actual question being asked: is the trailing rule
cutting winners short, or are these genuine reversals?

Usage:
    OPENALGO_API_KEY=... python3 candlebreaker_diagnostic.py [--days 45] [--workers 8]

Requires the `openalgo` pip package and a running OpenAlgo server at
http://127.0.0.1:5000 (matches this machine's dev setup).
"""
import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import pandas as pd
from openalgo import api

HERE = os.path.dirname(os.path.abspath(__file__))
SYMBOLS_FILE = os.path.join(HERE, "symbols.txt")
CACHE_DIR = os.path.join(HERE, "cache")
HOST = "http://127.0.0.1:5000"

# Mirrors CANDLEBREAKER_DEFAULTS in candlebreaker.ts exactly. All the
# off-by-default filters (VWAP, volume, breakout buffer, entry-time window,
# max-distance, hard-reject-SL, reference invalidation) are omitted below
# rather than implemented-but-disabled, since with these defaults they can
# never change the outcome -- see the comment above each skipped branch in
# the TS source for why each one is a no-op when False.
DEFAULTS = dict(
    useMaximumSLFilter=True,
    maximumSL=1.5,
    riskReward=2.0,
    useNextLevelSpaceFilter=True,
    minimumSpaceRR=1.0,
    exitHour=15,
    exitMinute=15,
    historyDays=5,
)


def fetch_history(client, symbol, lookback_days):
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_file = os.path.join(CACHE_DIR, f"{symbol}.csv")
    if os.path.exists(cache_file) and (time.time() - os.path.getmtime(cache_file)) < 3600:
        return pd.read_csv(cache_file, parse_dates=["timestamp"])
    end = datetime.now()
    start = end - timedelta(days=lookback_days)
    try:
        df = client.history(
            symbol=symbol,
            exchange="NSE",
            interval="5m",
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
        )
    except Exception as e:  # noqa: BLE001 - report and skip, don't kill the whole run
        print(f"  [{symbol}] fetch failed: {e}", file=sys.stderr)
        return None
    if df is None or not hasattr(df, "empty") or df.empty:
        return None
    df = df.reset_index().rename(columns={df.reset_index().columns[0]: "timestamp"})
    df.to_csv(cache_file, index=False)
    return df


def simulate_candlebreaker(bars, opts):
    """bars: list of dicts with time (tz-aware datetime), open, high, low, close.
    Returns a list of closed-trade dicts. Faithful port of calculateCandlebreaker's
    trade state machine (candlebreaker.ts) -- causal, no lookahead."""
    n = len(bars)
    trades = []
    if n == 0:
        return trades

    history_days = opts["historyDays"]
    day_key = bars[0]["time"].date()
    day_high, day_low = bars[0]["high"], bars[0]["low"]
    day_high_bar = day_low_bar = 0
    prev_day_high = prev_day_low = None
    high_levels, low_levels = [], []  # [(price, bar_idx), ...]

    trade_active = trade_taken_today = False
    trade_direction = 0
    entry_price = entry_time = current_sl = initial_sl = target_price = None
    mfe = mae = 0.0
    rr_reached = False

    buy_broken = buy_waiting_red = buy_ref_active = False
    buy_ref_high = buy_ref_low = None
    sell_broken = sell_waiting_green = sell_ref_active = False
    sell_ref_high = sell_ref_low = None

    trail_buy_high = trail_buy_low = None
    waiting_buy_trail = False
    trail_sell_high = trail_sell_low = None
    waiting_sell_trail = False

    def trim(levels):
        return levels[-history_days:] if len(levels) > history_days else levels

    def nearest_resistance(price):
        c = [p for p, _ in high_levels if p > price]
        return min(c) if c else None

    def nearest_support(price):
        c = [p for p, _ in low_levels if p < price]
        return max(c) if c else None

    def push_trade(exit_time, exit_price, exit_reason):
        trades.append(
            dict(
                direction="LONG" if trade_direction == 1 else "SHORT",
                entry_time=entry_time,
                entry=entry_price,
                initial_sl=initial_sl,
                target=target_price,
                exit_time=exit_time,
                exit_price=exit_price,
                exit_reason=exit_reason,
                final_sl=current_sl,
                mfe=mfe,
                mae=mae,
            )
        )

    for i in range(n):
        bar = bars[i]
        new_day = bar["time"].date() != day_key

        if new_day:
            high_levels = trim(high_levels + [(day_high, day_high_bar)])
            low_levels = trim(low_levels + [(day_low, day_low_bar)])
            prev_day_high, prev_day_low = day_high, day_low
            day_key = bar["time"].date()
            day_high, day_low = bar["high"], bar["low"]
            day_high_bar = day_low_bar = i

            if trade_active:
                last = bars[i - 1]
                push_trade(last["time"], last["close"], "TIME_EXIT")

            trade_active = trade_taken_today = False
            trade_direction = 0
            entry_price = current_sl = target_price = None
            rr_reached = False
            buy_broken = buy_waiting_red = buy_ref_active = False
            buy_ref_high = buy_ref_low = None
            sell_broken = sell_waiting_green = sell_ref_active = False
            sell_ref_high = sell_ref_low = None
            waiting_buy_trail = waiting_sell_trail = False
        elif i > 0:
            if bar["high"] >= day_high:
                day_high, day_high_bar = bar["high"], i
            if bar["low"] <= day_low:
                day_low, day_low_bar = bar["low"], i

        red = bar["close"] < bar["open"]
        green = bar["close"] > bar["open"]
        has_prev_day = prev_day_high is not None

        # ---- BUY side ----
        if has_prev_day and not trade_taken_today and not buy_broken and bar["close"] > prev_day_high:
            buy_broken = buy_waiting_red = True

        if buy_broken and buy_waiting_red and not trade_taken_today and red:
            buy_ref_high, buy_ref_low = bar["high"], bar["low"]
            buy_ref_active = True
            buy_waiting_red = False

        if buy_ref_active and not trade_taken_today and bar["close"] > buy_ref_high:
            px, sl = bar["close"], buy_ref_low
            risk = px - sl
            buy_signal = False
            if risk > 0:
                risk_pct = (risk / px) * 100
                res = nearest_resistance(px)
                space_rr = None if res is None else (res - px) / risk
                buy_signal = (not opts["useMaximumSLFilter"] or risk_pct <= opts["maximumSL"]) and (
                    not opts["useNextLevelSpaceFilter"] or res is None or space_rr >= opts["minimumSpaceRR"]
                )
            if buy_signal and not trade_active:
                entry_price, entry_time = px, bar["time"]
                current_sl = initial_sl = sl
                target_price = px + risk * opts["riskReward"]
                trade_active = trade_taken_today = True
                trade_direction = 1
                rr_reached = False
                mfe = mae = 0.0
                buy_ref_active = False
            else:
                buy_ref_active = False
                buy_ref_high = buy_ref_low = None
                buy_waiting_red = True

        # ---- SELL side ----
        if has_prev_day and not trade_taken_today and not sell_broken and bar["close"] < prev_day_low:
            sell_broken = sell_waiting_green = True

        if sell_broken and sell_waiting_green and not trade_taken_today and green:
            sell_ref_high, sell_ref_low = bar["high"], bar["low"]
            sell_ref_active = True
            sell_waiting_green = False

        if sell_ref_active and not trade_taken_today and bar["close"] < sell_ref_low:
            px, sl = bar["close"], sell_ref_high
            risk = sl - px
            sell_signal = False
            if risk > 0:
                risk_pct = (risk / px) * 100
                sup = nearest_support(px)
                space_rr = None if sup is None else (px - sup) / risk
                sell_signal = (not opts["useMaximumSLFilter"] or risk_pct <= opts["maximumSL"]) and (
                    not opts["useNextLevelSpaceFilter"] or sup is None or space_rr >= opts["minimumSpaceRR"]
                )
            if sell_signal and not trade_active:
                entry_price, entry_time = px, bar["time"]
                current_sl = initial_sl = sl
                target_price = px - risk * opts["riskReward"]
                trade_active = trade_taken_today = True
                trade_direction = -1
                rr_reached = False
                mfe = mae = 0.0
                sell_ref_active = False
            else:
                sell_ref_active = False
                sell_ref_high = sell_ref_low = None
                sell_waiting_green = True

        # ---- Target / trail / exit ----
        if trade_active:
            if trade_direction == 1:
                fav, adv = bar["high"] - entry_price, entry_price - bar["low"]
            else:
                fav, adv = entry_price - bar["low"], bar["high"] - entry_price
            mfe, mae = max(mfe, fav), max(mae, adv)

            target_reached = (
                bar["high"] >= target_price if trade_direction == 1 else bar["low"] <= target_price
            )
            if target_reached and not rr_reached:
                rr_reached = True
                current_sl = entry_price

            if trade_direction == 1:
                if red:
                    trail_buy_high, trail_buy_low, waiting_buy_trail = bar["high"], bar["low"], True
                if waiting_buy_trail and bar["close"] > trail_buy_high:
                    if trail_buy_low > current_sl:
                        current_sl = trail_buy_low
                    waiting_buy_trail = False
            else:
                if green:
                    trail_sell_high, trail_sell_low, waiting_sell_trail = bar["high"], bar["low"], True
                if waiting_sell_trail and bar["close"] < trail_sell_low:
                    if trail_sell_high < current_sl:
                        current_sl = trail_sell_high
                    waiting_sell_trail = False

        if trade_active:
            stop_hit = bar["low"] <= current_sl if trade_direction == 1 else bar["high"] >= current_sl
            time_exit = (bar["time"].hour > opts["exitHour"]) or (
                bar["time"].hour == opts["exitHour"] and bar["time"].minute >= opts["exitMinute"]
            )
            if stop_hit or time_exit:
                trade_active = False
                push_trade(bar["time"], current_sl if stop_hit else bar["close"], "SL_HIT" if stop_hit else "TIME_EXIT")

    if trade_active:
        last = bars[-1]
        push_trade(last["time"], last["close"], "TIME_EXIT")

    return trades


def post_stop_diagnostic(trade, bars):
    """Diagnosis only -- computed AFTER the full day's bars are known, never
    consulted by simulate_candlebreaker's own entry/exit decisions above.
    Answers: for a stopped-out trade, did price still reach the original
    target later the same day?"""
    if trade["exit_reason"] != "SL_HIT":
        return {}
    exit_time = trade["exit_time"]
    later = [b for b in bars if b["time"].date() == exit_time.date() and b["time"] > exit_time]
    if not later:
        return {"target_reached_after_stop": False, "best_price_after_stop": None}
    if trade["direction"] == "LONG":
        best = max(b["high"] for b in later)
        target_reached = best >= trade["target"]
        at_or_above_be = trade["final_sl"] >= trade["entry"]
    else:
        best = min(b["low"] for b in later)
        target_reached = best <= trade["target"]
        at_or_above_be = trade["final_sl"] <= trade["entry"]
    return {
        "target_reached_after_stop": target_reached,
        "best_price_after_stop": best,
        "exited_at_or_above_breakeven": at_or_above_be,
    }


def process_symbol(client, symbol, lookback_days):
    df = fetch_history(client, symbol, lookback_days)
    if df is None or len(df) < 20:
        return symbol, [], "no data"
    bars = [
        dict(time=row.timestamp, open=row.open, high=row.high, low=row.low, close=row.close)
        for row in df.itertuples()
    ]
    trades = simulate_candlebreaker(bars, DEFAULTS)
    for t in trades:
        t["symbol"] = symbol
        t.update(post_stop_diagnostic(t, bars))
    return symbol, trades, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45, help="calendar days of 5m history to fetch")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    api_key = os.environ.get("OPENALGO_API_KEY")
    if not api_key:
        sys.exit("Set OPENALGO_API_KEY in the environment first.")
    symbols = [l.strip() for l in open(SYMBOLS_FILE) if l.strip()]
    client = api(api_key=api_key, host=HOST)

    all_trades = []
    errors = []
    print(f"Fetching + simulating {len(symbols)} symbols, {args.days}d of 5m history...", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(process_symbol, client, s, args.days): s for s in symbols}
        for i, fut in enumerate(as_completed(futures)):
            symbol, trades, err = fut.result()
            if err:
                errors.append((symbol, err))
            else:
                all_trades.extend(trades)
            if (i + 1) % 20 == 0 or (i + 1) == len(symbols):
                print(f"  ...{i + 1}/{len(symbols)}", file=sys.stderr)

    if errors:
        print(f"\n{len(errors)} symbols skipped (no data / fetch error): "
              f"{', '.join(s for s, _ in errors[:10])}{' ...' if len(errors) > 10 else ''}", file=sys.stderr)

    df = pd.DataFrame(all_trades)
    out_csv = os.path.join(HERE, "candlebreaker_trades.csv")
    df.to_csv(out_csv, index=False)
    print(f"\nSaved {len(df)} trades -> {out_csv}")

    total = len(df)
    if total == 0:
        print("No trades produced -- check data fetch above.")
        return

    sl_hit = df[df.exit_reason == "SL_HIT"]
    time_exit = df[df.exit_reason == "TIME_EXIT"]
    n_sl, n_time = len(sl_hit), len(time_exit)

    print(f"\n=== Candlebreaker diagnostic: {len(symbols)} symbols, {args.days}d, 5m, defaults ===")
    print(f"Total trades: {total}")
    print(f"  SL_HIT:    {n_sl} ({n_sl/total*100:.1f}%)")
    print(f"  TIME_EXIT: {n_time} ({n_time/total*100:.1f}%)")

    if n_sl:
        be_or_better = sl_hit[sl_hit["exited_at_or_above_breakeven"] == True]  # noqa: E712
        real_loss = sl_hit[sl_hit["exited_at_or_above_breakeven"] == False]  # noqa: E712
        print(f"\nOf {n_sl} SL_HIT trades:")
        print(f"  {len(real_loss)} ({len(real_loss)/n_sl*100:.1f}%) stopped BELOW entry"
              f" -- genuine adverse move, SL never trailed to breakeven")
        print(f"  {len(be_or_better)} ({len(be_or_better)/n_sl*100:.1f}%) stopped AT/ABOVE breakeven"
              f" -- the trade worked first, then got trailed out")
        if len(be_or_better) > 0:
            target_after = be_or_better[be_or_better["target_reached_after_stop"] == True]  # noqa: E712
            pct = len(target_after) / len(be_or_better) * 100
            print(f"    -> of those, {len(target_after)} ({pct:.1f}%) saw price reach the ORIGINAL"
                  f" target later the same day -- the trail cut them before the real move completed")

    avg_mfe = df["mfe"].mean()
    avg_mfe_sl = sl_hit["mfe"].mean() if n_sl else 0
    print(f"\nAvg MFE (all trades): {avg_mfe:.2f} pts | Avg MFE (SL_HIT trades only): {avg_mfe_sl:.2f} pts")

    # Per-symbol SL-hit rate, worst offenders first
    by_symbol = df.groupby("symbol").agg(
        trades=("symbol", "count"),
        sl_hits=("exit_reason", lambda s: (s == "SL_HIT").sum()),
    )
    by_symbol["sl_rate"] = (by_symbol["sl_hits"] / by_symbol["trades"] * 100).round(1)
    by_symbol = by_symbol[by_symbol["trades"] >= 3].sort_values("sl_rate", ascending=False)
    print("\nWorst SL-hit rate (>=3 trades):")
    print(by_symbol.head(20).to_string())

    by_symbol_csv = os.path.join(HERE, "candlebreaker_by_symbol.csv")
    by_symbol.to_csv(by_symbol_csv)
    print(f"\nPer-symbol breakdown -> {by_symbol_csv}")


if __name__ == "__main__":
    main()
