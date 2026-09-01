#!/usr/bin/env python3
"""
Candlebreaker parameter sweep + realistic P&L.

Reuses the cached 5m bars from candlebreaker_diagnostic.py's run (no
re-fetch, no lookahead — same causal simulate_candlebreaker as before, just
parameterized on two levers the diagnostic identified):

  1. min_risk_pct  -- reject an entry whose reference-candle SL distance is
     below this % of price. Candlebreaker has no floor today (only a 1.5%
     CEILING via maximumSL) -- the diagnostic found median SL width was
     0.40%, tighter than normal 5m noise on these names.
  2. trail_confirm  -- how many CONSECUTIVE opposite-colour candles must
     print before the trail arms (current live behaviour = 1, a single
     candle). Diagnostic found ~11% of all trades were trailed out of a
     position that later reached target.

Then sizes every trade exactly like the live scanner/backtest engine
(computeISIQty in isiBacktestEngine.ts) and charges it exactly like
backtestCharges.ts's computeCharges, for a real ₹/₹% P&L, not just points.

Usage:
    python3 candlebreaker_sweep.py --balance 2000000 --capital-per-trade 50000 \
        --max-loss-pct 1.0 --margin-pct 20
"""
import argparse
import itertools
import json
import os

import pandas as pd

from candlebreaker_diagnostic import CACHE_DIR, DEFAULTS

HERE = os.path.dirname(os.path.abspath(__file__))
SYMBOLS_FILE = os.path.join(HERE, "symbols.txt")

DEFAULT_CHARGES = dict(
    brokeragePerOrder=20.0,
    brokeragePercent=0.0003,
    stt=0.00025,
    nseTransaction=0.0000307,
    sebi=0.000001,
    stampDuty=0.00003,
    gst=0.18,
)


def compute_charges(entry, exit_price, qty, cfg, direction):
    buy_t = (entry if direction == "LONG" else exit_price) * qty
    sell_t = (exit_price if direction == "LONG" else entry) * qty
    total_t = buy_t + sell_t
    brok = min(cfg["brokeragePerOrder"], buy_t * cfg["brokeragePercent"]) + min(
        cfg["brokeragePerOrder"], sell_t * cfg["brokeragePercent"]
    )
    stt = sell_t * cfg["stt"]
    txn = total_t * cfg["nseTransaction"]
    sebi = total_t * cfg["sebi"]
    stamp = buy_t * cfg["stampDuty"]
    gst = (brok + sebi + txn) * cfg["gst"]
    total = brok + stt + txn + sebi + stamp + gst
    return dict(brokerage=brok, stt=stt, txn=txn, sebi=sebi, stamp=stamp, gst=gst, total=total)


