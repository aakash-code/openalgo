"""
Survivor Options Strategy Backtester
=====================================
Backtest the Survivor strategy (NIFTY weekly options selling) using
Historify DuckDB data (Oct 2024 - May 2026).

Strategy logic:
- PE sell: when NIFTY rises >= pe_gap from pe reference → sell PE at (spot - pe_symbol_gap) strike
- CE sell: when NIFTY falls >= ce_gap from ce reference → sell CE at (spot + ce_symbol_gap) strike
- Multiplier scales with magnitude of move (capped at sell_multiplier_threshold)
- Expiry day: do NOT sell current-expiry options (gamma risk) — roll to next expiry instead
- Exit: hold all shorts to their weekly expiry (settled at last traded price on expiry day)
- Reset: reference resets when market moves back past reset_gap
- No capital cap — margin usage is tracked and reported per expiry window

Run: uv run python strategies/backtest_survivor.py
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

# ── Configuration (matches survivor.yml defaults) ──────────────────────────
CONFIG = {
    'pe_gap':                   20,
    'ce_gap':                   20,
    'pe_symbol_gap':           200,
    'ce_symbol_gap':           200,
    'pe_quantity':              75,
    'ce_quantity':              75,
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
    """Build OpenAlgo option symbol e.g. NIFTY03OCT2423050PE"""
    return f"NIFTY{expiry.strftime('%d%b%y').upper()}{int(strike)}{opt_type}"


def nearest_strike(spot: float, offset: float, opt_type: str, step: int = 50) -> int:
    """Compute ATM±offset strike rounded to step."""
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
    df['dt'] = pd.to_datetime(df['timestamp'], unit='s', utc=True).dt.tz_convert('Asia/Kolkata')
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
    """
    Load 1m close prices for options of a given expiry within [from_date, expiry].
    Restricting to the trading window cuts query size from millions to ~100K rows.
    Returns dict: symbol → {timestamp → close_price}
    """
    exp_str = expiry.strftime('%d%b%y').upper()
    # Load from (from_date - 1 day) to capture options pricing on day before window opens
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


def get_trade_expiry(bar_date: date, expiries: list) -> date | None:
    """
    Return which expiry to sell options for on bar_date.
    - If bar_date IS an expiry (gamma risk!): return the NEXT expiry.
    - Otherwise: return the nearest future expiry (>= bar_date).
    - Returns None when bar_date equals the last expiry (no next expiry exists).
    """
    for i, exp in enumerate(expiries):
        if exp >= bar_date:
            if exp == bar_date:
                return expiries[i + 1] if i + 1 < len(expiries) else None
            return exp
    return None


def lookup_price(options: dict, symbol: str, timestamp: int,
                 window: int = 3) -> float | None:
    """Look up option close price, searching ±window minutes."""
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
    """Last traded price on expiry day (or 0 if not found)."""
    ts_dict = options.get(symbol)
    if not ts_dict:
        return 0.0
    exp_start = int(datetime.combine(expiry, datetime.min.time()).timestamp())
    exp_end   = exp_start + 86400
    prices = [(ts, p) for ts, p in ts_dict.items()
              if exp_start <= ts <= exp_end]
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

    trades = []
    open_positions: dict[str, dict] = {}
    expiry_stats:   dict[date, dict] = {}

    # Lazy options cache — stores (expiry → options dict)
    # Each load is window-scoped so queries stay fast (~100K rows vs millions)
    options_cache: dict[date, dict] = {}
    # Map expiry → window start, so lazy loads know the date range
    expiry_window_start: dict[date, date] = {}
    for _ei, _exp in enumerate(expiries):
        _prev = expiries[_ei - 1] if _ei > 0 else None
        expiry_window_start[_exp] = (_prev + timedelta(days=1)) if _prev else _exp

    def get_options(exp: date) -> dict:
        if exp not in options_cache:
            from_dt = expiry_window_start.get(exp, exp)
            options_cache[exp] = load_options_for_expiry(conn, exp, from_dt)
        return options_cache[exp]

    pe_ref = None
    ce_ref = None
    pe_reset_flag = 0
    ce_reset_flag = 0

    market_open_s  = 9 * 3600 + 15 * 60
    market_close_s = 15 * 3600 + 30 * 60

    for ei, expiry in enumerate(expiries):
        prev_expiry  = expiries[ei - 1] if ei > 0 else None
        period_start = (prev_expiry + timedelta(days=1)) if prev_expiry else expiry

        print(f"\rProcessing expiry {expiry} ({ei+1}/{len(expiries)}) …", end='', flush=True)

        # Warm cache for current expiry (needed for settlement)
        get_options(expiry)

        # Initialise per-window stats
        expiry_stats[expiry] = {
            'window_start': period_start,
            'window_end':   expiry,
            'trades':       0,
            'premium_in':   0.0,
            'peak_margin':  0.0,
            'pnl':          0.0,
        }

        # Bars for this expiry window
        mask = (spot['date'] > prev_expiry) & (spot['date'] <= expiry) if prev_expiry \
               else (spot['date'] <= expiry)
        period_df = spot[mask]

        # Initialise references at first bar of first window
        if pe_ref is None and not period_df.empty:
            pe_ref = float(period_df['close'].iloc[0])
        if ce_ref is None and not period_df.empty:
            ce_ref = float(period_df['close'].iloc[0])

        # ── Bar-by-bar simulation ───────────────────────────────────────────
        for ts, row in period_df.iterrows():
            dt_bar = row['dt']
            sec_of_day = dt_bar.hour * 3600 + dt_bar.minute * 60
            if sec_of_day < market_open_s or sec_of_day >= market_close_s:
                continue

            price      = float(row['close'])
            bar_date   = row['date']

            # Which expiry to sell into for this bar?
            # On expiry day → next expiry (avoid gamma); otherwise → current
            trade_expiry = get_trade_expiry(bar_date, expiries)

            # ── PE logic (sell when NIFTY UP) ─────────────────────────────
            pe_diff = price - pe_ref
            if pe_diff >= cfg['pe_gap'] and trade_expiry is not None:
                mult = int(pe_diff / cfg['pe_gap'])
                if mult <= cfg['sell_multiplier_threshold']:
                    opts_trade     = get_options(trade_expiry)
                    avail_trade    = set(opts_trade.keys())
                    strike = nearest_strike(price, cfg['pe_symbol_gap'], 'PE')
                    for _ in range(8):
                        sym = option_symbol(trade_expiry, strike, 'PE')
                        if sym in avail_trade:
                            entry_px = lookup_price(opts_trade, sym, int(ts))
                            if entry_px and entry_px >= cfg['min_price_to_sell']:
                                qty = cfg['pe_quantity'] * mult
                                if sym in open_positions:
                                    ep = open_positions[sym]
                                    total_qty = ep['qty'] + qty
                                    ep['entry_px'] = (ep['entry_px'] * ep['qty'] + entry_px * qty) / total_qty
                                    ep['qty'] = total_qty
                                else:
                                    open_positions[sym] = dict(
                                        entry_px=entry_px, qty=qty,
                                        expiry=trade_expiry, window_expiry=expiry,
                                        opt_type='PE', entry_dt=dt_bar,
                                    )
                                pe_ref = pe_ref + cfg['pe_gap'] * mult
                                pe_reset_flag = 1
                                expiry_stats[expiry]['trades']     += 1
                                expiry_stats[expiry]['premium_in'] += entry_px * qty
                                break
                        strike -= 50

            # ── CE logic (sell when NIFTY DOWN) ───────────────────────────
            ce_diff = ce_ref - price
            if ce_diff >= cfg['ce_gap'] and trade_expiry is not None:
                mult = int(ce_diff / cfg['ce_gap'])
                if mult <= cfg['sell_multiplier_threshold']:
                    opts_trade  = get_options(trade_expiry)
                    avail_trade = set(opts_trade.keys())
                    strike = nearest_strike(price, cfg['ce_symbol_gap'], 'CE')
                    for _ in range(8):
                        sym = option_symbol(trade_expiry, strike, 'CE')
                        if sym in avail_trade:
                            entry_px = lookup_price(opts_trade, sym, int(ts))
                            if entry_px and entry_px >= cfg['min_price_to_sell']:
                                qty = cfg['ce_quantity'] * mult
                                if sym in open_positions:
                                    ep = open_positions[sym]
                                    total_qty = ep['qty'] + qty
                                    ep['entry_px'] = (ep['entry_px'] * ep['qty'] + entry_px * qty) / total_qty
                                    ep['qty'] = total_qty
                                else:
                                    open_positions[sym] = dict(
                                        entry_px=entry_px, qty=qty,
                                        expiry=trade_expiry, window_expiry=expiry,
                                        opt_type='CE', entry_dt=dt_bar,
                                    )
                                ce_ref = ce_ref - cfg['ce_gap'] * mult
                                ce_reset_flag = 1
                                expiry_stats[expiry]['trades']     += 1
                                expiry_stats[expiry]['premium_in'] += entry_px * qty
                                break
                        strike += 50

            # ── Reset logic ────────────────────────────────────────────────
            if pe_reset_flag == 1 and (pe_ref - price) >= cfg['pe_reset_gap']:
                pe_ref = price + cfg['pe_gap']
                pe_reset_flag = 0
            if ce_reset_flag == 1 and (price - ce_ref) >= cfg['ce_reset_gap']:
                ce_ref = price - cfg['ce_gap']
                ce_reset_flag = 0

            # ── Peak margin tracking ───────────────────────────────────────
            # NRML margin ≈ 14% × spot × qty (proxy for SPAN + Exposure)
            if open_positions:
                total_qty = sum(pos['qty'] for pos in open_positions.values())
                current_margin = 0.14 * price * total_qty
                if current_margin > expiry_stats[expiry]['peak_margin']:
                    expiry_stats[expiry]['peak_margin'] = current_margin

        # ── Settle positions for this expiry at end of window ──────────────
        to_settle = [sym for sym, pos in list(open_positions.items())
                     if pos['expiry'] == expiry]
        for sym in to_settle:
            pos     = open_positions.pop(sym)
            exit_px = get_expiry_settlement(get_options(pos['expiry']), sym, pos['expiry'])
            pnl     = (pos['entry_px'] - exit_px) * pos['qty']
            win_exp = pos.get('window_expiry', expiry)
            if win_exp in expiry_stats:
                expiry_stats[win_exp]['pnl'] += pnl
            trades.append(dict(
                symbol=sym, opt_type=pos['opt_type'],
                entry_px=pos['entry_px'], exit_px=exit_px,
                qty=pos['qty'],
                entry_dt=pos['entry_dt'], exit_dt=pos['expiry'],
                exit_reason='expiry', pnl=pnl,
                window_expiry=win_exp,
            ))

    # Mark any still-open positions (shouldn't occur in normal run)
    for sym, pos in open_positions.items():
        trades.append(dict(
            symbol=sym, opt_type=pos['opt_type'],
            entry_px=pos['entry_px'], exit_px=pos['entry_px'],
            qty=pos['qty'],
            entry_dt=pos['entry_dt'], exit_dt=None,
            exit_reason='open', pnl=0,
            window_expiry=pos.get('window_expiry'),
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

    overall_peak    = max((s['peak_margin'] for s in expiry_stats.values()), default=0)
    total_premium   = sum(s['premium_in']   for s in expiry_stats.values())

    print()
    print("=" * 72)
    print("  SURVIVOR STRATEGY BACKTEST RESULTS  (Expiry-Day Roll + Margin Tracking)")
    print("=" * 72)
    print(f"  Period    : {cfg['start_date']} → {cfg['end_date']}")
    print(f"  Rule      : On expiry day → sell NEXT expiry's options (no gamma risk)")
    print(f"  No capital cap — position size limited only by sell_multiplier_threshold")
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
        print(f"  Return on Peak Margin   : {total_pnl/overall_peak*100:>8.1f}%")
    print(f"  (Margin ≈ 14% × spot × qty — NRML proxy, not exact SEBI SPAN)")

    # ── Per-expiry table ──────────────────────────────────────────────────
    print()
    print("  Per-Expiry Window Summary:")
    hdr = f"  {'Expiry':<12}  {'Window':<24}  {'Trd':>4}  {'Premium Collected':>18}  {'Net P&L':>14}  {'Peak Margin':>14}  {'ROI':>7}"
    print(hdr)
    print("  " + "─" * (len(hdr) - 2))
    for exp_date in sorted(expiry_stats.keys()):
        es  = expiry_stats[exp_date]
        roi = (es['pnl'] / es['peak_margin'] * 100) if es['peak_margin'] > 0 else 0.0
        win = f"{es['window_start']} → {es['window_end']}"
        pnl_sign = '+' if es['pnl'] >= 0 else ''
        print(
            f"  {str(exp_date):<12}  {win:<24}  {es['trades']:>4}"
            f"  ₹{es['premium_in']:>16,.0f}"
            f"  {pnl_sign}₹{es['pnl']:>12,.0f}"
            f"  ₹{es['peak_margin']:>12,.0f}"
            f"  {roi:>6.1f}%"
        )

    # ── Monthly P&L ───────────────────────────────────────────────────────
    print()
    print("  Monthly P&L:")
    monthly = closed.groupby('month')['pnl'].agg(['sum', 'count'])
    max_abs = max(abs(monthly['sum']).max(), 1)
    for period, mrow in monthly.iterrows():
        bar  = '█' * int(abs(mrow['sum']) / max_abs * 20)
        sign = '+' if mrow['sum'] >= 0 else '-'
        print(f"    {period}  {sign}₹{abs(mrow['sum']):>10,.0f}  ({int(mrow['count'])} trades)  {bar}")

    print("=" * 72)

    out_path = os.path.join(os.path.dirname(__file__), 'survivor_backtest_results.csv')
    closed.to_csv(out_path, index=False)
    print(f"\n  Detailed trades saved to: {out_path}")


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print("Survivor Strategy Backtester  [Expiry-Day Roll + Margin Tracking]")
    print("─" * 60)
    df, expiry_stats = simulate(CONFIG)
    report(df, expiry_stats, CONFIG)
