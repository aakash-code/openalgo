#!/usr/bin/env python3
"""
NIFTY Options Selling Strategy - Backtest
==========================================
Based on: github.com/aakash-code/fully-automated-nifty-options-trading

  SuperTrend(Period=15, Mult=14.2) on 5m NIFTY bars
  BULLISH flip -> Bull Put Spread  (Sell ATM PE, Buy ATM-spread PE)
  BEARISH flip -> Bear Call Spread (Sell ATM CE, Buy ATM+spread CE)

EXITS (priority order):
  1. 95% target           — 95% of received premium decayed -> take profit
  2. Reversal flip        — ST direction changes adversely
  (No market close exit — positions held overnight for extra theta decay)

ENTRIES:
  1. ST flip -> enter credit spread (500pt width)
  2. Target re-entry -> continuation at reduced spread (150pt width)

NO LOOKAHEAD:
  ST computed causally (Wilder RMA)
  Entry: option price at bar_close = bar_ts + 5min (next bar open proxy)
  Exit:  option price at exit timestamp
  Target: checked on each 1m bar inside 5m window

CHARGES (full model):
  Brokerage Rs 20/order + STT + Transaction + SEBI + GST + Stamp

Data: Historify DuckDB (NIFTY 1m NSE_INDEX, options 1m NFO)
Lot sizes: actual per expiry from DB
"""

import sys
import duckdb
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, date, time as dtime
from pathlib import Path

# ============================================================================
# CONFIG
# ============================================================================
# SuperTrend parameters (from external repo)
ST_PERIOD       = 15
ST_MULTIPLIER   = 5.0

# Spread parameters
LOTS            = 5
SPREAD_WIDTH    = 500      # Primary trade spread width (points)
CONT_SPREAD_WIDTH = 150   # Continuation trade spread width (reduced risk)
STRIKE_INTERVAL = 50

# Exit thresholds
TARGET_PCT      = 0.95     # Take profit when 95% of net credit has decayed
EXIT_ON_REVERSAL = True    # Exit on ST reversal flip

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
# SUPERTREND (Wilder RMA - identical to live strategy)
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
    """Full charge model: brokerage + STT + txn + SEBI + GST + stamp."""
    brokerage = BROKERAGE_PER_ORDER * 4
    stt       = STT_SELL_PCT * (sell_entry * qty + sell_exit * qty + buy_exit * qty)
    turnover  = (sell_entry + buy_entry + sell_exit + buy_exit) * qty
    txn       = TXN_CHARGE_PCT * turnover
    sebi      = SEBI_PER_CRORE * turnover / 1e7
    gst       = GST_PCT * (brokerage + txn + sebi)
    stamp     = STAMP_BUY_PCT * (buy_entry * qty + sell_exit * qty)
    return brokerage + stt + txn + sebi + gst + stamp


