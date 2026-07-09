#!/usr/bin/env python3
"""
NIFTY Options Selling - Intraday-Only Multi-Dimension Optimizer
================================================================
2-Phase approach:
  Phase 1: Fast signal-quality scan using spot-based P&L proxy
           (no option DB lookups, runs in seconds per combo)
  Phase 2: Full option-based backtest on Top-N candidates from Phase 1

Sweeps:
  - SuperTrend Period:     [7, 10, 12, 15, 20, 25, 30, 35, 40, 50]
  - SuperTrend Multiplier: [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0]
  - Signal Timeframe:      [3min, 5min, 10min, 15min]

Intraday-only: square off at 15:20, entry cutoff 15:10
"""

import sys
import duckdb
import numpy as np
import pandas as pd
from datetime import datetime, date, time as dtime
from pathlib import Path
from itertools import product
from tqdm import tqdm

# ============================================================================
# PARAMETER GRID
# ============================================================================
ST_PERIODS      = [7, 10, 12, 15, 20, 25, 30, 35, 40, 50]
ST_MULTIPLIERS  = [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0]
TIMEFRAMES      = ['3min', '5min', '10min', '15min']

# Phase 2 refined sweep
SPREAD_WIDTHS   = [300, 500, 700]
TARGET_PCTS     = [0.85, 0.90, 0.95]

# Fixed parameters
LOTS            = 5
LOT_SIZE        = 75   # approximate for fast proxy
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

TOP_N_FOR_PHASE2 = 50  # how many Phase-1 winners to validate with options

_script_dir = Path(__file__).resolve().parent
DUCKDB_PATH = str(_script_dir / ".." / ".." / "db" / "historify.duckdb")


# ============================================================================
# SUPERTREND (Wilder RMA)
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


# ============================================================================
# PHASE 1: FAST SPOT-BASED PROXY BACKTEST
# ============================================================================
def phase1_backtest(st_dir, flips, df_tf_idx, tf_minutes,
                    df1m_index, df1m_close):
    """
    Fast directional P&L proxy using spot price movement.
    For each SuperTrend flip:
      - Enter at next bar's open (bar + tf_minutes)
      - Exit at reversal or EOD (15:20)
      - P&L = direction * (exit_spot - entry_spot) * qty
    No option DB lookups. Returns basic quality metrics.
    """
    n_bars = len(df_tf_idx)
    qty = LOTS * LOT_SIZE  # approximate

    in_trade = False
    direction = 0  # 1 = bullish (sold puts), -1 = bearish (sold calls)
    entry_spot = 0.0
    pending_flip = None
    trade_pnls = []

    for i in range(n_bars):
        bar_ts = df_tf_idx[i]
        bar_t  = bar_ts.time()
        flip   = flips[i]

        # EOD exit: check if this bar ENDS at or after 15:20
        bar_end_min = bar_t.hour * 60 + bar_t.minute + tf_minutes
        if in_trade and bar_end_min >= 15 * 60 + 15:
            # Get spot at 15:20
            exit_ts = bar_ts.replace(hour=15, minute=20, second=0, microsecond=0)
            idx = df1m_index.searchsorted(exit_ts, side='right') - 1
            if idx >= 0:
                exit_spot = float(df1m_close[idx])
                # Credit spread P&L proxy:
                # Bull put spread profits when spot stays above entry, loses when drops
                # Bear call spread profits when spot stays below entry, loses when rises
                # Approximate: spread P&L ~ clamp(direction * (exit - entry) * factor, -max_loss, max_gain)
                move = exit_spot - entry_spot
                raw_pnl = direction * move * qty * 0.01  # scale factor for spread approx
                trade_pnls.append(raw_pnl)
            in_trade = False
            pending_flip = None
            continue

        # Flip handling
        if flip is not None:
            if in_trade:
                # Reversal exit
                should_exit = ((direction == 1 and flip == 'BEARISH') or
                               (direction == -1 and flip == 'BULLISH'))
                if should_exit:
                    exit_ts = bar_ts + pd.Timedelta(minutes=tf_minutes)
                    idx = df1m_index.searchsorted(exit_ts, side='right') - 1
                    if idx >= 0:
                        exit_spot = float(df1m_close[idx])
                        move = exit_spot - entry_spot
                        raw_pnl = direction * move * qty * 0.01
                        trade_pnls.append(raw_pnl)
                    in_trade = False
                    # Try to enter new direction
                    if bar_t < ENTRY_CUTOFF:
                        pending_flip = flip
                    continue

            if not in_trade:
                pending_flip = flip

        # Pending entry
        if pending_flip and not in_trade and bar_t >= MARKET_OPEN and bar_t < ENTRY_CUTOFF:
            entry_ts_dt = bar_ts + pd.Timedelta(minutes=tf_minutes)
            idx = df1m_index.searchsorted(entry_ts_dt, side='right') - 1
            if idx >= 0:
                entry_spot = float(df1m_close[idx])
                direction = 1 if pending_flip == 'BULLISH' else -1
                in_trade = True
                pending_flip = None

    # Close open
    if in_trade and n_bars > 0:
        idx = df1m_index.searchsorted(df_tf_idx[-1], side='right') - 1
        if idx >= 0:
            exit_spot = float(df1m_close[idx])
            move = exit_spot - entry_spot
            trade_pnls.append(direction * move * qty * 0.01)

    if not trade_pnls:
        return dict(total_pnl=0, win_rate=0, sharpe=0, trade_count=0,
                    profit_factor=0, max_drawdown=0)

    arr = np.array(trade_pnls)
    wins = arr[arr > 0]
    losses = arr[arr <= 0]
    wr = len(wins) / len(arr) * 100
    pf = wins.sum() / abs(losses.sum()) if len(losses) > 0 and losses.sum() != 0 else 999.0
    sharpe = arr.mean() / arr.std() * np.sqrt(252) if arr.std() > 0 else 0
    cum = INITIAL_CAPITAL + arr.cumsum()
    max_eq = np.maximum.accumulate(cum)
    dd = ((cum - max_eq) / max_eq * 100).min()

    return dict(
        total_pnl=arr.sum(), win_rate=wr, sharpe=sharpe,
        trade_count=len(arr), profit_factor=min(pf, 999.0),
        max_drawdown=dd,
    )


