"""
Wave Strategy Backtester
========================
Backtest the Wave scalping strategy on NIFTY near-month futures
using Historify DuckDB data (Jul 2024 – May 2026).

Strategy logic:
- Simultaneously places LIMIT BUY at (ref - buy_gap) and LIMIT SELL at (ref + sell_gap)
- On fill: update reference price, restart after cool_off_time bars
- Dynamic gap scaling: as net position grows one-sided, gap multipliers increase
  (makes further trades in that direction progressively harder)
- Delta restriction: capped at max_delta_lots net position

Run: uv run python strategies/backtest_wave.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb
import pandas as pd
import numpy as np
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'db', 'historify.duckdb')

# ── Configuration (matches wave.yml defaults) ──────────────────────────────
CONFIG = {
    'buy_gap':          25.0,
    'sell_gap':         25.0,
    'lot_size':         75,
    'buy_quantity':     75,
    'sell_quantity':    75,
    'cool_off_bars':    10,      # 10 minutes cool-off
    'max_net_lots':     10,      # position cap in lots (±)
    'initial_capital':  2_000_000,
    'start_date':       '2024-07-26',
    'end_date':         '2026-05-09',
    # Multiplier scale mirrors the strategy's _generate_multiplier_scale()
    # Key = net_lots (str), Value = [buy_mult, sell_mult]
    'multiplier_scale': {
         '0': [1.0, 1.0],
         '1': [1.3, 1.0],  '2': [1.7, 1.0],  '3': [2.5, 1.0],
         '4': [3.0, 1.0],  '5': [10., 1.0],  '6': [10., 1.0],
         '7': [10., 1.0],  '8': [15., 1.0],  '9': [15., 1.0], '10': [15., 1.0],
        '-1': [1.0, 1.3], '-2': [1.0, 1.7], '-3': [1.0, 2.5],
        '-4': [1.0, 3.0], '-5': [1.0, 10.], '-6': [1.0, 10.],
        '-7': [1.0, 10.], '-8': [1.0, 15.], '-9': [1.0, 15.],'-10': [1.0, 15.],
    },
}

# Near-month futures contracts (symbol → period)
FUTURES_CHAIN = [
    ('NIFTY31OCT24FUT',  '2024-07-26', '2024-10-31'),
    ('NIFTY28NOV24FUT',  '2024-11-01', '2024-11-28'),
    ('NIFTY30JAN25FUT',  '2024-11-29', '2025-01-30'),
    ('NIFTY27FEB25FUT',  '2025-01-31', '2025-02-27'),
    ('NIFTY27MAR25FUT',  '2025-02-28', '2025-03-27'),
    ('NIFTY24APR25FUT',  '2025-03-28', '2025-04-24'),
    ('NIFTY29MAY25FUT',  '2025-04-25', '2025-05-29'),
    ('NIFTY26JUN25FUT',  '2025-05-30', '2025-06-26'),
    ('NIFTY31JUL25FUT',  '2025-06-27', '2025-07-31'),
    ('NIFTY28AUG25FUT',  '2025-08-01', '2025-08-28'),
    ('NIFTY25SEP25FUT',  '2025-08-29', '2025-09-25'),
    ('NIFTY30OCT25FUT',  '2025-09-26', '2025-10-30'),
    ('NIFTY27NOV25FUT',  '2025-10-31', '2025-11-27'),
    ('NIFTY25DEC25FUT',  '2025-11-28', '2025-12-25'),
    ('NIFTY29JAN26FUT',  '2025-12-26', '2026-01-29'),
    ('NIFTY26FEB26FUT',  '2026-01-30', '2026-02-26'),
    ('NIFTY26MAR26FUT',  '2026-02-27', '2026-03-26'),
    ('NIFTY30APR26FUT',  '2026-03-27', '2026-04-30'),
    ('NIFTY28MAY26FUT',  '2026-05-01', '2026-05-09'),
]


# ── Data loader ─────────────────────────────────────────────────────────────

def load_futures(conn) -> pd.DataFrame:
    """Load stitched near-month NIFTY futures 1m data."""
    frames = []
    for sym, sd, ed in FUTURES_CHAIN:
        ts0 = int(datetime.strptime(sd, '%Y-%m-%d').timestamp())
        ts1 = int(datetime.strptime(ed, '%Y-%m-%d').timestamp()) + 86400
        df = conn.execute("""
            SELECT timestamp, open, high, low, close
            FROM market_data
            WHERE exchange='NFO' AND symbol=? AND interval='1m'
              AND timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp
        """, [sym, ts0, ts1]).df()
        if not df.empty:
            df['symbol'] = sym
            frames.append(df)

    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)
    out['dt'] = pd.to_datetime(out['timestamp'], unit='s', utc=True).dt.tz_convert('Asia/Kolkata')
    out['date'] = out['dt'].dt.date
    return out.sort_values('timestamp').reset_index(drop=True)


# ── Multiplier helper ────────────────────────────────────────────────────────

def get_gaps(net_lots: int, cfg: dict) -> tuple[float, float]:
    """Return (buy_gap, sell_gap) after applying the position-based multiplier."""
    scale = cfg['multiplier_scale']
    key = str(max(-10, min(10, net_lots)))
    mult = scale.get(key, [1.0, 1.0])
    return cfg['buy_gap'] * mult[0], cfg['sell_gap'] * mult[1]


# ── Core simulation ──────────────────────────────────────────────────────────

def simulate(cfg: dict) -> pd.DataFrame:
    conn = duckdb.connect(DB_PATH, read_only=True)

    print("Loading NIFTY futures data …")
    df = load_futures(conn)
    conn.close()

    if df.empty:
        print("No futures data found!")
        return pd.DataFrame()

    print(f"  {len(df):,} bars | {df['dt'].iloc[0]:%Y-%m-%d} → {df['dt'].iloc[-1]:%Y-%m-%d}")

    market_open_s  = 9 * 3600 + 15 * 60
    market_close_s = 15 * 3600 + 29 * 60   # square-off before 15:30

    # State
    net_lots       = 0          # positive = net long lots
    buy_ref        = None       # reference price for buy leg
    sell_ref       = None       # reference price for sell leg
    prev_buy_px    = None       # previous wave buy price
    prev_sell_px   = None       # previous wave sell price
    cool_off_cnt   = 0          # bars since last order

    trades = []
    open_longs: list[dict]  = []   # each = {'price': x, 'qty': y}
    open_shorts: list[dict] = []

    print("Running simulation …")

    for i, row in df.iterrows():
        dt = row['dt']
        sec = dt.hour * 3600 + dt.minute * 60

        # Square-off at end of day
        if sec >= market_close_s:
            # Net-square all remaining open positions at close
            mid_px = float(row['close'])
            for pos in open_longs:
                pnl = (mid_px - pos['price']) * pos['qty']
                trades.append(dict(
                    direction='BUY', entry_px=pos['price'], exit_px=mid_px,
                    qty=pos['qty'], entry_dt=pos['dt'], exit_dt=dt,
                    exit_reason='eod', pnl=pnl,
                ))
            for pos in open_shorts:
                pnl = (pos['price'] - mid_px) * pos['qty']
                trades.append(dict(
                    direction='SELL', entry_px=pos['price'], exit_px=mid_px,
                    qty=pos['qty'], entry_dt=pos['dt'], exit_dt=dt,
                    exit_reason='eod', pnl=pnl,
                ))
            open_longs.clear()
            open_shorts.clear()
            net_lots   = 0
            buy_ref    = None
            sell_ref   = None
            prev_buy_px  = None
            prev_sell_px = None
            cool_off_cnt = 0
            continue

        if sec < market_open_s:
            continue

        bar_high = float(row['high'])
        bar_low  = float(row['low'])
        bar_close = float(row['close'])

        # Initialise references on first bar of session
        if buy_ref is None:
            buy_ref  = bar_close
            sell_ref = bar_close
            prev_buy_px  = None
            prev_sell_px = None
            cool_off_cnt = 0

        if cool_off_cnt > 0:
            cool_off_cnt -= 1
            continue

        # ── Compute limit prices ─────────────────────────────────────────
        buy_gap, sell_gap = get_gaps(net_lots, cfg)

        # Buy price: aggressive (lowest possible given history)
        candidate_buy  = bar_close  - buy_gap
        candidate_sell = bar_close  + sell_gap
        if prev_buy_px  is not None:
            candidate_buy  = min(candidate_buy,  prev_buy_px  - buy_gap)
        if prev_sell_px is not None:
            candidate_sell = max(candidate_sell, prev_sell_px + sell_gap)

        # ── Check fills ──────────────────────────────────────────────────
        sell_filled = False
        buy_filled  = False

        # Sell fills when bar_high >= sell_level
        if bar_high >= candidate_sell and abs(net_lots) < cfg['max_net_lots']:
            qty = cfg['sell_quantity']
            open_shorts.append(dict(price=candidate_sell, qty=qty, dt=dt))
            net_lots -= 1
            trades.append(dict(
                direction='SELL', entry_px=candidate_sell, exit_px=None,
                qty=qty, entry_dt=dt, exit_dt=None,
                exit_reason='pending', pnl=None,
            ))
            prev_sell_px = candidate_sell
            sell_ref     = candidate_sell
            sell_filled  = True

        # Buy fills when bar_low <= buy_level
        if bar_low <= candidate_buy and abs(net_lots) < cfg['max_net_lots']:
            qty = cfg['buy_quantity']
            open_longs.append(dict(price=candidate_buy, qty=qty, dt=dt))
            net_lots += 1
            trades.append(dict(
                direction='BUY', entry_px=candidate_buy, exit_px=None,
                qty=qty, entry_dt=dt, exit_dt=None,
                exit_reason='pending', pnl=None,
            ))
            prev_buy_px = candidate_buy
            buy_ref     = candidate_buy
            buy_filled  = True

        # ── Match open longs vs shorts ───────────────────────────────────
        # If we have opposing positions, match and realize P&L (FIFO)
        while open_longs and open_shorts:
            long_pos  = open_longs[0]
            short_pos = open_shorts[0]
            match_qty = min(long_pos['qty'], short_pos['qty'])
            pnl_per_lot = (short_pos['price'] - long_pos['price']) * cfg['lot_size'] / cfg['lot_size']
            # For simplicity, P&L = (sell_price - buy_price) * qty
            pnl = (short_pos['price'] - long_pos['price']) * match_qty

            # Update P&L for already-recorded entries
            for t in reversed(trades):
                if t['direction'] == 'BUY' and t['entry_px'] == long_pos['price'] and t['pnl'] is None:
                    t['pnl']     = (short_pos['price'] - long_pos['price']) * match_qty
                    t['exit_px'] = short_pos['price']
                    t['exit_dt'] = dt
                    t['exit_reason'] = 'matched'
                    break
            for t in reversed(trades):
                if t['direction'] == 'SELL' and t['entry_px'] == short_pos['price'] and t['pnl'] is None:
                    t['pnl']     = (short_pos['price'] - long_pos['price']) * match_qty
                    t['exit_px'] = long_pos['price']
                    t['exit_dt'] = dt
                    t['exit_reason'] = 'matched'
                    break

            long_pos['qty']  -= match_qty
            short_pos['qty'] -= match_qty
            if long_pos['qty'] <= 0:
                open_longs.pop(0)
            if short_pos['qty'] <= 0:
                open_shorts.pop(0)

        if buy_filled or sell_filled:
            cool_off_cnt = cfg['cool_off_bars']

    return pd.DataFrame(trades)


# ── Reporting ────────────────────────────────────────────────────────────────

def report(df: pd.DataFrame, cfg: dict) -> None:
    if df.empty:
        print("No trades to report.")
        return

    closed = df[df['exit_reason'].isin(['matched', 'eod'])].copy()
    closed['pnl'] = pd.to_numeric(closed['pnl'], errors='coerce').fillna(0)
    closed['entry_dt'] = pd.to_datetime(closed['entry_dt'])

    total_pnl   = closed['pnl'].sum()
    win_rate    = (closed['pnl'] > 0).mean() * 100
    avg_pnl     = closed['pnl'].mean()
    best_trade  = closed['pnl'].max()
    worst_trade = closed['pnl'].min()

    cum = closed.sort_values('entry_dt')['pnl'].cumsum()
    drawdown = (cum - cum.cummax()).min()

    buy_trades  = closed[closed['direction'] == 'BUY']
    sell_trades = closed[closed['direction'] == 'SELL']

    closed['month'] = closed['entry_dt'].dt.to_period('M')

    print()
    print("=" * 60)
    print("  WAVE STRATEGY BACKTEST RESULTS (NIFTY Futures)")
    print("=" * 60)
    print(f"  Period         : {cfg['start_date']} → {cfg['end_date']}")
    print(f"  Initial Capital: ₹{cfg['initial_capital']:,.0f}")
    print(f"  Buy Gap        : {cfg['buy_gap']} pts | Sell Gap: {cfg['sell_gap']} pts")
    print()
    print(f"  Total Trades   : {len(closed):,}")
    print(f"  Buy Trades     : {len(buy_trades):,}")
    print(f"  Sell Trades    : {len(sell_trades):,}")
    print()
    print(f"  Total P&L      : ₹{total_pnl:>12,.0f}")
    print(f"  Return         : {total_pnl/cfg['initial_capital']*100:>8.1f}%")
    print(f"  Win Rate       : {win_rate:>8.1f}%")
    print(f"  Avg P&L/trade  : ₹{avg_pnl:>12,.0f}")
    print(f"  Best Trade     : ₹{best_trade:>12,.0f}")
    print(f"  Worst Trade    : ₹{worst_trade:>12,.0f}")
    print(f"  Max Drawdown   : ₹{drawdown:>12,.0f}")
    print()
    print("  Monthly P&L:")
    monthly = closed.groupby('month')['pnl'].agg(['sum', 'count'])
    max_abs = max(abs(monthly['sum'].max()), abs(monthly['sum'].min()), 1)
    for period, row in monthly.iterrows():
        bar_len = int(abs(row['sum']) / max_abs * 20)
        bar = ('█' if row['sum'] >= 0 else '░') * bar_len
        sign = '+' if row['sum'] >= 0 else '-'
        print(f"    {period}  {sign}₹{abs(row['sum']):>10,.0f}  ({int(row['count'])} trades)  {bar}")
    print("=" * 60)

    out_path = os.path.join(os.path.dirname(__file__), 'wave_backtest_results.csv')
    closed.to_csv(out_path, index=False)
    print(f"\n  Detailed trades saved to: {out_path}")


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print("Wave Strategy Backtester (NIFTY Futures Scalping)")
    print("─" * 40)
    df = simulate(CONFIG)
    report(df, CONFIG)
