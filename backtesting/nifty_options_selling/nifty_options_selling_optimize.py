#!/usr/bin/env python3
"""
NIFTY Options Selling Strategy - Parameter Optimization
========================================================
Fixed: SuperTrend(15, 5.0) on 5m NIFTY bars

Sweeps:
  - SPREAD_WIDTH:     Credit spread width (points)
  - TARGET_PCT:       Take-profit threshold (% of premium decay)
  - CONTINUATIONS:    Enable/disable continuation trades after target

Outputs: top results table, heatmaps (Plotly), CSV of all results.
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
SPREAD_WIDTHS   = [100, 150, 200, 300, 400, 500, 700, 1000]
TARGET_PCTS     = [0.50, 0.60, 0.70, 0.80, 0.85, 0.90, 0.95]
CONTINUATIONS   = [True, False]

# Fixed parameters
ST_PERIOD       = 15
ST_MULTIPLIER   = 5.0
LOTS            = 5
CONT_SPREAD_WIDTH = 150
STRIKE_INTERVAL = 50
INITIAL_CAPITAL = 400_000
MARKET_CLOSE_H  = 15
MARKET_CLOSE_M  = 30

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


def build_option_symbol(expiry: date, strike: int, opt_type: str) -> str:
    return f"NIFTY{expiry.strftime('%d%b%y').upper()}{int(strike)}{opt_type.upper()}"


def compute_charges(sell_entry, buy_entry, sell_exit, buy_exit, qty):
    brokerage = BROKERAGE_PER_ORDER * 4
    stt       = STT_SELL_PCT * (sell_entry * qty + sell_exit * qty + buy_exit * qty)
    turnover  = (sell_entry + buy_entry + sell_exit + buy_exit) * qty
    txn       = TXN_CHARGE_PCT * turnover
    sebi      = SEBI_PER_CRORE * turnover / 1e7
    gst       = GST_PCT * (brokerage + txn + sebi)
    stamp     = STAMP_BUY_PCT * (buy_entry * qty + sell_exit * qty)
    return brokerage + stt + txn + sebi + gst + stamp


# ============================================================================
# SIMULATION
# ============================================================================
def run_backtest(spread_width, target_pct, enable_cont,
                 st_dir, flips, df5m_idx,
                 df1m_index, df1m_close,
                 expiry_dates, expiry_lotmap, BT_START, BT_END,
                 opt_cache, conn):
    """Run a single backtest with given spread/target/continuation params."""

    n5m = len(df5m_idx)

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

    pending_flip = None
    trades_pnl = []
    trades_charges = []

    MARKET_OPEN  = dtime(9, 15)

    def _try_enter(bar_ts_, direction_, spread_w):
        nonlocal in_trade, s_sym, b_sym, s_type, net_credit
        nonlocal entry_ts, t_expiry, t_qty, s_entry, b_entry, t_spread_w

        bar_date_ = bar_ts_.date()
        if bar_date_ < BT_START or bar_date_ > BT_END:
            return False
        if bar_ts_.time() >= dtime(15, 25):
            return False

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

    def _exit_trade(exit_ts_):
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
        in_trade = False

    # Main loop
    for i in range(n5m):
        bar_ts   = df5m_idx[i]
        bar_date = bar_ts.date()
        bar_t    = bar_ts.time()
        flip     = flips[i]
        curr_st  = int(st_dir[i])

        # A. Expiry exit
        if in_trade and t_expiry is not None and bar_date == t_expiry and bar_t >= dtime(15, 25):
            exit_label = bar_ts.replace(hour=15, minute=25, second=0, microsecond=0)
            _exit_trade(exit_label)
            pending_flip = None
            continue

        # B. In-trade 1m target monitoring
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
                if in_trade and (net_credit - cur_spread) >= net_credit * target_pct:
                    _exit_trade(t1m)
                    # Continuation re-entry
                    if enable_cont and curr_st != 0:
                        re_dir = 'BULLISH' if curr_st == 1 else 'BEARISH'
                        _try_enter(t1m, re_dir, CONT_SPREAD_WIDTH)
                    break

        # C. SuperTrend flip
        if flip is not None:
            if in_trade:
                should_exit = ((s_type == 'BULL_PUT'  and flip == 'BEARISH') or
                               (s_type == 'BEAR_CALL' and flip == 'BULLISH'))
                if should_exit:
                    exit_time = bar_ts + pd.Timedelta(minutes=5)
                    _exit_trade(exit_time)
                    if not _try_enter(bar_ts, flip, spread_width):
                        pending_flip = flip
                    continue

            if not in_trade:
                pending_flip = flip

        # D. Pending flip entry
        if pending_flip and not in_trade and bar_t >= MARKET_OPEN:
            if _try_enter(bar_ts, pending_flip, spread_width):
                pending_flip = None

    # Close open position
    if in_trade and n5m > 0:
        _exit_trade(df5m_idx[-1])

    # Compute metrics
    if not trades_pnl:
        return dict(
            total_pnl=0, win_rate=0, profit_factor=0, max_drawdown=0,
            sharpe=0, trade_count=0, avg_pnl=0, total_charges=0,
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

    return dict(
        total_pnl=pnl_arr.sum(),
        win_rate=wr,
        profit_factor=min(pf, 999.0),
        max_drawdown=max_dd,
        sharpe=sharpe,
        trade_count=len(pnl_arr),
        avg_pnl=pnl_arr.mean(),
        total_charges=sum(trades_charges),
    )


# ============================================================================
# MAIN
# ============================================================================
def main():
    total_combos = len(SPREAD_WIDTHS) * len(TARGET_PCTS) * len(CONTINUATIONS)
    print("=" * 70)
    print("NIFTY Options Selling - Spread/Target/Continuation Optimization")
    print("=" * 70)
    print(f"  Fixed ST:        Period={ST_PERIOD}, Mult={ST_MULTIPLIER}")
    print(f"  Spread widths:   {SPREAD_WIDTHS}")
    print(f"  Target %:        {[f'{t*100:.0f}%' for t in TARGET_PCTS]}")
    print(f"  Continuations:   {CONTINUATIONS}")
    print(f"  Total combos:    {total_combos}")
    print(f"  Fixed: LOTS={LOTS}, CONT_SPREAD={CONT_SPREAD_WIDTH}pt")
    print("=" * 70)

    if not Path(DUCKDB_PATH).exists():
        print(f"ERROR: DuckDB not found at {DUCKDB_PATH}")
        sys.exit(1)

    conn = duckdb.connect(DUCKDB_PATH, read_only=True)

    # -- Load data once ----------------------------------------------------
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

    print("\n[3] Resampling to 5m and computing SuperTrend...")
    df5m = (df1m.resample('5min', closed='left', label='left')
                .agg(open=('open','first'), high=('high','max'),
                     low=('low','min'), close=('close','last'),
                     volume=('volume','sum'))
                .dropna().between_time('09:15', '15:24'))
    print(f"  5m bars: {len(df5m):,}")

    # Compute SuperTrend once (fixed params)
    st_dir = compute_supertrend(
        df5m['high'].values, df5m['low'].values, df5m['close'].values,
        ST_PERIOD, ST_MULTIPLIER)

    # Detect flips once
    n5m = len(df5m)
    flips = np.full(n5m, None, dtype=object)
    if n5m > 1:
        prev = st_dir[:-1]; curr = st_dir[1:]
        flips[1:] = np.where((curr==1)&(prev==-1), 'BULLISH',
                     np.where((curr==-1)&(prev==1), 'BEARISH', None))

    n_flips = (flips != None).sum()
    print(f"  ST flips: {n_flips}")

    df5m_idx    = df5m.index
    df1m_index  = df1m.index
    df1m_close  = df1m['close'].values

    # Shared option cache
    opt_cache: dict[str, pd.DataFrame] = {}

    # -- Run optimization --------------------------------------------------
    print(f"\n[4] Running {total_combos} parameter combinations...")
    results = []

    combos = list(product(SPREAD_WIDTHS, TARGET_PCTS, CONTINUATIONS))

    for sw, tp, cont in tqdm(combos, desc="Optimizing", unit="combo"):
        metrics = run_backtest(
            sw, tp, cont,
            st_dir, flips, df5m_idx,
            df1m_index, df1m_close,
            expiry_dates, expiry_lotmap, BT_START, BT_END,
            opt_cache, conn,
        )
        metrics['spread_width'] = sw
        metrics['target_pct'] = tp
        metrics['continuations'] = cont
        results.append(metrics)

    conn.close()

    print(f"\n  Option symbols cached: {len(opt_cache)}")

    # -- Analyze results ---------------------------------------------------
    df_r = pd.DataFrame(results)

    df_active = df_r[df_r['trade_count'] > 0].copy()
    print(f"\n  Combos with trades: {len(df_active)} / {len(df_r)}")

    if df_active.empty:
        print("\nNo parameter combination produced any trades!")
        return

    # Top 15 by total P&L
    print("\n" + "=" * 70)
    print("TOP 15 BY TOTAL P&L")
    print("=" * 70)
    top_pnl = df_active.nlargest(15, 'total_pnl')
    for _, row in top_pnl.iterrows():
        cont_label = "Cont" if row['continuations'] else "NoCont"
        print(f"  Spread={int(row['spread_width']):>4}pt Target={row['target_pct']*100:>4.0f}% {cont_label:<6} | "
              f"P&L=Rs {row['total_pnl']:>10,.0f} | WR={row['win_rate']:>5.1f}% | "
              f"PF={row['profit_factor']:>5.2f} | DD={row['max_drawdown']:>6.1f}% | "
              f"Sharpe={row['sharpe']:>5.2f} | Trades={int(row['trade_count'])}")

    # Top 10 by Sharpe (min 10 trades)
    df_min = df_active[df_active['trade_count'] >= 10]
    print("\n" + "=" * 70)
    print("TOP 10 BY SHARPE RATIO (min 10 trades)")
    print("=" * 70)
    if len(df_min) > 0:
        top_sharpe = df_min.nlargest(10, 'sharpe')
        for _, row in top_sharpe.iterrows():
            cont_label = "Cont" if row['continuations'] else "NoCont"
            print(f"  Spread={int(row['spread_width']):>4}pt Target={row['target_pct']*100:>4.0f}% {cont_label:<6} | "
                  f"Sharpe={row['sharpe']:>5.2f} | P&L=Rs {row['total_pnl']:>10,.0f} | "
                  f"WR={row['win_rate']:>5.1f}% | PF={row['profit_factor']:>5.2f} | "
                  f"DD={row['max_drawdown']:>6.1f}% | Trades={int(row['trade_count'])}")

    # Best overall
    best = df_active.loc[df_active['total_pnl'].idxmax()]
    roi  = best['total_pnl'] / INITIAL_CAPITAL * 100

    print("\n" + "=" * 70)
    print("BEST PARAMETERS (by Total P&L)")
    print("=" * 70)
    print(f"  SuperTrend:     Period={ST_PERIOD}, Multiplier={ST_MULTIPLIER} (fixed)")
    print(f"  Spread Width:   {int(best['spread_width'])}pt")
    print(f"  Target:         {best['target_pct']*100:.0f}%")
    print(f"  Continuations:  {'Enabled' if best['continuations'] else 'Disabled'}")
    print(f"  Total P&L:      Rs {best['total_pnl']:,.0f}  ({roi:.1f}% ROI)")
    print(f"  Win Rate:       {best['win_rate']:.1f}%")
    print(f"  Profit Factor:  {best['profit_factor']:.2f}")
    print(f"  Max Drawdown:   {best['max_drawdown']:.1f}%")
    print(f"  Sharpe Ratio:   {best['sharpe']:.2f}")
    print(f"  Trade Count:    {int(best['trade_count'])}")
    print(f"  Total Charges:  Rs {best['total_charges']:,.0f}")

    # Compare cont vs no-cont for the best spread+target
    best_sw = int(best['spread_width'])
    best_tp = best['target_pct']
    comp = df_active[(df_active['spread_width'] == best_sw) &
                      (df_active['target_pct'] == best_tp)]
    if len(comp) == 2:
        print(f"\n  Continuation impact at Spread={best_sw}pt Target={best_tp*100:.0f}%:")
        for _, row in comp.iterrows():
            cl = "  Cont ON " if row['continuations'] else "  Cont OFF"
            print(f"    {cl}: P&L=Rs {row['total_pnl']:>10,.0f} | "
                  f"Trades={int(row['trade_count'])} | WR={row['win_rate']:.1f}%")

    # -- Save CSV ----------------------------------------------------------
    out_dir  = Path(__file__).parent
    csv_path = out_dir / "nifty_options_selling_optimization.csv"
    df_r.to_csv(csv_path, index=False)
    print(f"\nAll results CSV: {csv_path}")

    # -- Heatmaps ----------------------------------------------------------
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots

        # Heatmap: Spread x Target for no-cont (cleaner signal)
        for cont_val, cont_name in [(False, "No Continuations"), (True, "With Continuations")]:
            hm = df_r[df_r['continuations'] == cont_val].copy()
            if hm.empty:
                continue

            pnl_pivot = hm.pivot_table(
                values='total_pnl', index='spread_width', columns='target_pct',
                aggfunc='first')
            pnl_pivot = pnl_pivot.sort_index(ascending=False)

            sharpe_pivot = hm.pivot_table(
                values='sharpe', index='spread_width', columns='target_pct',
                aggfunc='first')
            sharpe_pivot = sharpe_pivot.sort_index(ascending=False)

            trades_pivot = hm.pivot_table(
                values='trade_count', index='spread_width', columns='target_pct',
                aggfunc='first')
            trades_pivot = trades_pivot.sort_index(ascending=False)

            fig = make_subplots(
                rows=1, cols=3,
                subplot_titles=["Total P&L", "Sharpe Ratio", "Trade Count"],
                horizontal_spacing=0.08)

            pnl_text = pnl_pivot.map(lambda x: f"Rs {x/1e5:.1f}L" if pd.notna(x) else "")
            fig.add_trace(go.Heatmap(
                z=pnl_pivot.values,
                x=[f"{c*100:.0f}%" for c in pnl_pivot.columns],
                y=[str(r) for r in pnl_pivot.index],
                text=pnl_text.values, texttemplate="%{text}",
                colorscale='RdYlGn', name='P&L',
                colorbar=dict(x=0.28, len=0.8)), row=1, col=1)

            sharpe_text = sharpe_pivot.map(lambda x: f"{x:.2f}" if pd.notna(x) else "")
            fig.add_trace(go.Heatmap(
                z=sharpe_pivot.values,
                x=[f"{c*100:.0f}%" for c in sharpe_pivot.columns],
                y=[str(r) for r in sharpe_pivot.index],
                text=sharpe_text.values, texttemplate="%{text}",
                colorscale='RdYlGn', name='Sharpe',
                colorbar=dict(x=0.63, len=0.8)), row=1, col=2)

            trades_text = trades_pivot.map(lambda x: f"{int(x)}" if pd.notna(x) else "")
            fig.add_trace(go.Heatmap(
                z=trades_pivot.values,
                x=[f"{c*100:.0f}%" for c in trades_pivot.columns],
                y=[str(r) for r in trades_pivot.index],
                text=trades_text.values, texttemplate="%{text}",
                colorscale='Blues', name='Trades',
                colorbar=dict(x=0.99, len=0.8)), row=1, col=3)

            fig.update_layout(
                title=f"ST(15,5.0) Spread x Target — {cont_name}",
                template="plotly_dark", height=600, width=1600,
                showlegend=False)

            for c in range(1, 4):
                fig.update_xaxes(title_text="Target %", row=1, col=c)
                fig.update_yaxes(title_text="Spread Width (pt)", row=1, col=c)

            suffix = "cont" if cont_val else "nocont"
            hm_path = out_dir / f"nifty_options_selling_heatmap_{suffix}.html"
            fig.write_html(str(hm_path))
            print(f"Heatmap ({cont_name}): {hm_path}")
            fig.show()

    except ImportError:
        print("plotly not available -- skipping charts")

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"""
{total_combos} combos tested: {len(SPREAD_WIDTHS)} spreads x {len(TARGET_PCTS)} targets x 2 (cont on/off)

Best: Spread={int(best['spread_width'])}pt, Target={best['target_pct']*100:.0f}%, """
          f"""Cont={'ON' if best['continuations'] else 'OFF'}
P&L: Rs {best['total_pnl']:,.0f} ({roi:.1f}% ROI) | {int(best['trade_count'])} trades
WR: {best['win_rate']:.1f}% | PF: {best['profit_factor']:.2f} | DD: {best['max_drawdown']:.1f}% | Sharpe: {best['sharpe']:.2f}

Update nifty_options_selling_backtest.py with these values for full analysis.
""")


if __name__ == "__main__":
    main()
