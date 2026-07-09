#!/usr/bin/env python3
"""
NIFTY Options Selling - Intraday Detailed Backtest
====================================================
Full trade-by-trade analysis for top optimizer configs.
Generates: trade logs, monthly breakdown, equity curves, drawdown charts.
"""

import sys
import duckdb
import numpy as np
import pandas as pd
from datetime import datetime, date, time as dtime
from pathlib import Path
from collections import Counter

# ============================================================================
# TOP CONFIGS FROM OPTIMIZER
# ============================================================================
CONFIGS = [
    ("15min ST(7,2.5) 700pt/85% [Best P&L]",       '15min',  7, 2.5, 700, 0.85),
    ("15min ST(20,3.5) 700pt/95% [Best 15m Sharpe]",'15min', 20, 3.5, 700, 0.95),
    ("10min ST(20,6.0) 700pt/85% [Best Sharpe]",   '10min', 20, 6.0, 700, 0.85),
    ("5min ST(7,10.0) 700pt/85% [Conservative]",    '5min',  7, 10.0, 700, 0.85),
]

# Fixed parameters
LOTS            = 5
CONT_SPREAD_WIDTH = 150
STRIKE_INTERVAL = 50
INITIAL_CAPITAL = 400_000
ENABLE_CONT     = True

# Intraday timing
MARKET_OPEN     = dtime(9, 15)
ENTRY_CUTOFF    = dtime(15, 10)
INTRADAY_EXIT   = dtime(15, 20)

# Charges
BROKERAGE_PER_ORDER = 20
STT_SELL_PCT        = 0.001
TXN_CHARGE_PCT      = 0.0003553
SEBI_PER_CRORE      = 10
GST_PCT             = 0.18
STAMP_BUY_PCT       = 0.00003

_script_dir = Path(__file__).resolve().parent
DUCKDB_PATH = str(_script_dir / ".." / ".." / "db" / "historify.duckdb")


# ============================================================================
# SUPERTREND
# ============================================================================
def compute_supertrend(highs, lows, closes, period, multiplier):
    n = len(closes)
    highs  = np.asarray(highs, dtype=float)
    lows   = np.asarray(lows, dtype=float)
    closes = np.asarray(closes, dtype=float)
    tr = np.empty(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i] - closes[i-1]),
                    abs(lows[i]  - closes[i-1]))
    alpha = 1.0 / period
    atr = np.zeros(n)
    if n >= period:
        atr[period-1] = tr[:period].mean()
        for i in range(period, n):
            atr[i] = alpha * tr[i] + (1 - alpha) * atr[i-1]
    else:
        atr[:] = tr.mean()
    hl2       = (highs + lows) / 2.0
    upper_raw = hl2 + multiplier * atr
    lower_raw = hl2 - multiplier * atr
    final_upper = upper_raw.copy()
    final_lower = lower_raw.copy()
    direction   = np.ones(n, dtype=int)
    for i in range(1, n):
        fu = (upper_raw[i]
              if upper_raw[i] < final_upper[i-1] or closes[i-1] > final_upper[i-1]
              else final_upper[i-1])
        fl = (lower_raw[i]
              if lower_raw[i] > final_lower[i-1] or closes[i-1] < final_lower[i-1]
              else final_lower[i-1])
        final_upper[i] = fu
        final_lower[i] = fl
        if direction[i-1] == 1:
            direction[i] = 1 if closes[i] >= fl else -1
        else:
            direction[i] = -1 if closes[i] <= fu else 1
    return direction


def build_option_symbol(expiry, strike, opt_type):
    return f"NIFTY{expiry.strftime('%d%b%y').upper()}{int(strike)}{opt_type.upper()}"


def compute_charges(sell_entry, buy_entry, sell_exit, buy_exit, qty):
    brokerage = BROKERAGE_PER_ORDER * 4
    stt       = STT_SELL_PCT * (sell_entry * qty + buy_exit * qty)
    turnover  = (sell_entry + buy_entry + sell_exit + buy_exit) * qty
    txn       = TXN_CHARGE_PCT * turnover
    sebi      = SEBI_PER_CRORE * turnover / 1e7
    gst       = GST_PCT * (brokerage + txn + sebi)
    stamp     = STAMP_BUY_PCT * (buy_entry * qty + sell_exit * qty)
    return brokerage + stt + txn + sebi + gst + stamp