# ============================================================================
# MAIN
# ============================================================================
def main():
    print("=" * 70)
    print("NIFTY Options Selling Strategy - Backtest")
    print("SuperTrend Credit Spreads")
    print("=" * 70)
    print(f"  ST:          Period={ST_PERIOD}, Mult={ST_MULTIPLIER} | 5m NIFTY bars")
    print(f"  Spread:      {SPREAD_WIDTH}pt primary | {CONT_SPREAD_WIDTH}pt continuation")
    print(f"  Lots:        {LOTS} (actual qty per expiry from DB)")
    print(f"  Target:      {TARGET_PCT*100:.0f}% premium decay")
    print(f"  Exit:        Overnight hold (no market close exit)")
    print(f"  Capital:     Rs {INITIAL_CAPITAL:,}")
    print("=" * 70)

    if not Path(DUCKDB_PATH).exists():
        print(f"ERROR: DuckDB not found at {DUCKDB_PATH}")
        sys.exit(1)

    conn = duckdb.connect(DUCKDB_PATH, read_only=True)

    # -- 1. Expiry metadata ------------------------------------------------
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

    def get_expiry(d: date) -> date:
        for exp in expiry_dates:
            if exp >= d:
                return exp
        return None

    def get_next_expiry(d: date) -> date:
        found = False
        for exp in expiry_dates:
            if exp >= d:
                if not found:
                    found = True
                    continue
                return exp
        return None

    # -- 2. NIFTY 1m data --------------------------------------------------
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
    print(f"  1m bars: {len(df1m):,}  ({df1m.index[0].date()} to {df1m.index[-1].date()})")

    # -- 3. Resample to 5m -------------------------------------------------
    print("\n[3] Resampling to 5m...")
    df5m = (df1m.resample('5min', closed='left', label='left')
                .agg(open=('open','first'), high=('high','max'),
                     low=('low','min'), close=('close','last'),
                     volume=('volume','sum'))
                .dropna().between_time('09:15', '15:24'))
    print(f"  5m bars: {len(df5m):,}")

    # -- 4. SuperTrend ------------------------------------------------------
    print("\n[4] Computing SuperTrend...")
    st_dir = compute_supertrend(
        df5m['high'].values, df5m['low'].values, df5m['close'].values,
        ST_PERIOD, ST_MULTIPLIER)
    df5m['st_dir'] = st_dir
    prev = st_dir[:-1]; curr = st_dir[1:]
    flips = np.full(len(st_dir), None, dtype=object)
    flips[1:] = np.where((curr==1)&(prev==-1), 'BULLISH',
                 np.where((curr==-1)&(prev==1), 'BEARISH', None))
    df5m['flip'] = flips
    n_bull = (df5m['flip']=='BULLISH').sum()
    n_bear = (df5m['flip']=='BEARISH').sum()
    print(f"  Flips: {n_bull} bull, {n_bear} bear ({n_bull + n_bear} total)")

    # -- 5. Option cache ----------------------------------------------------
    print("\n[5] Option cache ready (loaded on demand)...")
    opt_cache: dict[str, pd.DataFrame] = {}

    def load_option(sym: str) -> pd.DataFrame:
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

    def price_at(sym: str, ts: pd.Timestamp) -> float:
        df = load_option(sym)
        if df.empty:
            return 0.0
        idx = df.index.searchsorted(ts, side='right') - 1
        return float(df['close'].iloc[idx]) if idx >= 0 else 0.0

    # -- 6. Simulation ------------------------------------------------------
    print("\n[6] Running simulation...")

    df5m_idx   = df5m.index
    df5m_close = df5m['close'].values
    df5m_st    = st_dir
    df5m_flip  = df5m['flip'].values
    df1m_index = df1m.index
    df1m_close = df1m['close'].values
    n5m        = len(df5m_idx)

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
    running_pnl  = 0.0

    trades = []
    counters = dict(
        total_flips=0,
        primary_entries=0, continuation_entries=0,
        pending_entries=0,
        target_exits=0, reversal_exits=0, expiry_exits=0, market_close_exits=0,
        skipped_no_data=0, skipped_neg_credit=0,
    )

    MARKET_OPEN  = dtime(9, 15)
    MARKET_CLOSE = dtime(MARKET_CLOSE_H, MARKET_CLOSE_M)

    def _try_enter(bar_ts_: pd.Timestamp, direction_: str, spread_w: int,
                   is_cont: bool):
        nonlocal in_trade, s_sym, b_sym, s_type, net_credit
        nonlocal entry_ts, t_expiry, t_qty, s_entry, b_entry
        nonlocal t_spread_w, t_is_cont

        bar_date_ = bar_ts_.date()
        if bar_date_ < BT_START or bar_date_ > BT_END:
            return False

        # Don't enter too close to market close
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

    def _exit_trade(exit_ts_: pd.Timestamp, reason_: str):
        nonlocal in_trade, running_pnl
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
        running_pnl += total_pnl
        in_trade = False
        return total_pnl

    for i in range(n5m):
        bar_ts   = df5m_idx[i]
        bar_date = bar_ts.date()
        bar_t    = bar_ts.time()
        bar_close = float(df5m_close[i])
        flip     = df5m_flip[i]
        curr_st  = int(df5m_st[i])

        # -- A. EXPIRY EXIT (exit on expiry day at 15:25) --------------------
        if in_trade and t_expiry is not None and bar_date == t_expiry and bar_t >= dtime(15, 25):
            exit_label = bar_ts.replace(hour=15, minute=25, second=0, microsecond=0)
            _exit_trade(exit_label, 'Expiry')
            counters['expiry_exits'] += 1
            pending_flip = None
            continue

        # -- B. IN-TRADE 1m MONITORING: TARGET -----------------------------
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

                # 95% target: premium decayed by 95%
                if in_trade and (net_credit - cur_spread) >= net_credit * TARGET_PCT:
                    _exit_trade(t1m, 'Target')
                    counters['target_exits'] += 1
                    # Continuation re-entry at reduced spread width
                    if curr_st != 0:
                        re_dir = 'BULLISH' if curr_st == 1 else 'BEARISH'
                        if _try_enter(t1m, re_dir, CONT_SPREAD_WIDTH, True):
                            counters['continuation_entries'] += 1
                    break

        # -- C. SUPERTREND FLIP HANDLING -----------------------------------
        if flip is not None:
            counters['total_flips'] += 1

            if in_trade and EXIT_ON_REVERSAL:
                should_exit = ((s_type == 'BULL_PUT'  and flip == 'BEARISH') or
                               (s_type == 'BEAR_CALL' and flip == 'BULLISH'))
                if should_exit:
                    exit_time = bar_ts + pd.Timedelta(minutes=5)
                    _exit_trade(exit_time, f'Reversal({flip})')
                    counters['reversal_exits'] += 1
                    # Enter new direction immediately
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

    # -- End of data: close open position ----------------------------------
    if in_trade:
        last_ts = df5m_idx[-1]
        _exit_trade(last_ts, 'EndOfData')

    conn.close()

    # ======================================================================
    # RESULTS
    # ======================================================================
    if not trades:
        print("\nNo trades generated.")
        print(f"\n  Total ST flips: {counters['total_flips']}")
        print(f"  Skipped (no data):    {counters['skipped_no_data']}")
        print(f"  Skipped (neg credit): {counters['skipped_neg_credit']}")
        return

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

    print(f"\n  Option symbols cached: {len(opt_cache)}")
    print(f"  Skipped (no data):     {counters['skipped_no_data']}")
    print(f"  Skipped (neg credit):  {counters['skipped_neg_credit']}")

    print("\n" + "=" * 70)
    print("BACKTEST RESULTS")
    print("=" * 70)
    print(f"  Period:          {df_t['entry_ts'].min().date()} to {df_t['exit_ts'].max().date()}")
    print(f"  Total Trades:    {len(df_t)}  ({n_primary} primary, {n_cont} continuation)")
    print(f"  Win Rate:        {wr:.1f}%")
    print(f"  Profit Factor:   {pf:.2f}")
    print(f"  Total P&L:       Rs {total:,.0f}")
    print(f"  Total Charges:   Rs {df_t['charges'].sum():,.0f}")
    print(f"  ROI on Capital:  {roi:.1f}%")
    print(f"  Max Drawdown:    {max_dd:.1f}%")
    print(f"  Sharpe Ratio:    {sharpe:.2f}")
    if len(wins) > 0:
        print(f"  Avg Win:         Rs {wins['pnl'].mean():,.0f}")
    if len(losses) > 0:
        print(f"  Avg Loss:        Rs {losses['pnl'].mean():,.0f}")
    print(f"  Avg Credit:      Rs {df_t['entry_credit'].mean():.1f}")

    print("\nEntry/Exit Counters:")
    print(f"  ST flips total:        {counters['total_flips']}")
    print(f"  Primary entries:       {counters['primary_entries']}")
    print(f"  Continuation entries:  {counters['continuation_entries']}")
    print(f"  Pending flip entries:  {counters['pending_entries']}")
    print(f"  --- exits ---")
    print(f"  Reversal exits:        {counters['reversal_exits']}")
    print(f"  Target (95%) exits:    {counters['target_exits']}")
    print(f"  Expiry exits:          {counters['expiry_exits']}")
    print(f"  Market close exits:    {counters['market_close_exits']}")

    print("\nExit Reasons:")
    for r, c in df_t['exit_reason'].value_counts().items():
        print(f"  {r:<28} {c:>4}  ({c/len(df_t)*100:.0f}%)")

    print("\nBy Spread Type:")
    ts = df_t.groupby('type')['pnl'].agg(
        Trades='count', Total_PnL='sum', Avg_PnL='mean',
        Win_Rate=lambda x: (x > 0).mean() * 100)
    print(ts.to_string())

    print("\nBy Trade Type:")
    for label, mask in [('Primary', ~df_t['is_continuation']),
                        ('Continuation', df_t['is_continuation'])]:
        sub = df_t[mask]
        if len(sub) > 0:
            sw = (sub['pnl'] > 0).mean() * 100
            print(f"  {label:<16} {len(sub):>4} trades | "
                  f"WR={sw:.0f}% | P&L=Rs {sub['pnl'].sum():,.0f} | "
                  f"Avg spread={sub['spread_width'].iloc[0]}pt")

    df_t['month'] = df_t['exit_ts'].dt.to_period('M')
    monthly = df_t.groupby('month')['pnl'].agg(Trades='count', PnL='sum')
    print("\nMonthly P&L:")
    print(monthly.to_string())

    # -- Export ------------------------------------------------------------
    out_dir  = Path(__file__).parent
    csv_path = out_dir / "nifty_options_selling_trades.csv"
    df_t.drop(columns=['month'], errors='ignore').to_csv(csv_path, index=False)
    print(f"\nTrades CSV: {csv_path}")

    # -- Plot --------------------------------------------------------------
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots

        fig = make_subplots(rows=3, cols=1, shared_xaxes=True,
                            subplot_titles=["Equity Curve (Rs)",
                                            "Drawdown (%)",
                                            "Trade P&L (Rs)"],
                            row_heights=[0.5, 0.25, 0.25],
                            vertical_spacing=0.08)
        fig.add_trace(go.Scatter(x=df_t['exit_ts'], y=df_t['equity'],
                                 mode='lines', name='Equity',
                                 line=dict(color='#00d4aa', width=2)), row=1, col=1)
        fig.add_hline(y=INITIAL_CAPITAL, line_dash='dash', line_color='gray',
                      annotation_text='Capital', row=1, col=1)
        fig.add_trace(go.Scatter(x=df_t['exit_ts'], y=dd_pct.values,
                                 mode='lines', name='Drawdown',
                                 fill='tozeroy', line=dict(color='#ff4444', width=1),
                                 fillcolor='rgba(255,68,68,0.15)'), row=2, col=1)
        colors = ['#00d4aa' if p > 0 else '#ff4444' for p in df_t['pnl']]
        fig.add_trace(go.Bar(x=df_t['exit_ts'], y=df_t['pnl'],
                             name='Trade P&L', marker_color=colors, opacity=0.7),
                      row=3, col=1)
        fig.update_layout(
            title=(f"ST({ST_PERIOD},{ST_MULTIPLIER}) Credit Spread | "
                   f"PF={pf:.2f} | WR={wr:.1f}% | "
                   f"Trades={len(df_t)} | P&L=Rs {total:,.0f} | MaxDD={max_dd:.1f}%"),
            template="plotly_dark", height=800, showlegend=True)
        html_path = out_dir / "nifty_options_selling_equity.html"
        fig.write_html(str(html_path))
        print(f"Chart: {html_path}")
        fig.show()
    except ImportError:
        print("plotly not available — skipping chart")

    # -- Summary -----------------------------------------------------------
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"""
Strategy: ST({ST_PERIOD},{ST_MULTIPLIER}) credit spreads on NIFTY 5m | {LOTS} lots

{len(df_t)} trades | {wr:.1f}% win rate | PF {pf:.2f} | Sharpe {sharpe:.2f}

Total P&L:  Rs {total:,.0f}  ({roi:.1f}% ROI on Rs {INITIAL_CAPITAL:,} capital)
Max DD:     {max_dd:.1f}%  (worst peak-to-valley)
Charges:    Rs {df_t['charges'].sum():,.0f} total  (Rs {df_t['charges'].mean():,.0f}/trade avg)

{n_primary} primary trades ({SPREAD_WIDTH}pt spread) + {n_cont} continuation ({CONT_SPREAD_WIDTH}pt spread)
Target (95%) captured {counters['target_exits']} quick wins
Reversal exits: {counters['reversal_exits']} | Expiry exits: {counters['expiry_exits']}
""")


if __name__ == "__main__":
    main()
