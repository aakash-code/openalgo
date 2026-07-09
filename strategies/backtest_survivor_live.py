"""
Survivor Strategy Backtester — Live Version
============================================
Backtests the production Survivor strategy (scripts/survivor_tracked_*.py) faithfully
against Historify DuckDB data.

Key differences vs backtest_survivor.py (which has the expiry-roll improvement):
  - pe_quantity=65, ce_quantity=65  (live CONFIG values, not 75)
  - NO expiry-day roll — sells current-expiry options on ALL days including expiry day
  - PE/CE trigger: strictly >  pe_gap  (not >=)
  - Reset: new_ref = current_price ± pe_reset_gap  (not ± pe_gap)
  - pe/ce_reset_flag stays 1 after first trade (never zeroed — matches live code)
  - TradeTracker-style output: credit_received, margin_required, cumulative_margin

Run: uv run python strategies/backtest_survivor_live.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb
import pandas as pd
from datetime import datetime, date, timedelta
import warnings
warnings.filterwarnings('ignore')

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'db', 'historify.duckdb')

# ── Configuration — mirrors live strategy CONFIG ───────────────────────────
CONFIG = {
    'pe_gap':                   20,
    'ce_gap':                   20,
    'pe_symbol_gap':           200,
    'ce_symbol_gap':           200,
    'pe_quantity':              65,   # live uses 65, not 75
    'ce_quantity':              65,
    'min_price_to_sell':        15,
    'sell_multiplier_threshold': 5,
    'pe_reset_gap':             30,
    'ce_reset_gap':             30,
    'lot_size':                 25,
    'start_date':      '2024-10-03',
    'end_date':        '2026-05-09',
}


# ── Symbol helpers ─────────────────────────────────────────────────────────

def option_symbol(expiry: date, strike: int, opt_type: str) -> str:
    return f"NIFTY{expiry.strftime('%d%b%y').upper()}{int(strike)}{opt_type}"


def nearest_strike(spot: float, offset: float, opt_type: str, step: int = 50) -> int:
    target = spot - offset if opt_type == 'PE' else spot + offset
    return int(round(target / step) * step)


# ── Data loaders ───────────────────────────────────────────────────────────

def load_nifty_spot(conn, start_date: str, end_date: str) -> pd.DataFrame:
    ts0 = int(datetime.strptime(start_date, '%Y-%m-%d').timestamp())
    ts1 = int(datetime.strptime(end_date,   '%Y-%m-%d').timestamp()) + 86400
    df = conn.execute("""
        SELECT timestamp, open, high, low, close
        FROM market_data
        WHERE exchange='NSE_INDEX' AND symbol='NIFTY' AND interval='1m'
          AND timestamp >= ? AND timestamp <= ?
        ORDER BY timestamp
    """, [ts0, ts1]).df()
    df['dt']   = pd.to_datetime(df['timestamp'], unit='s', utc=True).dt.tz_convert('Asia/Kolkata')
    df['date'] = df['dt'].dt.date
    return df.set_index('timestamp')


def load_expiries(conn, start_date: str, end_date: str) -> list:
    rows = conn.execute("""
        SELECT DISTINCT expiry_date
        FROM expired_fno_expiries
        WHERE openalgo_symbol = 'NIFTY'
          AND expiry_date >= ? AND expiry_date <= ?
        ORDER BY expiry_date
    """, [start_date, end_date]).fetchall()
    return [r[0] for r in rows]


def load_options_for_expiry(conn, expiry: date, from_date: date | None = None) -> dict:
    """Load 1m close prices for options of a given expiry within [from_date, expiry]."""
    exp_str = expiry.strftime('%d%b%y').upper()
    ts0 = int(datetime.combine(from_date - timedelta(days=2), datetime.min.time()).timestamp()) \
          if from_date else 0
    ts1 = int(datetime.combine(expiry + timedelta(days=1), datetime.min.time()).timestamp())
    rows = conn.execute("""
        SELECT symbol, timestamp, close
        FROM market_data
        WHERE exchange='NFO'
          AND symbol LIKE ?
          AND interval='1m'
          AND timestamp >= ? AND timestamp <= ?
        ORDER BY symbol, timestamp
    """, [f'NIFTY{exp_str}%', ts0, ts1]).fetchall()
    data: dict[str, dict[int, float]] = {}
    for sym, ts, close in rows:
        if sym not in data:
            data[sym] = {}
        data[sym][ts] = close
    return data


def lookup_price(options: dict, symbol: str, timestamp: int, window: int = 3) -> float | None:
    ts_dict = options.get(symbol)
    if ts_dict is None:
        return None
    for delta in range(0, window * 60 + 1, 60):
        for sign in (0, 1, -1):
            v = ts_dict.get(timestamp + sign * delta)
            if v is not None and v > 0:
                return v
    return None


def get_expiry_settlement(options: dict, symbol: str, expiry: date) -> float:
    ts_dict = options.get(symbol)
    if not ts_dict:
        return 0.0
    exp_start = int(datetime.combine(expiry, datetime.min.time()).timestamp())
    exp_end   = exp_start + 86400
    prices = [(ts, p) for ts, p in ts_dict.items() if exp_start <= ts <= exp_end]
    if not prices:
        return 0.0
    return sorted(prices)[-1][1]


# ── Core simulation ────────────────────────────────────────────────────────

def simulate(cfg: dict) -> tuple[pd.DataFrame, dict]:
    conn = duckdb.connect(DB_PATH, read_only=True)

    print("Loading NIFTY spot data …")
    spot = load_nifty_spot(conn, cfg['start_date'], cfg['end_date'])
    print(f"  {len(spot):,} bars | {spot['dt'].iloc[0]:%Y-%m-%d} → {spot['dt'].iloc[-1]:%Y-%m-%d}")

    expiries = load_expiries(conn, cfg['start_date'], cfg['end_date'])
    print(f"  {len(expiries)} weekly expiries | {expiries[0]} → {expiries[-1]}")

    trades          = []
    open_positions: dict[str, dict] = {}
    expiry_stats:   dict[date, dict] = {}

    # Lazy options cache — window-scoped queries for performance
    options_cache: dict[date, dict] = {}
    expiry_window_start: dict[date, date] = {}
    for _ei, _exp in enumerate(expiries):
        _prev = expiries[_ei - 1] if _ei > 0 else None
        expiry_window_start[_exp] = (_prev + timedelta(days=1)) if _prev else _exp

    def get_options(exp: date) -> dict:
        if exp not in options_cache:
            from_dt = expiry_window_start.get(exp, exp)
            options_cache[exp] = load_options_for_expiry(conn, exp, from_dt)
        return options_cache[exp]

    pe_ref        = None
    ce_ref        = None
    pe_reset_flag = 0   # stays 1 once a PE trade has occurred (matches live code)
    ce_reset_flag = 0

    # Running TradeTracker totals (for per-trade records)
    cumulative_margin = 0.0
    trade_no          = 0

    market_open_s  = 9 * 3600 + 15 * 60
    market_close_s = 15 * 3600 + 30 * 60

    for ei, expiry in enumerate(expiries):
        prev_expiry  = expiries[ei - 1] if ei > 0 else None
        period_start = (prev_expiry + timedelta(days=1)) if prev_expiry else expiry

        print(f"\rProcessing expiry {expiry} ({ei+1}/{len(expiries)}) …", end='', flush=True)

        get_options(expiry)  # warm cache for settlement

        expiry_stats[expiry] = {
            'window_start': period_start,
            'window_end':   expiry,
            'trades':       0,
            'premium_in':   0.0,
            'peak_margin':  0.0,
            'pnl':          0.0,
        }

        mask = (spot['date'] > prev_expiry) & (spot['date'] <= expiry) if prev_expiry \
               else (spot['date'] <= expiry)
        period_df = spot[mask]

        if pe_ref is None and not period_df.empty:
            pe_ref = float(period_df['close'].iloc[0])
        if ce_ref is None and not period_df.empty:
            ce_ref = float(period_df['close'].iloc[0])

        # ── Bar-by-bar simulation ─────────────────────────────────────────
        for ts, row in period_df.iterrows():
            dt_bar     = row['dt']
            sec_of_day = dt_bar.hour * 3600 + dt_bar.minute * 60
            if sec_of_day < market_open_s or sec_of_day >= market_close_s:
                continue

            price = float(row['close'])

            # ── PE logic: sell PE when NIFTY UP (live: strictly >) ────────
            pe_diff = price - pe_ref
            if pe_diff > cfg['pe_gap']:
                sell_multiplier = int(pe_diff / cfg['pe_gap'])
                if sell_multiplier > cfg['sell_multiplier_threshold']:
                    # Live code returns/warns but does NOT cap — skip the trade
                    pass
                else:
                    opts     = get_options(expiry)   # NO roll — current expiry always
                    strike   = nearest_strike(price, cfg['pe_symbol_gap'], 'PE')
                    for _ in range(8):
                        sym = option_symbol(expiry, strike, 'PE')
                        if sym in opts:
                            entry_px = lookup_price(opts, sym, int(ts))
                            if entry_px and entry_px >= cfg['min_price_to_sell']:
                                qty = cfg['pe_quantity'] * sell_multiplier
                                # Margin proxy: 14% × spot × qty
                                margin = 0.14 * price * qty
                                cumulative_margin += margin
                                trade_no += 1
                                if sym in open_positions:
                                    ep = open_positions[sym]
                                    total_qty = ep['qty'] + qty
                                    ep['entry_px'] = (ep['entry_px'] * ep['qty'] + entry_px * qty) / total_qty
                                    ep['qty'] = total_qty
                                    ep['credit_received'] += entry_px * qty
                                    ep['margin_used']     += margin
                                else:
                                    open_positions[sym] = dict(
                                        entry_px=entry_px, qty=qty,
                                        expiry=expiry,
                                        opt_type='PE', entry_dt=dt_bar,
                                        credit_received=entry_px * qty,
                                        margin_used=margin,
                                        trade_no=trade_no,
                                    )
                                pe_ref += cfg['pe_gap'] * sell_multiplier
                                pe_reset_flag = 1   # stays 1 forever after first trade
                                expiry_stats[expiry]['trades']     += 1
                                expiry_stats[expiry]['premium_in'] += entry_px * qty
                                break
                        strike -= 50

            # ── CE logic: sell CE when NIFTY DOWN (live: strictly >) ─────
            ce_diff = ce_ref - price
            if ce_diff > cfg['ce_gap']:
                sell_multiplier = int(ce_diff / cfg['ce_gap'])
                if sell_multiplier > cfg['sell_multiplier_threshold']:
                    pass
                else:
                    opts   = get_options(expiry)
                    strike = nearest_strike(price, cfg['ce_symbol_gap'], 'CE')
                    for _ in range(8):
                        sym = option_symbol(expiry, strike, 'CE')
                        if sym in opts:
                            entry_px = lookup_price(opts, sym, int(ts))
                            if entry_px and entry_px >= cfg['min_price_to_sell']:
                                qty = cfg['ce_quantity'] * sell_multiplier
                                margin = 0.14 * price * qty
                                cumulative_margin += margin
                                trade_no += 1
                                if sym in open_positions:
                                    ep = open_positions[sym]
                                    total_qty = ep['qty'] + qty
                                    ep['entry_px'] = (ep['entry_px'] * ep['qty'] + entry_px * qty) / total_qty
                                    ep['qty'] = total_qty
                                    ep['credit_received'] += entry_px * qty
                                    ep['margin_used']     += margin
                                else:
                                    open_positions[sym] = dict(
                                        entry_px=entry_px, qty=qty,
                                        expiry=expiry,
                                        opt_type='CE', entry_dt=dt_bar,
                                        credit_received=entry_px * qty,
                                        margin_used=margin,
                                        trade_no=trade_no,
                                    )
                                ce_ref -= cfg['ce_gap'] * sell_multiplier
                                ce_reset_flag = 1
                                expiry_stats[expiry]['trades']     += 1
                                expiry_stats[expiry]['premium_in'] += entry_px * qty
                                break
                        strike += 50

            # ── Reset logic (live exact: ref = price ± reset_gap, flag stays) ──
            if pe_reset_flag and (pe_ref - price) > cfg['pe_reset_gap']:
                pe_ref = price + cfg['pe_reset_gap']   # live: + pe_reset_gap, not + pe_gap
                # pe_reset_flag stays 1 (live code does NOT reset it)

            if ce_reset_flag and (price - ce_ref) > cfg['ce_reset_gap']:
                ce_ref = price - cfg['ce_reset_gap']   # live: - ce_reset_gap

            # ── Peak margin tracking ──────────────────────────────────────
            if open_positions:
                total_qty      = sum(pos['qty'] for pos in open_positions.values())
                current_margin = 0.14 * price * total_qty
                if current_margin > expiry_stats[expiry]['peak_margin']:
                    expiry_stats[expiry]['peak_margin'] = current_margin

        # ── Settle positions expiring this week ───────────────────────────
        to_settle = [sym for sym, pos in list(open_positions.items())
                     if pos['expiry'] == expiry]
        for sym in to_settle:
            pos     = open_positions.pop(sym)
            exit_px = get_expiry_settlement(get_options(expiry), sym, expiry)
            pnl     = (pos['entry_px'] - exit_px) * pos['qty']
            expiry_stats[expiry]['pnl'] += pnl
            trades.append(dict(
                trade_no=pos['trade_no'],
                symbol=sym,
                opt_type=pos['opt_type'],
                entry_px=pos['entry_px'],
                exit_px=exit_px,
                qty=pos['qty'],
                entry_dt=pos['entry_dt'],
                exit_dt=expiry,
                exit_reason='expiry',
                pnl=pnl,
                credit_received=pos['credit_received'],
                margin_used=pos['margin_used'],
                expiry=expiry,
            ))

    # Any still-open positions at end of data
    for sym, pos in open_positions.items():
        trades.append(dict(
            trade_no=pos['trade_no'],
            symbol=sym,
            opt_type=pos['opt_type'],
            entry_px=pos['entry_px'],
            exit_px=pos['entry_px'],
            qty=pos['qty'],
            entry_dt=pos['entry_dt'],
            exit_dt=None,
            exit_reason='open',
            pnl=0,
            credit_received=pos['credit_received'],
            margin_used=pos['margin_used'],
            expiry=pos['expiry'],
        ))

    conn.close()
    print()
    return pd.DataFrame(trades), expiry_stats


# ── Reporting ──────────────────────────────────────────────────────────────

def report(df: pd.DataFrame, expiry_stats: dict, cfg: dict) -> None:
    closed = df[df['exit_reason'] == 'expiry'].copy()
    if closed.empty:
        print("\nNo closed trades to report.")
        return

    closed['entry_dt'] = pd.to_datetime(closed['entry_dt'])
    closed['month']    = closed['entry_dt'].dt.to_period('M')

    total_pnl   = closed['pnl'].sum()
    win_rate    = (closed['pnl'] > 0).mean() * 100
    avg_pnl     = closed['pnl'].mean()
    best_trade  = closed['pnl'].max()
    worst_trade = closed['pnl'].min()

    cum      = closed.sort_values('entry_dt')['pnl'].cumsum()
    drawdown = (cum - cum.cummax()).min()

    overall_peak  = max((s['peak_margin']  for s in expiry_stats.values()), default=0)
    total_premium = sum(s['premium_in']    for s in expiry_stats.values())
    total_margin  = sum(s.get('peak_margin', 0) for s in expiry_stats.values())

    print()
    print("=" * 72)
    print("  SURVIVOR STRATEGY BACKTEST  (Live Version — No Expiry Roll)")
    print("=" * 72)
    print(f"  Period      : {cfg['start_date']} → {cfg['end_date']}")
    print(f"  Qty         : PE={cfg['pe_quantity']}  CE={cfg['ce_quantity']}  (live values)")
    print(f"  Gaps        : PE={cfg['pe_gap']}  CE={cfg['ce_gap']} pts  |  "
          f"Reset={cfg['pe_reset_gap']} pts")
    print(f"  NO expiry-day roll — sells current expiry on all days")
    print()
    print(f"  Total Trades   : {len(closed):,}")
    print(f"  PE Trades      : {(closed['opt_type']=='PE').sum():,}")
    print(f"  CE Trades      : {(closed['opt_type']=='CE').sum():,}")
    print()
    print(f"  Total P&L      : ₹{total_pnl:>12,.0f}")
    print(f"  Win Rate       : {win_rate:>8.1f}%")
    print(f"  Avg P&L/trade  : ₹{avg_pnl:>12,.0f}")
    print(f"  Best Trade     : ₹{best_trade:>12,.0f}")
    print(f"  Worst Trade    : ₹{worst_trade:>12,.0f}")
    print(f"  Max Drawdown   : ₹{drawdown:>12,.0f}")
    print()
    print(f"  Total Premium Collected : ₹{total_premium:>12,.0f}")
    print(f"  Overall Peak Margin     : ₹{overall_peak:>12,.0f}")
    if overall_peak > 0:
        print(f"  Return on Peak Margin   : {total_pnl/overall_peak*100:>11.1f}%")
    print(f"  Note: Margin ≈ 14% × spot × qty (NRML proxy)")
    print()

    # ── Per-expiry table ──────────────────────────────────────────────────
    print(f"  {'Expiry':<12}  {'Window':<24}  {'Trd':>3}  "
          f"{'Premium':>14}  {'P&L':>12}  {'PeakMargin':>12}  {'ROI':>6}")
    print("  " + "-" * 88)
    for exp, s in sorted(expiry_stats.items()):
        if s['trades'] == 0 and s['pnl'] == 0:
            continue
        roi = s['pnl'] / s['peak_margin'] * 100 if s['peak_margin'] > 0 else 0.0
        sign = '+' if s['pnl'] >= 0 else ''
        print(
            f"  {str(exp):<12}  "
            f"{str(s['window_start'])} → {str(s['window_end'])}  "
            f"{s['trades']:>3}  "
            f"₹{s['premium_in']:>12,.0f}  "
            f"{sign}₹{s['pnl']:>10,.0f}  "
            f"₹{s['peak_margin']:>10,.0f}  "
            f"{roi:>5.1f}%"
        )
    print()

    # ── Monthly breakdown ─────────────────────────────────────────────────
    print("  Monthly P&L:")
    monthly  = closed.groupby('month')['pnl'].agg(['sum', 'count'])
    max_abs  = max(abs(monthly['sum'].max()), abs(monthly['sum'].min()), 1)
    for period, row in monthly.iterrows():
        bar_len = int(abs(row['sum']) / max_abs * 20)
        bar  = ('█' if row['sum'] >= 0 else '░') * bar_len
        sign = '+' if row['sum'] >= 0 else '-'
        print(f"    {period}  {sign}₹{abs(row['sum']):>10,.0f}  ({int(row['count'])} trades)  {bar}")

    print()

    # ── TradeTracker-style per-trade table (top 20 by abs P&L) ───────────
    print("  Per-Trade Detail (sorted by |P&L|, top 30):")
    display = closed.copy()
    display['abs_pnl'] = display['pnl'].abs()
    display = display.sort_values('abs_pnl', ascending=False).head(30)
    hdr = (f"  {'#':>3}  {'Entry':>16}  {'Symbol':<24}  {'Qty':>4}  "
           f"{'Sell':>6}  {'Settle':>6}  {'P&L':>10}  {'Credit':>10}  {'Margin':>10}")
    print(hdr)
    print("  " + "-" * 100)
    for _, t in display.iterrows():
        pnl_s  = f"₹{t['pnl']:>+9,.0f}"
        entry_s = str(t['entry_dt'])[:16] if pd.notna(t['entry_dt']) else '---'
        print(
            f"  {int(t['trade_no']):>3}  "
            f"{entry_s:>16}  "
            f"{t['symbol']:<24}  "
            f"{int(t['qty']):>4}  "
            f"{t['entry_px']:>6.1f}  "
            f"{(t['exit_px'] or 0):>6.1f}  "
            f"{pnl_s:>10}  "
            f"₹{t['credit_received']:>8,.0f}  "
            f"₹{t['margin_used']:>8,.0f}"
        )

    print("=" * 72)

    out_path = os.path.join(os.path.dirname(__file__), 'survivor_live_backtest_results.csv')
    closed.to_csv(out_path, index=False)
    print(f"\n  Detailed trades saved to: {out_path}")


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print("Survivor Strategy Backtester — Live Version (No Expiry Roll)")
    print("─" * 50)
    df, expiry_stats = simulate(CONFIG)
    report(df, expiry_stats, CONFIG)
