#!/usr/bin/env python3
"""
Corrected Comparison - Tests all configs with bug-free engine
=============================================================
Fixes applied vs external backtest:
  1. NO LOOKAHEAD: entry at bar_time + 5min (next bar open proxy)
  2. Max loss exit using actual option spread value, not spot vs strike
  3. Target check on every 1m bar (not i%3 index-based)
  4. Actual per-expiry lot sizes from DB
  5. No duplicate trade recording

Configs tested:
  A. ST(15, 14.2)  - original external config
  B. ST(35, 5.0)   - external optimized-1
  C. ST(80, 3.6)   - external optimized-2
  D. ST(100, 2.0)  - external best (68L claimed)
  E. ST(15, 5.0)   - our optimizer best
"""

import sys
import duckdb
import numpy as np
import pandas as pd
from datetime import datetime, date, time as dtime
from pathlib import Path

# ============================================================================
# CONFIG
# ============================================================================
LOTS            = 5
SPREAD_WIDTH    = 500
CONT_SPREAD_WIDTH = 150
STRIKE_INTERVAL = 50
TARGET_PCT      = 0.95
INITIAL_CAPITAL = 400_000
ENABLE_CONT     = True

# Charges
BROKERAGE_PER_ORDER = 20
STT_SELL_PCT        = 0.001
TXN_CHARGE_PCT      = 0.0003553
SEBI_PER_CRORE      = 10
GST_PCT             = 0.18
STAMP_BUY_PCT       = 0.00003

_script_dir = Path(__file__).resolve().parent
DUCKDB_PATH = str(_script_dir / ".." / ".." / "db" / "historify.duckdb")

CONFIGS = [
    ("ST(15,14.2) [external original]",  15, 14.2),
    ("ST(35,5.0)  [external opt-1]",     35, 5.0),
    ("ST(80,3.6)  [external opt-2]",     80, 3.6),
    ("ST(100,2.0) [external best 68L]", 100, 2.0),
    ("ST(15,5.0)  [our optimizer best]", 15, 5.0),
]


# ============================================================================
# SUPERTREND (Wilder RMA)
# ============================================================================
def compute_supertrend(highs, lows, closes, period, multiplier):
    n      = len(closes)
    highs  = np.asarray(highs,  dtype=float)
    lows   = np.asarray(lows,   dtype=float)
    closes = np.asarray(closes, dtype=float)
    tr = np.empty(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i] - closes[i-1]),
                    abs(lows[i]  - closes[i-1]))
    alpha = 1.0 / period
    atr   = np.zeros(n)
    if n >= period:
        atr[period-1] = tr[:period].mean()
        for i in range(period, n):
            atr[i] = alpha * tr[i] + (1 - alpha) * atr[i-1]
    else:
        atr[:] = tr.mean()
    hl2         = (highs + lows) / 2.0
    upper_raw   = hl2 + multiplier * atr
    lower_raw   = hl2 - multiplier * atr
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