# ============================================================================
# PHASE 2: FULL OPTION-BASED BACKTEST
# ============================================================================
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


def phase2_backtest(st_dir, flips, df_tf_idx, tf_minutes,
                    spread_width, target_pct,
                    df1m_index, df1m_close,
                    expiry_dates, expiry_lotmap, BT_START, BT_END,
                    opt_cache, conn):
    """Full option-based intraday backtest."""

    n_bars = len(df_tf_idx)

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

    in_trade = False
    s_sym = b_sym = s_type = None
    net_credit = entry_ts = t_expiry = None
    t_qty = 0
    s_entry = b_entry = 0.0
    t_spread_w = 0

    pending_flip = None
    trades_pnl = []
    trades_charges = []
    exit_reasons = []

    def _try_enter(bar_ts_, direction_, spread_w):
        nonlocal in_trade, s_sym, b_sym, s_type, net_credit
        nonlocal entry_ts, t_expiry, t_qty, s_entry, b_entry, t_spread_w

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
            return False
        if sp <= bp:
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
        return True

    def _exit_trade(exit_ts_, reason=''):
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
            return
        exit_spread  = se - be
        pnl_per_unit = net_credit - exit_spread
        gross_pnl    = pnl_per_unit * t_qty
        charges      = compute_charges(s_entry, b_entry, se, be, t_qty)
        total_pnl    = gross_pnl - charges
        trades_pnl.append(total_pnl)
        trades_charges.append(charges)
        exit_reasons.append(reason)
        in_trade = False

    for i in range(n_bars):
        bar_ts   = df_tf_idx[i]
        bar_date = bar_ts.date()
        bar_t    = bar_ts.time()
        flip     = flips[i]
        curr_st  = int(st_dir[i])

        # A. Intraday mandatory exit: bar ENDS at or after 15:20
        bar_end_min = bar_t.hour * 60 + bar_t.minute + tf_minutes
        if in_trade and bar_end_min >= 15 * 60 + 15:
            exit_label = bar_ts.replace(hour=15, minute=20, second=0, microsecond=0)
            _exit_trade(exit_label, 'EOD')
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
                    break

                if in_trade and (net_credit - cur_spread) >= net_credit * target_pct:
                    _exit_trade(t1m, 'Target')
                    if ENABLE_CONT and curr_st != 0 and t1m.time() < ENTRY_CUTOFF:
                        re_dir = 'BULLISH' if curr_st == 1 else 'BEARISH'
                        _try_enter(t1m, re_dir, CONT_SPREAD_WIDTH)
                    break

        # C. SuperTrend flip
        if flip is not None:
            if in_trade:
                should_exit = ((s_type == 'BULL_PUT'  and flip == 'BEARISH') or
                               (s_type == 'BEAR_CALL' and flip == 'BULLISH'))
                if should_exit:
                    exit_time = bar_ts + pd.Timedelta(minutes=tf_minutes)
                    _exit_trade(exit_time, 'Reversal')
                    if bar_t < ENTRY_CUTOFF:
                        if not _try_enter(bar_ts, flip, spread_width):
                            pending_flip = flip
                    continue
            if not in_trade:
                pending_flip = flip

        # D. Pending entry
        if pending_flip and not in_trade and bar_t >= MARKET_OPEN and bar_t < ENTRY_CUTOFF:
            if _try_enter(bar_ts, pending_flip, spread_width):
                pending_flip = None

    if in_trade and n_bars > 0:
        _exit_trade(df_tf_idx[-1], 'EndOfData')

    if not trades_pnl:
        return dict(
            total_pnl=0, win_rate=0, profit_factor=0, max_drawdown=0,
            sharpe=0, trade_count=0, avg_pnl=0, total_charges=0,
            exit_reasons={},
        )

    pnl_arr = np.array(trades_pnl)
    cum_pnl = pnl_arr.cumsum()
    equity  = INITIAL_CAPITAL + cum_pnl
    max_eq  = np.maximum.accumulate(equity)
    dd_pct  = (equity - max_eq) / max_eq * 100
    max_dd  = dd_pct.min()

    wins   = pnl_arr[pnl_arr > 0]
    losses = pnl_arr[pnl_arr <= 0]
    wr     = len(wins) / len(pnl_arr) * 100
    pf     = wins.sum() / abs(losses.sum()) if len(losses) > 0 and losses.sum() != 0 else 999.0
    sharpe = (pnl_arr.mean() / pnl_arr.std() * np.sqrt(252)
              if pnl_arr.std() > 0 else 0)

    # Exit reason counts
    from collections import Counter
    reason_counts = dict(Counter(exit_reasons))

    return dict(
        total_pnl=pnl_arr.sum(),
        win_rate=wr,
        profit_factor=min(pf, 999.0),
        max_drawdown=max_dd,
        sharpe=sharpe,
        trade_count=len(pnl_arr),
        avg_pnl=pnl_arr.mean(),
        total_charges=sum(trades_charges),
        exit_reasons=reason_counts,
    )