def resample_to_tf(df1m, tf_str):
    tf_map = {'3min': '15:29', '5min': '15:24', '10min': '15:19', '15min': '15:14'}
    upper = tf_map.get(tf_str, '15:24')
    df_tf = (df1m.resample(tf_str, closed='left', label='left')
                 .agg(open=('open','first'), high=('high','max'),
                      low=('low','min'), close=('close','last'),
                      volume=('volume','sum'))
                 .dropna().between_time('09:15', upper))
    return df_tf


def tf_to_minutes(tf_str):
    return int(tf_str.replace('min', ''))


# ============================================================================
# FULL BACKTEST WITH TRADE LOG
# ============================================================================
def run_detailed_backtest(st_period, st_multiplier, tf_str,
                          spread_width, target_pct,
                          df1m, expiry_dates, expiry_lotmap,
                          BT_START, BT_END, opt_cache, conn):

    df_tf = resample_to_tf(df1m, tf_str)
    tf_minutes = tf_to_minutes(tf_str)

    st_dir = compute_supertrend(
        df_tf['high'].values, df_tf['low'].values, df_tf['close'].values,
        st_period, st_multiplier)

    n_bars = len(df_tf)
    flips = np.full(n_bars, None, dtype=object)
    if n_bars > 1:
        prev = st_dir[:-1]; curr = st_dir[1:]
        flips[1:] = np.where((curr==1)&(prev==-1), 'BULLISH',
                     np.where((curr==-1)&(prev==1), 'BEARISH', None))

    df_tf_idx  = df_tf.index
    df1m_index = df1m.index
    df1m_close = df1m['close'].values

    def get_expiry(d):
        for exp in expiry_dates:
            if exp >= d:
                return exp
        return None

    def get_next_expiry(d):
        found = False
        for exp in expiry_dates:
            if exp >= d:
                if not found:
                    found = True
                    continue
                return exp
        return None

    def load_option(sym):
        if sym in opt_cache:
            return opt_cache[sym]
        df = conn.execute("""
            SELECT timestamp, close FROM market_data
            WHERE symbol=? AND exchange='NFO' AND interval='1m'
            ORDER BY timestamp
        """, [sym]).df()
        if df.empty:
            opt_cache[sym] = pd.DataFrame()
            return opt_cache[sym]
        df['dt'] = (pd.to_datetime(df['timestamp'], unit='s', utc=True)
                      .dt.tz_convert('Asia/Kolkata')
                      .dt.tz_localize(None))
        df = df.set_index('dt').drop(columns=['timestamp'])
        opt_cache[sym] = df
        return df

    def price_at(sym, ts):
        df = load_option(sym)
        if df.empty:
            return 0.0
        idx = df.index.searchsorted(ts, side='right') - 1
        return float(df['close'].iloc[idx]) if idx >= 0 else 0.0

    # Trade state
    in_trade = False
    s_sym = b_sym = s_type = None
    net_credit = entry_ts = t_expiry = None
    t_qty = 0
    s_entry = b_entry = 0.0
    t_spread_w = 0
    t_is_cont = False

    pending_flip = None
    trades = []
    counters = dict(
        total_flips=0, primary_entries=0, continuation_entries=0,
        target_exits=0, reversal_exits=0, eod_exits=0, maxloss_exits=0,
        skipped_no_data=0, skipped_neg_credit=0,
    )

    def _try_enter(bar_ts_, direction_, spread_w, is_cont=False):
        nonlocal in_trade, s_sym, b_sym, s_type, net_credit
        nonlocal entry_ts, t_expiry, t_qty, s_entry, b_entry
        nonlocal t_spread_w, t_is_cont

        bar_date_ = bar_ts_.date()
        if bar_date_ < BT_START or bar_date_ > BT_END:
            return False
        if bar_ts_.time() >= ENTRY_CUTOFF:
            return False

        entry_time_ = bar_ts_ + pd.Timedelta(minutes=tf_minutes)
        # Ensure entry happens early enough for same-day exit
        if entry_time_.time() >= dtime(15, 15):
            return False
        idx_ = df1m_index.searchsorted(entry_time_, side='right') - 1
        if idx_ < 0:
            return False
        spot_ = float(df1m_close[idx_])
        atm_ = int(round(spot_ / STRIKE_INTERVAL) * STRIKE_INTERVAL)

        near_ = get_expiry(bar_date_)
        if near_ is None:
            return False
        exp_ = get_next_expiry(bar_date_) if near_ == bar_date_ else near_
        if exp_ is None:
            return False

        if direction_ == 'BULLISH':
            ss = build_option_symbol(exp_, atm_, 'PE')
            bs = build_option_symbol(exp_, atm_ - spread_w, 'PE')
            st = 'BULL_PUT'
        else:
            ss = build_option_symbol(exp_, atm_, 'CE')
            bs = build_option_symbol(exp_, atm_ + spread_w, 'CE')
            st = 'BEAR_CALL'

        sp = price_at(ss, entry_time_)
        bp = price_at(bs, entry_time_)
        if sp <= 0 or bp <= 0:
            counters['skipped_no_data'] += 1
            return False
        if sp <= bp:
            counters['skipped_neg_credit'] += 1
            return False

        ls = expiry_lotmap.get(exp_, 25)
        in_trade   = True
        s_sym      = ss;  b_sym   = bs
        s_type     = st
        net_credit = sp - bp
        entry_ts   = entry_time_
        t_expiry   = exp_
        t_qty      = LOTS * ls
        s_entry    = sp;  b_entry = bp
        t_spread_w = spread_w
        t_is_cont  = is_cont
        return True

    def _exit_trade(exit_ts_, reason_):
        nonlocal in_trade
        se = price_at(s_sym, exit_ts_)
        be = price_at(b_sym, exit_ts_)
        if se <= 0 or be <= 0:
            for lb in range(1, 6):
                se = price_at(s_sym, exit_ts_ - pd.Timedelta(minutes=lb))
                be = price_at(b_sym, exit_ts_ - pd.Timedelta(minutes=lb))
                if se > 0 and be > 0:
                    break
        if se <= 0 or be <= 0:
            in_trade = False
            return None

        exit_spread  = se - be
        pnl_per_unit = net_credit - exit_spread
        gross_pnl    = pnl_per_unit * t_qty
        charges      = compute_charges(s_entry, b_entry, se, be, t_qty)
        total_pnl    = gross_pnl - charges

        trades.append(dict(
            entry_ts=entry_ts, exit_ts=exit_ts_,
            type=s_type, sell_sym=s_sym, buy_sym=b_sym,
            entry_credit=net_credit, exit_spread=exit_spread,
            sell_entry=s_entry, buy_entry=b_entry,
            sell_exit=se, buy_exit=be,
            qty=t_qty, spread_width=t_spread_w,
            is_continuation=t_is_cont,
            gross_pnl=gross_pnl, charges=charges,
            pnl=total_pnl, exit_reason=reason_,
        ))
        in_trade = False
        return total_pnl

    for i in range(n_bars):
        bar_ts   = df_tf_idx[i]
        bar_date = bar_ts.date()
        bar_t    = bar_ts.time()
        flip     = flips[i]
        curr_st  = int(st_dir[i])

        # A. Intraday mandatory exit
        # Check if this bar ENDS at or after 15:20 (bar_start + tf_minutes >= 15:20)
        bar_end_min = bar_t.hour * 60 + bar_t.minute + tf_minutes
        if in_trade and bar_end_min >= 15 * 60 + 15:
            exit_label = bar_ts.replace(hour=15, minute=20, second=0, microsecond=0)
            _exit_trade(exit_label, 'EOD')
            counters['eod_exits'] += 1
            pending_flip = None
            continue

        # B. In-trade 1m monitoring
        if in_trade:
            scan_start = max(bar_ts, entry_ts)
            bar_end_ts = bar_ts + pd.Timedelta(minutes=tf_minutes)
            lo = df1m_index.searchsorted(scan_start, side='left')
            hi = df1m_index.searchsorted(bar_end_ts, side='left')
            for k in range(lo, hi):
                t1m   = df1m_index[k]
                s_ltp = price_at(s_sym, t1m)
                b_ltp = price_at(b_sym, t1m)
                if s_ltp <= 0 or b_ltp <= 0:
                    continue
                cur_spread = s_ltp - b_ltp

                if cur_spread >= t_spread_w * 0.95:
                    _exit_trade(t1m, 'MaxLoss')
                    counters['maxloss_exits'] += 1
                    break

                if in_trade and (net_credit - cur_spread) >= net_credit * target_pct:
                    _exit_trade(t1m, 'Target')
                    counters['target_exits'] += 1
                    if ENABLE_CONT and curr_st != 0 and t1m.time() < ENTRY_CUTOFF:
                        re_dir = 'BULLISH' if curr_st == 1 else 'BEARISH'
                        if _try_enter(t1m, re_dir, CONT_SPREAD_WIDTH, True):
                            counters['continuation_entries'] += 1
                    break

        # C. SuperTrend flip
        if flip is not None:
            counters['total_flips'] += 1
            if in_trade:
                should_exit = ((s_type == 'BULL_PUT'  and flip == 'BEARISH') or
                               (s_type == 'BEAR_CALL' and flip == 'BULLISH'))
                if should_exit:
                    exit_time = bar_ts + pd.Timedelta(minutes=tf_minutes)
                    _exit_trade(exit_time, f'Reversal({flip})')
                    counters['reversal_exits'] += 1
                    if bar_t < ENTRY_CUTOFF:
                        if _try_enter(bar_ts, flip, spread_width):
                            counters['primary_entries'] += 1
                        else:
                            pending_flip = flip
                    continue
            if not in_trade:
                pending_flip = flip

        # D. Pending entry
        if pending_flip and not in_trade and bar_t >= MARKET_OPEN and bar_t < ENTRY_CUTOFF:
            if _try_enter(bar_ts, pending_flip, spread_width):
                counters['primary_entries'] += 1
                pending_flip = None

    if in_trade and n_bars > 0:
        _exit_trade(df_tf_idx[-1], 'EndOfData')

    return trades, counters