# ============================================================================
# SINGLE CONFIG BACKTEST
# ============================================================================
def run_backtest(st_period, st_multiplier,
                 df5m, df1m, expiry_dates, expiry_lotmap,
                 BT_START, BT_END, opt_cache, conn):
    """Run corrected backtest. Returns (trades_list, counters_dict)."""

    df5m_highs  = df5m['high'].values
    df5m_lows   = df5m['low'].values
    df5m_closes = df5m['close'].values

    st_dir = compute_supertrend(df5m_highs, df5m_lows, df5m_closes,
                                st_period, st_multiplier)

    n5m = len(df5m)
    flips = np.full(n5m, None, dtype=object)
    if n5m > 1:
        prev = st_dir[:-1]; curr = st_dir[1:]
        flips[1:] = np.where((curr==1)&(prev==-1), 'BULLISH',
                     np.where((curr==-1)&(prev==1), 'BEARISH', None))

    df5m_idx   = df5m.index
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
    in_trade    = False
    s_sym = b_sym = s_type = None
    net_credit  = 0.0
    entry_ts    = None
    t_expiry    = None
    t_qty       = 0
    s_entry     = b_entry = 0.0
    t_spread_w  = 0
    t_is_cont   = False

    pending_flip = None
    trades = []

    counters = dict(
        total_flips=0,
        primary_entries=0, continuation_entries=0,
        pending_entries=0,
        target_exits=0, reversal_exits=0, expiry_exits=0,
        maxloss_exits=0,
        skipped_no_data=0, skipped_neg_credit=0,
    )

    MARKET_OPEN  = dtime(9, 15)

    def _try_enter(bar_ts_, direction_, spread_w, is_cont):
        nonlocal in_trade, s_sym, b_sym, s_type, net_credit
        nonlocal entry_ts, t_expiry, t_qty, s_entry, b_entry
        nonlocal t_spread_w, t_is_cont

        bar_date_ = bar_ts_.date()
        if bar_date_ < BT_START or bar_date_ > BT_END:
            return False
        if bar_ts_.time() >= dtime(15, 25):
            return False

        # FIX #1: Entry at NEXT bar (bar_ts + 5min), not signal bar
        entry_time_ = bar_ts_ + pd.Timedelta(minutes=5)
        idx_ = df1m_index.searchsorted(entry_time_, side='right') - 1
        if idx_ < 0:
            return False
        spot_ = float(df1m_close[idx_])
        atm_  = int(round(spot_ / STRIKE_INTERVAL) * STRIKE_INTERVAL)

        near_ = get_expiry(bar_date_)
        if near_ is None:
            return False
        exp_ = get_next_expiry(bar_date_) if near_ == bar_date_ else near_
        if exp_ is None:
            return False

        if direction_ == 'BULLISH':
            ss = build_option_symbol(exp_, atm_,            'PE')
            bs = build_option_symbol(exp_, atm_ - spread_w, 'PE')
            st = 'BULL_PUT'
        else:
            ss = build_option_symbol(exp_, atm_,            'CE')
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
        in_trade    = True
        s_sym       = ss;  b_sym    = bs
        s_type      = st
        net_credit  = sp - bp
        entry_ts    = entry_time_
        t_expiry    = exp_
        t_qty       = LOTS * ls
        s_entry     = sp;  b_entry  = bp
        t_spread_w  = spread_w
        t_is_cont   = is_cont
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

        entry_date = entry_ts.date() if hasattr(entry_ts, 'date') else entry_ts
        exit_date  = exit_ts_.date() if hasattr(exit_ts_, 'date') else exit_ts_
        days_held  = (exit_date - entry_date).days

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
            days_held=days_held,
        ))
        in_trade = False
        return total_pnl

    for i in range(n5m):
        bar_ts   = df5m_idx[i]
        bar_date = bar_ts.date()
        bar_t    = bar_ts.time()
        flip     = flips[i]
        curr_st  = int(st_dir[i])

        # -- A. EXPIRY EXIT (15:25 on expiry day) --------------------------
        if in_trade and t_expiry is not None and bar_date == t_expiry and bar_t >= dtime(15, 25):
            exit_label = bar_ts.replace(hour=15, minute=25, second=0, microsecond=0)
            _exit_trade(exit_label, 'Expiry')
            counters['expiry_exits'] += 1
            pending_flip = None
            continue

        # -- B. IN-TRADE 1m MONITORING: TARGET + MAX LOSS ------------------
        if in_trade:
            scan_start = max(bar_ts, entry_ts)
            bar_end_ts = bar_ts + pd.Timedelta(minutes=5)
            lo = df1m_index.searchsorted(scan_start, side='left')
            hi = df1m_index.searchsorted(bar_end_ts, side='left')
            for k in range(lo, hi):
                t1m   = df1m_index[k]
                s_ltp = price_at(s_sym, t1m)
                b_ltp = price_at(b_sym, t1m)
                if s_ltp <= 0 or b_ltp <= 0:
                    continue
                cur_spread = s_ltp - b_ltp

                # FIX #2: Max loss check using actual spread value
                # Max loss = spread is worth >= spread_width (fully ITM)
                if cur_spread >= t_spread_w * 0.95:
                    _exit_trade(t1m, 'MaxLoss')
                    counters['maxloss_exits'] += 1
                    break

                # Target: premium decayed by TARGET_PCT
                if in_trade and (net_credit - cur_spread) >= net_credit * TARGET_PCT:
                    _exit_trade(t1m, 'Target')
                    counters['target_exits'] += 1
                    if ENABLE_CONT and curr_st != 0:
                        re_dir = 'BULLISH' if curr_st == 1 else 'BEARISH'
                        if _try_enter(t1m, re_dir, CONT_SPREAD_WIDTH, True):
                            counters['continuation_entries'] += 1
                    break

        # -- C. SUPERTREND FLIP HANDLING -----------------------------------
        if flip is not None:
            counters['total_flips'] += 1

            if in_trade:
                should_exit = ((s_type == 'BULL_PUT'  and flip == 'BEARISH') or
                               (s_type == 'BEAR_CALL' and flip == 'BULLISH'))
                if should_exit:
                    exit_time = bar_ts + pd.Timedelta(minutes=5)
                    _exit_trade(exit_time, f'Reversal({flip})')
                    counters['reversal_exits'] += 1
                    if _try_enter(bar_ts, flip, SPREAD_WIDTH, False):
                        counters['primary_entries'] += 1
                    else:
                        pending_flip = flip
                    continue

            if not in_trade:
                pending_flip = flip

        # -- D. PENDING FLIP ENTRY -----------------------------------------
        if pending_flip and not in_trade and bar_t >= MARKET_OPEN:
            if _try_enter(bar_ts, pending_flip, SPREAD_WIDTH, False):
                counters['primary_entries'] += 1
                counters['pending_entries'] += 1
                pending_flip = None

    # Close open position at end
    if in_trade:
        _exit_trade(df5m_idx[-1], 'EndOfData')

    return trades, counters