def compute_qty(entry, sl_dist, balance, max_loss_pct, capital_per_trade, margin_pct):
    if entry <= 0 or sl_dist <= 0:
        return 0
    risk_budget = balance * (max_loss_pct / 100)
    margin_frac = min(max(margin_pct, 1), 100) / 100
    risk_qty_cap = int(risk_budget // sl_dist)
    margin_qty_cap = int(capital_per_trade // (entry * margin_frac))
    return max(0, min(risk_qty_cap, margin_qty_cap))


def simulate_variant(bars, opts, min_risk_pct, trail_confirm):
    """Same state machine as simulate_candlebreaker in candlebreaker_diagnostic.py,
    with two added knobs: min_risk_pct (SL-width floor) and trail_confirm
    (consecutive opposite-colour candles required to arm the trail)."""
    n = len(bars)
    trades = []
    if n == 0:
        return trades

    history_days = opts["historyDays"]
    day_key = bars[0]["time"].date()
    day_high, day_low = bars[0]["high"], bars[0]["low"]
    day_high_bar = day_low_bar = 0
    prev_day_high = prev_day_low = None
    high_levels, low_levels = [], []

    trade_active = trade_taken_today = False
    trade_direction = 0
    entry_price = entry_time = current_sl = initial_sl = target_price = None
    mfe = mae = 0.0
    rr_reached = False

    buy_broken = buy_waiting_red = buy_ref_active = False
    buy_ref_high = buy_ref_low = None
    sell_broken = sell_waiting_green = sell_ref_active = False
    sell_ref_high = sell_ref_low = None

    # Trail arms once `trail_confirm` consecutive opposite-colour candles have
    # printed; trail_low/high track the min/max of that run.
    buy_run_len = 0
    buy_run_low = buy_run_high = None
    waiting_buy_trail = False
    sell_run_len = 0
    sell_run_low = sell_run_high = None
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
            buy_run_len = sell_run_len = 0
        elif i > 0:
            if bar["high"] >= day_high:
                day_high, day_high_bar = bar["high"], i
            if bar["low"] <= day_low:
                day_low, day_low_bar = bar["low"], i

        red = bar["close"] < bar["open"]
        green = bar["close"] > bar["open"]
        has_prev_day = prev_day_high is not None

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
                buy_signal = (
                    (not opts["useMaximumSLFilter"] or risk_pct <= opts["maximumSL"])
                    and risk_pct >= min_risk_pct
                    and (not opts["useNextLevelSpaceFilter"] or res is None or space_rr >= opts["minimumSpaceRR"])
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
                sell_signal = (
                    (not opts["useMaximumSLFilter"] or risk_pct <= opts["maximumSL"])
                    and risk_pct >= min_risk_pct
                    and (not opts["useNextLevelSpaceFilter"] or sup is None or space_rr >= opts["minimumSpaceRR"])
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

        if trade_active:
            if trade_direction == 1:
                fav, adv = bar["high"] - entry_price, entry_price - bar["low"]
            else:
                fav, adv = entry_price - bar["low"], bar["high"] - entry_price
            mfe, mae = max(mfe, fav), max(mae, adv)

            target_reached = bar["high"] >= target_price if trade_direction == 1 else bar["low"] <= target_price
            if target_reached and not rr_reached:
                rr_reached = True
                current_sl = entry_price

            if trade_direction == 1:
                if red:
                    buy_run_len += 1
                    buy_run_low = bar["low"] if buy_run_low is None else min(buy_run_low, bar["low"])
                    buy_run_high = bar["high"]
                    if buy_run_len >= trail_confirm:
                        waiting_buy_trail = True
                else:
                    if not waiting_buy_trail:
                        buy_run_len = 0
                        buy_run_low = None
                if waiting_buy_trail and bar["close"] > buy_run_high:
                    if buy_run_low > current_sl:
                        current_sl = buy_run_low
                    waiting_buy_trail = False
                    buy_run_len = 0
                    buy_run_low = None
            else:
                if green:
                    sell_run_len += 1
                    sell_run_high = bar["high"] if sell_run_high is None else max(sell_run_high, bar["high"])
                    sell_run_low = bar["low"]
                    if sell_run_len >= trail_confirm:
                        waiting_sell_trail = True
                else:
                    if not waiting_sell_trail:
                        sell_run_len = 0
                        sell_run_high = None
                if waiting_sell_trail and bar["close"] < sell_run_low:
                    if sell_run_high < current_sl:
                        current_sl = sell_run_high
                    waiting_sell_trail = False
                    sell_run_len = 0
                    sell_run_high = None

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


def load_all_bars():
    symbols = [l.strip() for l in open(SYMBOLS_FILE) if l.strip()]
    out = {}
    for s in symbols:
        f = os.path.join(CACHE_DIR, f"{s}.csv")
        if not os.path.exists(f):
            continue
        df = pd.read_csv(f, parse_dates=["timestamp"])
        out[s] = [
            dict(time=row.timestamp, open=row.open, high=row.high, low=row.low, close=row.close)
            for row in df.itertuples()
        ]
    return out


def load_boost_days():
    """symbol -> set of date strings ('YYYY-MM-DD') it was actually in
    TradeFinder's 'intraday_boost' momentum list, from the real historical
    snapshot DB (db/tf_boost_snapshots.duckdb) -- not reconstructed after
    the fact, these are the snapshots TradeFinder itself captured live
    during those sessions, so restricting to them is still lookahead-free."""
    path = os.path.join(HERE, "boost_days.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        raw = json.load(f)
    return {sym: set(days) for sym, days in raw.items()}


def price_and_pnl(trades, symbol, balance, capital_per_trade, max_loss_pct, margin_pct, charges_cfg, boost_days=None):
    rows = []
    allowed = boost_days.get(symbol) if boost_days is not None else None
    for t in trades:
        if boost_days is not None:
            if allowed is None or t["entry_time"].strftime("%Y-%m-%d") not in allowed:
                continue
        risk_dist = abs(t["entry"] - t["initial_sl"])
        qty = compute_qty(t["entry"], risk_dist, balance, max_loss_pct, capital_per_trade, margin_pct)
        if qty == 0:
            continue
        gross = (t["exit_price"] - t["entry"]) * qty * (1 if t["direction"] == "LONG" else -1)
        ch = compute_charges(t["entry"], t["exit_price"], qty, charges_cfg, t["direction"])
        net = gross - ch["total"]
        rows.append(dict(symbol=symbol, qty=qty, gross_pnl=gross, charges=ch["total"], net_pnl=net, **t))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--balance", type=float, default=2_000_000)
    ap.add_argument("--capital-per-trade", type=float, default=50_000)
    ap.add_argument("--max-loss-pct", type=float, default=1.0)
    ap.add_argument("--margin-pct", type=float, default=20.0)
    args = ap.parse_args()

    print("Loading cached bars for all symbols (no re-fetch)...")
    all_bars = load_all_bars()
    print(f"  {len(all_bars)} symbols loaded from cache")

    boost_days = load_boost_days()
    if boost_days is None:
        print("  boost_days.json not found -- skipping the momentum-day-only variants")
        boost_grid = [False]
    else:
        n_syms = len(boost_days)
        n_days = len(set().union(*boost_days.values())) if boost_days else 0
        print(f"  boost snapshot DB: {n_syms} symbols, {n_days} distinct momentum days")
        boost_grid = [False, True]

    min_risk_grid = [0.0, 0.3, 0.5]
    trail_grid = [1, 2]

    results = []
    for min_risk_pct, trail_confirm, boost_only in itertools.product(min_risk_grid, trail_grid, boost_grid):
        all_priced = []
        for symbol, bars in all_bars.items():
            trades = simulate_variant(bars, DEFAULTS, min_risk_pct, trail_confirm)
            all_priced.extend(
                price_and_pnl(
                    trades, symbol, args.balance, args.capital_per_trade, args.max_loss_pct, args.margin_pct,
                    DEFAULT_CHARGES, boost_days if boost_only else None,
                )
            )
        df = pd.DataFrame(all_priced)
        if df.empty:
            continue
        total_trades = len(df)
        sl_hit = (df["exit_reason"] == "SL_HIT").sum()
        wins = (df["net_pnl"] > 0).sum()
        gross_total = df["gross_pnl"].sum()
        charges_total = df["charges"].sum()
        net_total = df["net_pnl"].sum()
        gross_wins = df.loc[df["gross_pnl"] > 0, "gross_pnl"].sum()
        gross_losses = -df.loc[df["gross_pnl"] < 0, "gross_pnl"].sum()
        profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf")

        results.append(
            dict(
                min_risk_pct=min_risk_pct,
                trail_confirm=trail_confirm,
                momentum_days_only=boost_only,
                trades=total_trades,
                sl_hit_pct=round(sl_hit / total_trades * 100, 1),
                win_pct=round(wins / total_trades * 100, 1),
                gross_pnl=round(gross_total, 0),
                charges=round(charges_total, 0),
                net_pnl=round(net_total, 0),
                profit_factor=round(profit_factor, 2),
                avg_net_per_trade=round(net_total / total_trades, 1),
            )
        )
        out_csv = os.path.join(HERE, f"sweep_min{min_risk_pct}_trail{trail_confirm}_boost{boost_only}.csv")
        df.to_csv(out_csv, index=False)

    summary = pd.DataFrame(results).sort_values("net_pnl", ascending=False)
    print(f"\n=== Parameter sweep — balance ₹{args.balance:,.0f}, "
          f"₹{args.capital_per_trade:,.0f}/trade, max loss {args.max_loss_pct}%, "
          f"margin {args.margin_pct}%, real charges ===\n")
    print(summary.to_string(index=False))
    summary_csv = os.path.join(HERE, "sweep_summary.csv")
    summary.to_csv(summary_csv, index=False)
    print(f"\nSummary -> {summary_csv}")
    print("Per-combo trade-level detail -> sweep_min<X>_trail<Y>.csv")


if __name__ == "__main__":
    main()