# ============================================================================
# MAIN
# ============================================================================
def main():
    print("=" * 90)
    print("NIFTY Options Selling - INTRADAY Detailed Backtest")
    print("=" * 90)
    print(f"  Configs to test:  {len(CONFIGS)}")
    print(f"  Capital:          Rs {INITIAL_CAPITAL:,}")
    print(f"  Entry Cutoff:     {ENTRY_CUTOFF}")
    print(f"  Intraday Exit:    {INTRADAY_EXIT}")
    print(f"  Continuations:    {'ON' if ENABLE_CONT else 'OFF'}")
    print("=" * 90)

    if not Path(DUCKDB_PATH).exists():
        print(f"ERROR: DuckDB not found at {DUCKDB_PATH}")
        sys.exit(1)

    conn = duckdb.connect(DUCKDB_PATH, read_only=True)

    print("\n[1] Loading expiry metadata...")
    meta = conn.execute("""
        SELECT expiry_date, MIN(lot_size) AS lot_size
        FROM expired_fno_contracts
        WHERE openalgo_symbol LIKE 'NIFTY%'
          AND contract_type IN ('CE', 'PE') AND lot_size IS NOT NULL
        GROUP BY expiry_date ORDER BY expiry_date
    """).df()
    meta['expiry_date'] = pd.to_datetime(meta['expiry_date']).dt.date
    expiry_dates  = meta['expiry_date'].tolist()
    expiry_lotmap = dict(zip(meta['expiry_date'], meta['lot_size']))
    BT_START      = expiry_dates[0]
    BT_END        = expiry_dates[-1]
    print(f"  Expiries: {len(expiry_dates)}  ({BT_START} to {BT_END})")

    print("\n[2] Loading NIFTY 1m data...")
    end_ts = int(datetime.combine(BT_END, datetime.max.time()).timestamp())
    df1m = conn.execute("""
        SELECT timestamp, open, high, low, close, volume
        FROM market_data
        WHERE symbol='NIFTY' AND exchange='NSE_INDEX' AND interval='1m'
          AND timestamp <= ?
        ORDER BY timestamp
    """, [end_ts]).df()
    df1m['dt'] = (pd.to_datetime(df1m['timestamp'], unit='s', utc=True)
                    .dt.tz_convert('Asia/Kolkata')
                    .dt.tz_localize(None))
    df1m = (df1m.set_index('dt').drop(columns=['timestamp'])
                .between_time('09:15', '15:29'))
    print(f"  1m bars: {len(df1m):,}")

    opt_cache = {}
    all_summaries = []

    for cfg_name, tf, period, mult, sw, tp in CONFIGS:
        print(f"\n{'='*90}")
        print(f"  {cfg_name}")
        print(f"  TF={tf} ST({period},{mult}) Spread={sw}pt Target={tp*100:.0f}%")
        print(f"{'='*90}")

        trades, counters = run_detailed_backtest(
            period, mult, tf, sw, tp,
            df1m, expiry_dates, expiry_lotmap,
            BT_START, BT_END, opt_cache, conn)

        if not trades:
            print("  No trades.")
            continue

        df_t = pd.DataFrame(trades)
        df_t['entry_ts'] = pd.to_datetime(df_t['entry_ts'])
        df_t['exit_ts']  = pd.to_datetime(df_t['exit_ts'])
        df_t = df_t.sort_values('exit_ts').reset_index(drop=True)

        df_t['cum_pnl'] = df_t['pnl'].cumsum()
        df_t['equity']  = INITIAL_CAPITAL + df_t['cum_pnl']
        max_eq  = df_t['equity'].cummax()
        dd_pct  = (df_t['equity'] - max_eq) / max_eq * 100
        max_dd  = dd_pct.min()

        wins     = df_t[df_t['pnl'] > 0]
        losses   = df_t[df_t['pnl'] <= 0]
        wr       = len(wins) / len(df_t) * 100
        pf       = (wins['pnl'].sum() / abs(losses['pnl'].sum())
                    if len(losses) > 0 else float('inf'))
        total    = df_t['pnl'].sum()
        roi      = total / INITIAL_CAPITAL * 100
        sharpe   = (df_t['pnl'].mean() / df_t['pnl'].std() * np.sqrt(252)
                    if df_t['pnl'].std() > 0 else 0)

        n_primary = df_t[~df_t['is_continuation']].shape[0]
        n_cont    = df_t[df_t['is_continuation']].shape[0]

        # -- Summary ----------------------------------------------------------
        print(f"\n  PERFORMANCE SUMMARY")
        print(f"  {'─'*50}")
        print(f"  Total Trades:    {len(df_t)} ({n_primary} primary, {n_cont} continuation)")
        print(f"  Win Rate:        {wr:.1f}%")
        print(f"  Profit Factor:   {pf:.2f}")
        print(f"  Total P&L:       Rs {total:,.0f}  ({roi:.1f}% ROI)")
        print(f"  Max Drawdown:    {max_dd:.1f}%")
        print(f"  Sharpe Ratio:    {sharpe:.2f}")
        print(f"  Total Charges:   Rs {df_t['charges'].sum():,.0f}")
        print(f"  Avg P&L/Trade:   Rs {df_t['pnl'].mean():,.0f}")
        if len(wins) > 0:
            print(f"  Avg Win:         Rs {wins['pnl'].mean():,.0f}")
        if len(losses) > 0:
            print(f"  Avg Loss:        Rs {losses['pnl'].mean():,.0f}")
        print(f"  Max Win:         Rs {df_t['pnl'].max():,.0f}")
        print(f"  Max Loss:        Rs {df_t['pnl'].min():,.0f}")
        if len(wins) > 0 and len(losses) > 0:
            rr = abs(wins['pnl'].mean() / losses['pnl'].mean())
            print(f"  Risk:Reward:     1:{rr:.2f}")

        # -- Exit Reasons ------------------------------------------------------
        print(f"\n  EXIT REASONS")
        print(f"  {'─'*50}")
        for r, c in df_t['exit_reason'].value_counts().items():
            sub = df_t[df_t['exit_reason'] == r]
            sub_wr = (sub['pnl'] > 0).mean() * 100
            print(f"    {r:<24} {c:>4} trades | WR={sub_wr:5.1f}% | "
                  f"P&L=Rs {sub['pnl'].sum():>10,.0f}")

        # -- By Spread Type ----------------------------------------------------
        print(f"\n  BY SPREAD TYPE")
        print(f"  {'─'*50}")
        for stype in ['BULL_PUT', 'BEAR_CALL']:
            sub = df_t[df_t['type'] == stype]
            if len(sub) > 0:
                swr = (sub['pnl'] > 0).mean() * 100
                print(f"    {stype:<12} {len(sub):>4} trades | WR={swr:5.1f}% | "
                      f"P&L=Rs {sub['pnl'].sum():>10,.0f}")

        # -- Counters ----------------------------------------------------------
        print(f"\n  SIGNAL COUNTERS")
        print(f"  {'─'*50}")
        print(f"    Total Flips:      {counters['total_flips']}")
        print(f"    Primary Entries:  {counters['primary_entries']}")
        print(f"    Continuation:     {counters['continuation_entries']}")
        print(f"    Target Exits:     {counters['target_exits']}")
        print(f"    Reversal Exits:   {counters['reversal_exits']}")
        print(f"    EOD Exits:        {counters['eod_exits']}")
        print(f"    MaxLoss Exits:    {counters['maxloss_exits']}")
        print(f"    Skipped (data):   {counters['skipped_no_data']}")
        print(f"    Skipped (neg):    {counters['skipped_neg_credit']}")

        # -- Streak Analysis ---------------------------------------------------
        streak_wins = 0; streak_losses = 0
        max_win_streak = 0; max_loss_streak = 0
        for pnl in df_t['pnl']:
            if pnl > 0:
                streak_wins += 1
                streak_losses = 0
                max_win_streak = max(max_win_streak, streak_wins)
            else:
                streak_losses += 1
                streak_wins = 0
                max_loss_streak = max(max_loss_streak, streak_losses)
        print(f"\n  STREAK ANALYSIS")
        print(f"  {'─'*50}")
        print(f"    Max Win Streak:   {max_win_streak}")
        print(f"    Max Loss Streak:  {max_loss_streak}")

        # -- Monthly Breakdown -------------------------------------------------
        df_t['month'] = df_t['exit_ts'].dt.to_period('M')
        monthly = df_t.groupby('month').agg(
            Trades=('pnl', 'count'),
            Wins=('pnl', lambda x: (x > 0).sum()),
            PnL=('pnl', 'sum'),
            AvgPnL=('pnl', 'mean'),
            MaxWin=('pnl', 'max'),
            MaxLoss=('pnl', 'min'),
            Charges=('charges', 'sum'),
        )
        print(f"\n  MONTHLY BREAKDOWN")
        print(f"  {'─'*90}")
        print(f"  {'Month':>7} | {'Trades':>6} {'Wins':>5} {'WR%':>5} | "
              f"{'P&L':>12} {'Cum P&L':>12} | {'AvgPnL':>8} {'MaxWin':>8} {'MaxLoss':>9}")
        print(f"  {'─'*90}")
        cum = 0
        monthly_pnls = []
        for m, row in monthly.iterrows():
            cum += row['PnL']
            mwr = row['Wins'] / row['Trades'] * 100 if row['Trades'] > 0 else 0
            monthly_pnls.append(row['PnL'])
            print(f"  {str(m):>7} | {int(row['Trades']):>6} {int(row['Wins']):>5} {mwr:>4.0f}% | "
                  f"Rs {row['PnL']:>10,.0f} Rs {cum:>10,.0f} | "
                  f"{row['AvgPnL']:>7,.0f} {row['MaxWin']:>7,.0f} {row['MaxLoss']:>8,.0f}")

        # Monthly stats
        mp = np.array(monthly_pnls)
        win_months = (mp > 0).sum()
        loss_months = (mp <= 0).sum()
        print(f"\n  Monthly: {win_months} profitable, {loss_months} losing "
              f"({win_months/(win_months+loss_months)*100:.0f}% win rate)")
        if len(mp) > 0:
            print(f"  Avg Monthly P&L: Rs {mp.mean():,.0f}")
            print(f"  Best Month:      Rs {mp.max():,.0f}")
            print(f"  Worst Month:     Rs {mp.min():,.0f}")

        # -- Day of Week Analysis ----------------------------------------------
        df_t['dow'] = df_t['entry_ts'].dt.day_name()
        dow_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']
        print(f"\n  DAY-OF-WEEK ANALYSIS")
        print(f"  {'─'*70}")
        print(f"  {'Day':>10} | {'Trades':>6} {'WR%':>6} | {'P&L':>12} | {'AvgPnL':>8}")
        print(f"  {'─'*70}")
        for day in dow_order:
            sub = df_t[df_t['dow'] == day]
            if len(sub) > 0:
                dwr = (sub['pnl'] > 0).mean() * 100
                print(f"  {day:>10} | {len(sub):>6} {dwr:>5.1f}% | "
                      f"Rs {sub['pnl'].sum():>10,.0f} | {sub['pnl'].mean():>7,.0f}")

        # -- Entry Hour Analysis -----------------------------------------------
        df_t['entry_hour'] = df_t['entry_ts'].dt.hour
        print(f"\n  ENTRY HOUR ANALYSIS")
        print(f"  {'─'*70}")
        print(f"  {'Hour':>6} | {'Trades':>6} {'WR%':>6} | {'P&L':>12} | {'AvgPnL':>8}")
        print(f"  {'─'*70}")
        for hour in sorted(df_t['entry_hour'].unique()):
            sub = df_t[df_t['entry_hour'] == hour]
            hwr = (sub['pnl'] > 0).mean() * 100
            print(f"  {hour:>5}h | {len(sub):>6} {hwr:>5.1f}% | "
                  f"Rs {sub['pnl'].sum():>10,.0f} | {sub['pnl'].mean():>7,.0f}")

        # -- Quarterly Analysis ------------------------------------------------
        df_t['quarter'] = df_t['exit_ts'].dt.to_period('Q')
        print(f"\n  QUARTERLY ANALYSIS")
        print(f"  {'─'*70}")
        quarterly = df_t.groupby('quarter').agg(
            Trades=('pnl', 'count'),
            PnL=('pnl', 'sum'),
        )
        qcum = 0
        for q, row in quarterly.iterrows():
            qcum += row['PnL']
            print(f"  {str(q):>7} | {int(row['Trades']):>4} trades | "
                  f"P&L=Rs {row['PnL']:>10,.0f} | Cum=Rs {qcum:>10,.0f}")

        # -- Save trade log CSV ------------------------------------------------
        safe_name = cfg_name.split('[')[1].split(']')[0].replace(' ', '_')
        csv_path = _script_dir / f"intraday_trades_{safe_name}.csv"
        df_t.drop(columns=['month', 'dow', 'entry_hour', 'quarter'],
                  errors='ignore').to_csv(csv_path, index=False)
        print(f"\n  Trade log: {csv_path}")

        all_summaries.append(dict(
            config=cfg_name, trades=len(df_t), primary=n_primary, cont=n_cont,
            pnl=total, roi=roi, wr=wr, pf=pf, max_dd=max_dd, sharpe=sharpe,
            charges=df_t['charges'].sum(), win_months=win_months,
            loss_months=loss_months,
        ))

    conn.close()

    # =========================================================================
    # FINAL COMPARISON TABLE
    # =========================================================================
    print(f"\n\n{'='*90}")
    print("FINAL COMPARISON")
    print(f"{'='*90}")
    print(f"  {'Config':<45} {'Trades':>6} {'P&L':>12} {'ROI':>6} "
          f"{'WR%':>6} {'PF':>5} {'DD%':>7} {'Sharpe':>7}")
    print(f"  {'-'*100}")
    for s in all_summaries:
        print(f"  {s['config']:<45} {s['trades']:>6} Rs {s['pnl']:>10,.0f} "
              f"{s['roi']:>5.0f}% {s['wr']:>5.1f}% {s['pf']:>4.2f} "
              f"{s['max_dd']:>6.1f}% {s['sharpe']:>6.2f}")

    # =========================================================================
    # PLOTLY CHARTS
    # =========================================================================
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots

        colors = ['#4d96ff', '#ff6b6b', '#6bcb77', '#ffd93d']

        # Equity + Drawdown chart
        fig = make_subplots(
            rows=3, cols=1, shared_xaxes=True,
            subplot_titles=["Equity Curves", "Drawdown %", "Monthly P&L Comparison"],
            row_heights=[0.45, 0.25, 0.30], vertical_spacing=0.06)

        for idx, (cfg_name, tf, period, mult, sw, tp) in enumerate(CONFIGS):
            safe_name = cfg_name.split('[')[1].split(']')[0].replace(' ', '_')
            csv_path = _script_dir / f"intraday_trades_{safe_name}.csv"
            if not csv_path.exists():
                continue
            tdf = pd.read_csv(csv_path, parse_dates=['entry_ts', 'exit_ts'])
            if tdf.empty:
                continue

            short_name = cfg_name.split('[')[1].split(']')[0]
            cum_pnl = tdf['pnl'].cumsum()
            equity = INITIAL_CAPITAL + cum_pnl
            dd = (equity - equity.cummax()) / equity.cummax() * 100

            fig.add_trace(go.Scatter(
                x=tdf['exit_ts'], y=equity,
                mode='lines', name=short_name,
                line=dict(color=colors[idx], width=2.5)), row=1, col=1)

            fig.add_trace(go.Scatter(
                x=tdf['exit_ts'], y=dd,
                mode='lines', name=f'{short_name} DD',
                line=dict(color=colors[idx], width=1.5),
                showlegend=False), row=2, col=1)

            # Monthly bars
            tdf['month'] = tdf['exit_ts'].dt.to_period('M')
            monthly = tdf.groupby('month')['pnl'].sum()
            fig.add_trace(go.Bar(
                x=[str(m) for m in monthly.index],
                y=monthly.values,
                name=short_name,
                marker_color=colors[idx],
                opacity=0.7,
                showlegend=False), row=3, col=1)

        fig.add_hline(y=INITIAL_CAPITAL, line_dash='dash', line_color='gray',
                      annotation_text='Capital', row=1, col=1)
        fig.add_hline(y=0, line_dash='dash', line_color='gray', row=3, col=1)

        fig.update_layout(
            title="NIFTY Intraday Credit Spread - Top Configs Comparison",
            template="plotly_dark", height=1100, width=1500,
            legend=dict(x=0.01, y=0.99, bgcolor='rgba(0,0,0,0.5)'))
        fig.update_yaxes(title_text="Equity (Rs)", row=1, col=1)
        fig.update_yaxes(title_text="Drawdown %", row=2, col=1)
        fig.update_yaxes(title_text="Monthly P&L (Rs)", row=3, col=1)

        html_path = _script_dir / "intraday_detailed_comparison.html"
        fig.write_html(str(html_path))
        print(f"\n  Chart: {html_path}")
        fig.show()

    except Exception as e:
        print(f"\n  Plot error: {e}")

    print(f"\n{'='*90}")
    print("DONE")
    print(f"{'='*90}")


if __name__ == "__main__":
    main()