# ============================================================================
# RESAMPLE HELPER
# ============================================================================
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
# MAIN
# ============================================================================
def main():
    total_phase1 = len(ST_PERIODS) * len(ST_MULTIPLIERS) * len(TIMEFRAMES)

    print("=" * 80)
    print("NIFTY Options Selling - INTRADAY-ONLY Multi-Dimension Optimizer")
    print("=" * 80)
    print(f"  ST Periods:       {ST_PERIODS}")
    print(f"  ST Multipliers:   {ST_MULTIPLIERS}")
    print(f"  Timeframes:       {TIMEFRAMES}")
    print(f"  Phase 1 combos:   {total_phase1} (fast spot-based proxy)")
    print(f"  Phase 2 top-N:    {TOP_N_FOR_PHASE2} -> x{len(SPREAD_WIDTHS)*len(TARGET_PCTS)} spread/target")
    print(f"  Entry Cutoff:     {ENTRY_CUTOFF}")
    print(f"  Intraday Exit:    {INTRADAY_EXIT}")
    print("=" * 80)

    if not Path(DUCKDB_PATH).exists():
        print(f"ERROR: DuckDB not found at {DUCKDB_PATH}")
        sys.exit(1)

    conn = duckdb.connect(DUCKDB_PATH, read_only=True)

    # -- Load data once --------------------------------------------------------
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

    df1m_index = df1m.index
    df1m_close = df1m['close'].values

    # -- Resample all timeframes -----------------------------------------------
    print("\n[3] Resampling timeframes...")
    tf_data = {}
    for tf in TIMEFRAMES:
        df_tf = resample_to_tf(df1m, tf)
        tf_data[tf] = df_tf
        print(f"  {tf}: {len(df_tf):,} bars")

    # -- Pre-compute SuperTrend ------------------------------------------------
    print("\n[4] Pre-computing SuperTrend signals...")
    st_cache = {}
    st_combos = list(product(TIMEFRAMES, ST_PERIODS, ST_MULTIPLIERS))

    for tf, period, mult in tqdm(st_combos, desc="SuperTrend", unit="st"):
        df_tf = tf_data[tf]
        st_dir = compute_supertrend(
            df_tf['high'].values, df_tf['low'].values, df_tf['close'].values,
            period, mult)
        n = len(df_tf)
        flips = np.full(n, None, dtype=object)
        if n > 1:
            prev = st_dir[:-1]; curr = st_dir[1:]
            flips[1:] = np.where((curr==1)&(prev==-1), 'BULLISH',
                         np.where((curr==-1)&(prev==1), 'BEARISH', None))
        st_cache[(tf, period, mult)] = (st_dir, flips, df_tf.index)

    # =========================================================================
    # PHASE 1: Fast signal-quality scan
    # =========================================================================
    print(f"\n{'='*80}")
    print(f"PHASE 1: Fast Signal-Quality Scan ({total_phase1} combos)")
    print(f"{'='*80}")

    p1_results = []
    for tf, period, mult in tqdm(st_combos, desc="Phase 1", unit="combo"):
        st_dir, flips, df_tf_idx = st_cache[(tf, period, mult)]
        tf_min = tf_to_minutes(tf)

        metrics = phase1_backtest(st_dir, flips, df_tf_idx, tf_min,
                                  df1m_index, df1m_close)
        metrics['timeframe']     = tf
        metrics['st_period']     = period
        metrics['st_multiplier'] = mult
        p1_results.append(metrics)

    df_p1 = pd.DataFrame(p1_results)
    df_p1_active = df_p1[df_p1['trade_count'] >= 10].copy()

    # Rank by composite: sharpe * pf * wr_factor
    df_p1_active['score'] = (df_p1_active['sharpe'] *
                              df_p1_active['profit_factor'].clip(upper=10) *
                              (1 + df_p1_active['win_rate']/100))

    print(f"\n  Active combos (>=10 trades): {len(df_p1_active)}")

    # Show top 30 Phase 1
    print(f"\n  TOP 30 Phase-1 (by Score = Sharpe x PF x WR_factor):")
    print(f"  {'TF':>4} {'ST':>10} | {'Score':>7} {'Sharpe':>7} {'PF':>6} "
          f"{'WR%':>6} {'DD%':>7} {'Trades':>6}")
    print(f"  {'-'*70}")
    top30 = df_p1_active.nlargest(30, 'score')
    for _, row in top30.iterrows():
        st_label = f"({int(row['st_period'])},{row['st_multiplier']:.1f})"
        print(f"  {row['timeframe']:>4} {st_label:>10} | {row['score']:>6.1f} "
              f"{row['sharpe']:>6.2f} {row['profit_factor']:>5.2f} "
              f"{row['win_rate']:>5.1f}% {row['max_drawdown']:>6.1f}% "
              f"{int(row['trade_count']):>6}")

    # Select top-N for Phase 2
    top_candidates = df_p1_active.nlargest(TOP_N_FOR_PHASE2, 'score')
    candidate_params = top_candidates[['timeframe', 'st_period', 'st_multiplier']].values.tolist()

    # =========================================================================
    # PHASE 2: Full option-based backtest on top candidates
    # =========================================================================
    total_phase2 = len(candidate_params) * len(SPREAD_WIDTHS) * len(TARGET_PCTS)
    print(f"\n{'='*80}")
    print(f"PHASE 2: Full Option Backtest on Top {len(candidate_params)} "
          f"x {len(SPREAD_WIDTHS)*len(TARGET_PCTS)} = {total_phase2} combos")
    print(f"{'='*80}")

    opt_cache = {}
    p2_results = []

    p2_combos = [(tf, p, m, sw, tp)
                 for tf, p, m in candidate_params
                 for sw in SPREAD_WIDTHS
                 for tp in TARGET_PCTS]

    for tf, period, mult, sw, tp in tqdm(p2_combos, desc="Phase 2", unit="combo"):
        period = int(period)
        mult = float(mult)
        st_dir, flips, df_tf_idx = st_cache[(tf, period, mult)]
        tf_min = tf_to_minutes(tf)

        metrics = phase2_backtest(
            st_dir, flips, df_tf_idx, tf_min,
            sw, tp,
            df1m_index, df1m_close,
            expiry_dates, expiry_lotmap, BT_START, BT_END,
            opt_cache, conn,
        )
        # Remove non-serializable field for CSV
        exit_reasons = metrics.pop('exit_reasons', {})
        metrics['timeframe']     = tf
        metrics['st_period']     = period
        metrics['st_multiplier'] = mult
        metrics['spread_width']  = sw
        metrics['target_pct']    = tp
        metrics['exit_eod']      = exit_reasons.get('EOD', 0)
        metrics['exit_target']   = exit_reasons.get('Target', 0)
        metrics['exit_reversal'] = exit_reasons.get('Reversal', 0)
        metrics['exit_maxloss']  = exit_reasons.get('MaxLoss', 0)
        p2_results.append(metrics)

    conn.close()

    print(f"\n  Option symbols cached: {len(opt_cache)}")

    df_p2 = pd.DataFrame(p2_results)
    df_p2_active = df_p2[df_p2['trade_count'] > 0].copy()

    # Save CSV
    csv_path = _script_dir / "intraday_optimization_results.csv"
    df_p2.to_csv(csv_path, index=False)
    print(f"\nAll Phase-2 results CSV: {csv_path}")

    # -- TOP 25 BY P&L --------------------------------------------------------
    print(f"\n{'='*80}")
    print("TOP 25 BY TOTAL P&L (Intraday-Only, Full Options)")
    print(f"{'='*80}")
    top_pnl = df_p2_active.nlargest(25, 'total_pnl')
    print(f"  {'TF':>4} {'ST':>10} {'Spread':>6} {'Tgt':>4} | "
          f"{'P&L':>12} {'WR%':>6} {'PF':>6} {'DD%':>7} {'Sharpe':>7} "
          f"{'Trades':>6} {'EOD':>4} {'Tgt':>4} {'Rev':>4}")
    print(f"  {'-'*100}")
    for _, row in top_pnl.iterrows():
        st_label = f"({int(row['st_period'])},{row['st_multiplier']:.1f})"
        print(f"  {row['timeframe']:>4} {st_label:>10} {int(row['spread_width']):>5}pt "
              f"{row['target_pct']*100:>3.0f}% | "
              f"Rs {row['total_pnl']:>10,.0f} {row['win_rate']:>5.1f}% "
              f"{row['profit_factor']:>5.2f} {row['max_drawdown']:>6.1f}% "
              f"{row['sharpe']:>6.2f} "
              f"{int(row['trade_count']):>6} {int(row.get('exit_eod',0)):>4} "
              f"{int(row.get('exit_target',0)):>4} {int(row.get('exit_reversal',0)):>4}")

    # -- TOP 25 BY SHARPE (min 20 trades) --------------------------------------
    df_min = df_p2_active[df_p2_active['trade_count'] >= 20]
    print(f"\n{'='*80}")
    print("TOP 25 BY SHARPE RATIO (min 20 trades)")
    print(f"{'='*80}")
    if len(df_min) > 0:
        top_sharpe = df_min.nlargest(25, 'sharpe')
        print(f"  {'TF':>4} {'ST':>10} {'Spread':>6} {'Tgt':>4} | "
              f"{'Sharpe':>7} {'P&L':>12} {'WR%':>6} {'PF':>6} {'DD%':>7} "
              f"{'Trades':>6}")
        print(f"  {'-'*85}")
        for _, row in top_sharpe.iterrows():
            st_label = f"({int(row['st_period'])},{row['st_multiplier']:.1f})"
            print(f"  {row['timeframe']:>4} {st_label:>10} {int(row['spread_width']):>5}pt "
                  f"{row['target_pct']*100:>3.0f}% | "
                  f"{row['sharpe']:>6.2f} Rs {row['total_pnl']:>10,.0f} "
                  f"{row['win_rate']:>5.1f}% {row['profit_factor']:>5.2f} "
                  f"{row['max_drawdown']:>6.1f}% {int(row['trade_count']):>6}")

    # -- COMPOSITE SCORE -------------------------------------------------------
    if len(df_min) > 0:
        df_min = df_min.copy()
        df_min['score'] = (df_min['sharpe'] * df_min['profit_factor'] *
                           (1 + df_min['win_rate']/100) /
                           (1 + df_min['max_drawdown'].abs()/100))
        print(f"\n{'='*80}")
        print("TOP 25 BY COMPOSITE SCORE (Sharpe x PF x WR / DD)")
        print(f"{'='*80}")
        top_score = df_min.nlargest(25, 'score')
        print(f"  {'TF':>4} {'ST':>10} {'Spread':>6} {'Tgt':>4} | "
              f"{'Score':>7} {'P&L':>12} {'WR%':>6} {'PF':>6} {'DD%':>7} "
              f"{'Sharpe':>7} {'Trades':>6}")
        print(f"  {'-'*95}")
        for _, row in top_score.iterrows():
            st_label = f"({int(row['st_period'])},{row['st_multiplier']:.1f})"
            print(f"  {row['timeframe']:>4} {st_label:>10} {int(row['spread_width']):>5}pt "
                  f"{row['target_pct']*100:>3.0f}% | "
                  f"{row['score']:>6.2f} Rs {row['total_pnl']:>10,.0f} "
                  f"{row['win_rate']:>5.1f}% {row['profit_factor']:>5.2f} "
                  f"{row['max_drawdown']:>6.1f}% {row['sharpe']:>6.2f} "
                  f"{int(row['trade_count']):>6}")

    # -- BEST BY TIMEFRAME -----------------------------------------------------
    print(f"\n{'='*80}")
    print("BEST BY TIMEFRAME (by P&L, min 20 trades)")
    print(f"{'='*80}")
    for tf in TIMEFRAMES:
        tf_sub = df_min[df_min['timeframe'] == tf] if len(df_min) > 0 else pd.DataFrame()
        if tf_sub.empty:
            print(f"  {tf}: No qualifying combos")
            continue
        best = tf_sub.loc[tf_sub['total_pnl'].idxmax()]
        roi = best['total_pnl'] / INITIAL_CAPITAL * 100
        st_label = f"({int(best['st_period'])},{best['st_multiplier']:.1f})"
        print(f"  {tf:>4}: ST{st_label} Spread={int(best['spread_width'])}pt "
              f"Target={best['target_pct']*100:.0f}% | "
              f"P&L=Rs {best['total_pnl']:,.0f} ({roi:.0f}%) | "
              f"WR={best['win_rate']:.1f}% PF={best['profit_factor']:.2f} "
              f"DD={best['max_drawdown']:.1f}% Sharpe={best['sharpe']:.2f} "
              f"Trades={int(best['trade_count'])}")

    # -- OVERALL BEST ----------------------------------------------------------
    best = df_p2_active.loc[df_p2_active['total_pnl'].idxmax()]
    roi = best['total_pnl'] / INITIAL_CAPITAL * 100
    print(f"\n{'='*80}")
    print("OVERALL BEST (by Total P&L)")
    print(f"{'='*80}")
    print(f"  Timeframe:      {best['timeframe']}")
    print(f"  SuperTrend:     Period={int(best['st_period'])}, "
          f"Multiplier={best['st_multiplier']}")
    print(f"  Spread Width:   {int(best['spread_width'])}pt")
    print(f"  Target:         {best['target_pct']*100:.0f}%")
    print(f"  Total P&L:      Rs {best['total_pnl']:,.0f}  ({roi:.1f}% ROI)")
    print(f"  Win Rate:       {best['win_rate']:.1f}%")
    print(f"  Profit Factor:  {best['profit_factor']:.2f}")
    print(f"  Max Drawdown:   {best['max_drawdown']:.1f}%")
    print(f"  Sharpe Ratio:   {best['sharpe']:.2f}")
    print(f"  Trade Count:    {int(best['trade_count'])}")
    print(f"  Total Charges:  Rs {best['total_charges']:,.0f}")
    print(f"  EOD Exits:      {int(best.get('exit_eod',0))}")
    print(f"  Target Exits:   {int(best.get('exit_target',0))}")
    print(f"  Reversal Exits: {int(best.get('exit_reversal',0))}")

    if len(df_min) > 0:
        best_s = df_min.loc[df_min['sharpe'].idxmax()]
        roi_s = best_s['total_pnl'] / INITIAL_CAPITAL * 100
        print(f"\n  BEST BY SHARPE (min 20 trades):")
        print(f"  TF={best_s['timeframe']} ST({int(best_s['st_period'])},"
              f"{best_s['st_multiplier']}) "
              f"Spread={int(best_s['spread_width'])}pt "
              f"Target={best_s['target_pct']*100:.0f}%")
        print(f"  Sharpe={best_s['sharpe']:.2f} P&L=Rs {best_s['total_pnl']:,.0f} "
              f"({roi_s:.0f}%) WR={best_s['win_rate']:.1f}% "
              f"PF={best_s['profit_factor']:.2f} DD={best_s['max_drawdown']:.1f}%")

    # -- Heatmaps --------------------------------------------------------------
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots

        for tf in TIMEFRAMES:
            tf_df = df_p2_active[df_p2_active['timeframe'] == tf].copy()
            if tf_df.empty:
                continue
            best_per_st = (tf_df.groupby(['st_period', 'st_multiplier'])
                           .apply(lambda g: g.loc[g['total_pnl'].idxmax()])
                           .reset_index(drop=True))

            pnl_pivot = best_per_st.pivot_table(
                values='total_pnl', index='st_period', columns='st_multiplier',
                aggfunc='first')
            sharpe_pivot = best_per_st.pivot_table(
                values='sharpe', index='st_period', columns='st_multiplier',
                aggfunc='first')

            fig = make_subplots(rows=1, cols=2,
                                subplot_titles=[f"P&L - {tf}", f"Sharpe - {tf}"],
                                horizontal_spacing=0.1)

            pnl_text = pnl_pivot.map(lambda x: f"{x/1e5:.1f}L" if pd.notna(x) else "")
            fig.add_trace(go.Heatmap(
                z=pnl_pivot.values,
                x=[str(c) for c in pnl_pivot.columns],
                y=[str(r) for r in pnl_pivot.index],
                text=pnl_text.values, texttemplate="%{text}",
                colorscale='RdYlGn', name='P&L',
                colorbar=dict(x=0.42, len=0.8)), row=1, col=1)

            sharpe_text = sharpe_pivot.map(lambda x: f"{x:.1f}" if pd.notna(x) else "")
            fig.add_trace(go.Heatmap(
                z=sharpe_pivot.values,
                x=[str(c) for c in sharpe_pivot.columns],
                y=[str(r) for r in sharpe_pivot.index],
                text=sharpe_text.values, texttemplate="%{text}",
                colorscale='RdYlGn', name='Sharpe',
                colorbar=dict(x=0.99, len=0.8)), row=1, col=2)

            fig.update_layout(
                title=f"Intraday Optimizer: ST Period x Multiplier — {tf}",
                template="plotly_dark", height=600, width=1200,
                showlegend=False)
            for c in range(1, 3):
                fig.update_xaxes(title_text="ST Multiplier", row=1, col=c)
                fig.update_yaxes(title_text="ST Period", row=1, col=c)

            hm_path = _script_dir / f"intraday_heatmap_{tf}.html"
            fig.write_html(str(hm_path))
            print(f"\nHeatmap ({tf}): {hm_path}")

    except ImportError:
        print("\nplotly not available -- skipping charts")

    print(f"\n{'='*80}")
    print("DONE")
    print(f"{'='*80}")


if __name__ == "__main__":
    main()