# ============================================================================
# MAIN
# ============================================================================
def main():
    print("=" * 80)
    print("CORRECTED COMPARISON - All Configs with Bug-Free Engine")
    print("=" * 80)
    print("Fixes applied:")
    print("  1. Entry at bar_time + 5min (NO lookahead)")
    print("  2. Max loss via actual spread value (not spot vs strike)")
    print("  3. Target check on every 1m bar (not i%3)")
    print("  4. Actual per-expiry lot sizes from DB")
    print("  5. No duplicate trades")
    print(f"  Spread: {SPREAD_WIDTH}pt | Target: {TARGET_PCT*100:.0f}%")
    print(f"  Continuations: {'ON' if ENABLE_CONT else 'OFF'}")
    print("=" * 80)

    if not Path(DUCKDB_PATH).exists():
        print(f"ERROR: DuckDB not found at {DUCKDB_PATH}")
        sys.exit(1)

    conn = duckdb.connect(DUCKDB_PATH, read_only=True)

    # Load data once
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

    print("\n[3] Resampling to 5m...")
    df5m = (df1m.resample('5min', closed='left', label='left')
                .agg(open=('open','first'), high=('high','max'),
                     low=('low','min'), close=('close','last'),
                     volume=('volume','sum'))
                .dropna().between_time('09:15', '15:24'))
    print(f"  5m bars: {len(df5m):,}")

    opt_cache = {}
    all_results = []

    for name, period, mult in CONFIGS:
        print(f"\n{'='*80}")
        print(f"  {name}")
        print(f"{'='*80}")

        trades, counters = run_backtest(
            period, mult, df5m, df1m,
            expiry_dates, expiry_lotmap, BT_START, BT_END,
            opt_cache, conn)

        if not trades:
            print("  No trades.")
            all_results.append(dict(config=name, trades=0, pnl=0))
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

        all_results.append(dict(
            config=name, trades=len(df_t), primary=n_primary, cont=n_cont,
            pnl=total, roi=roi, wr=wr, pf=pf, max_dd=max_dd, sharpe=sharpe,
            charges=df_t['charges'].sum(),
            avg_days=df_t['days_held'].mean(),
        ))

        print(f"  Trades:      {len(df_t)} ({n_primary} primary, {n_cont} continuation)")
        print(f"  Win Rate:    {wr:.1f}%")
        print(f"  Profit Fac:  {pf:.2f}")
        print(f"  Total P&L:   Rs {total:,.0f}  ({roi:.1f}% ROI)")
        print(f"  Max DD:      {max_dd:.1f}%")
        print(f"  Sharpe:      {sharpe:.2f}")
        print(f"  Charges:     Rs {df_t['charges'].sum():,.0f}")
        print(f"  Avg Days:    {df_t['days_held'].mean():.1f}")

        print(f"\n  Exit Reasons:")
        for r, c in df_t['exit_reason'].value_counts().items():
            sub = df_t[df_t['exit_reason'] == r]
            sub_wr = (sub['pnl'] > 0).mean() * 100
            print(f"    {r:<24} {c:>4} trades | WR={sub_wr:5.1f}% | "
                  f"P&L=Rs {sub['pnl'].sum():>10,.0f}")

        print(f"\n  By Spread Type:")
        for stype in ['BULL_PUT', 'BEAR_CALL']:
            sub = df_t[df_t['type'] == stype]
            if len(sub) > 0:
                swr = (sub['pnl'] > 0).mean() * 100
                print(f"    {stype:<12} {len(sub):>4} trades | WR={swr:5.1f}% | "
                      f"P&L=Rs {sub['pnl'].sum():>10,.0f}")

        if len(wins) > 0:
            print(f"\n  Avg Win:     Rs {wins['pnl'].mean():,.0f}")
        if len(losses) > 0:
            print(f"  Avg Loss:    Rs {losses['pnl'].mean():,.0f}")
        print(f"  Max Win:     Rs {df_t['pnl'].max():,.0f}")
        print(f"  Max Loss:    Rs {df_t['pnl'].min():,.0f}")

        print(f"\n  Counters: flips={counters['total_flips']} | "
              f"maxloss={counters['maxloss_exits']} | "
              f"skipped_data={counters['skipped_no_data']} | "
              f"skipped_neg={counters['skipped_neg_credit']}")

        # Monthly
        df_t['month'] = df_t['exit_ts'].dt.to_period('M')
        monthly = df_t.groupby('month')['pnl'].agg(Trades='count', PnL='sum')
        print(f"\n  Monthly P&L:")
        cum = 0
        for m, row in monthly.iterrows():
            cum += row['PnL']
            print(f"    {m} | {int(row['Trades']):>3} trades | "
                  f"P&L=Rs {row['PnL']:>10,.0f} | Cum=Rs {cum:>10,.0f}")

        # Save
        csv_path = _script_dir / f"corrected_ST{period}_{mult}.csv"
        df_t.drop(columns=['month'], errors='ignore').to_csv(csv_path, index=False)

    conn.close()

    # ======================================================================
    # FINAL COMPARISON TABLE
    # ======================================================================
    print(f"\n\n{'='*80}")
    print("FINAL COMPARISON - CORRECTED (No Lookahead) vs EXTERNAL (With Lookahead)")
    print(f"{'='*80}")

    # External claimed results for comparison
    external = {
        "ST(15,14.2) [external original]":  3_83_740,
        "ST(35,5.0)  [external opt-1]":    32_20_617,
        "ST(80,3.6)  [external opt-2]":    40_41_103,
        "ST(100,2.0) [external best 68L]": 68_38_166,
    }

    print(f"\n  {'Config':<36} {'Trades':>6} {'WR%':>6} {'PF':>6} "
          f"{'Corrected P&L':>14} {'External P&L':>14} {'Diff':>10} {'DD':>7} {'Sharpe':>7}")
    print(f"  {'-'*110}")
    for r in all_results:
        ext_pnl = external.get(r['config'], 0)
        diff = r['pnl'] - ext_pnl if ext_pnl > 0 else 0
        ext_str = f"Rs {ext_pnl:>10,.0f}" if ext_pnl > 0 else "    N/A"
        diff_str = f"{diff:>+10,.0f}" if ext_pnl > 0 else "    N/A"
        print(f"  {r['config']:<36} {r['trades']:>6} {r.get('wr',0):>5.1f}% "
              f"{r.get('pf',0):>5.2f} Rs {r['pnl']:>10,.0f} {ext_str} {diff_str} "
              f"{r.get('max_dd',0):>6.1f}% {r.get('sharpe',0):>6.2f}")

    print(f"\n  Note: Difference = impact of fixing lookahead bias (entry at next bar)")
    print(f"  External results used signal-bar prices; corrected uses bar+5min prices")

    # Plot
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots

        fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                            subplot_titles=["Equity Curves (Corrected)", "Drawdown"],
                            row_heights=[0.7, 0.3], vertical_spacing=0.08)

        for r in all_results:
            if r['trades'] == 0:
                continue
            csv_path = _script_dir / f"corrected_ST{r['config'].split('(')[1].split(')')[0].replace(',','_').replace(' ','')}.csv"
            # Just use the config name to find right file
        colors = ['#ff6b6b', '#ffd93d', '#6bcb77', '#4d96ff', '#00d4aa']
        for idx, (name, period, mult) in enumerate(CONFIGS):
            csv_path = _script_dir / f"corrected_ST{period}_{mult}.csv"
            if csv_path.exists():
                tdf = pd.read_csv(csv_path, parse_dates=['entry_ts', 'exit_ts'])
                if len(tdf) > 0:
                    cum_pnl = tdf['pnl'].cumsum()
                    equity = INITIAL_CAPITAL + cum_pnl
                    dd = (equity - equity.cummax()) / equity.cummax() * 100
                    fig.add_trace(go.Scatter(
                        x=tdf['exit_ts'], y=equity,
                        mode='lines', name=name,
                        line=dict(color=colors[idx], width=2)), row=1, col=1)
                    fig.add_trace(go.Scatter(
                        x=tdf['exit_ts'], y=dd,
                        mode='lines', name=f'{name} DD',
                        line=dict(color=colors[idx], width=1),
                        showlegend=False), row=2, col=1)

        fig.add_hline(y=INITIAL_CAPITAL, line_dash='dash', line_color='gray',
                      annotation_text='Capital', row=1, col=1)
        fig.update_layout(
            title="Corrected Comparison - All Configs (No Lookahead Bias)",
            template="plotly_dark", height=800, width=1400)
        html_path = _script_dir / "corrected_comparison.html"
        fig.write_html(str(html_path))
        print(f"\n  Chart: {html_path}")
        fig.show()
    except Exception as e:
        print(f"  Plot error: {e}")


if __name__ == "__main__":
    main()
