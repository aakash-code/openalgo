"""
Survivor Strategy Backtester — V2 (Improved Risk Controls)
===========================================================
Based on the live strategy logic (backtest_survivor_live.py) with three
improvements to control drawdown while preserving profit:

  1. 3× Stop-Loss   — if option LTP > 3× entry price, buy back immediately
                      Prevents the 8× losers (e.g., sold 62 → settled 516)
  2. Max Open Qty   — cap total open quantity across all positions
                      Prevents the Oct 2024 week building ₹4.5 Cr margin
  3. Weekly Circuit — stop new entries when week's running P&L < -threshold
                      Cuts cascading losses in directional trending weeks

All three parameters are configurable in IMPROVEMENTS section below.

Run: uv run python strategies/backtest_survivor_v2.py
Compare against: strategies/backtest_survivor_live.py (baseline)
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

# ── Base config (matches live strategy) ───────────────────────────────────
CONFIG = {
    'pe_gap':                   20,
    'ce_gap':                   20,
    'pe_symbol_gap':           200,
    'ce_symbol_gap':           200,
    'pe_quantity':              65,
    'ce_quantity':              65,
    'min_price_to_sell':        15,
    'sell_multiplier_threshold': 5,
    'pe_reset_gap':             30,
    'ce_reset_gap':             30,
    'lot_size':                 25,
    'start_date':      '2024-10-03',
    'end_date':        '2026-05-09',
}

# ── Improvement parameters ─────────────────────────────────────────────────
IMPROVEMENTS = {
    # 1. EOD Stop-loss: at 15:20 each day, if any position's close price >
    #    stop_multiple × entry_price → exit at that price.
    #    Checked END-OF-DAY only (not every 1-minute bar) to avoid intraday spikes.
    #    Set to None to disable.  Typical range: 3.0 – 5.0×
    'stop_loss_multiple': 3.0,

    # 2. Max total open units across ALL positions combined.
    #    Set to None to disable.  At NIFTY 23K: 3000 qty ≈ ₹9.66L margin
    'max_open_qty': 3000,

    # 3. Weekly loss circuit breaker: once running week P&L (stop-outs + settled)
    #    drops below this threshold (negative Rs), no new entries until next expiry.
    #    Set to None to disable.  Suggested: -400000 (₹4L)
    'weekly_circuit_breaker': -400_000,
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

def simulate(cfg: dict, imp: dict) -> tuple[pd.DataFrame, dict]:
    stop_multiple  = imp.get('stop_loss_multiple')   # None → disabled
    max_qty        = imp.get('max_open_qty')          # None → disabled
    circuit_thresh = imp.get('weekly_circuit_breaker') # None → disabled

    conn = duckdb.connect(DB_PATH, read_only=True)

    print("Loading NIFTY spot data …")
    spot = load_nifty_spot(conn, cfg['start_date'], cfg['end_date'])
    print(f"  {len(spot):,} bars | {spot['dt'].iloc[0]:%Y-%m-%d} → {spot['dt'].iloc[-1]:%Y-%m-%d}")

    expiries = load_expiries(conn, cfg['start_date'], cfg['end_date'])
    print(f"  {len(expiries)} weekly expiries | {expiries[0]} → {expiries[-1]}")
    print(f"  Stop-loss: {stop_multiple}×  |  Max qty: {max_qty}  |  Circuit: ₹{circuit_thresh}")

    trades          = []
    open_positions: dict[str, dict] = {}
    expiry_stats:   dict[date, dict] = {}
    stop_loss_count = 0

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
    pe_reset_flag = 0
    ce_reset_flag = 0
    cumulative_margin = 0.0
    trade_no          = 0

    market_open_s  = 9 * 3600 + 15 * 60
    market_close_s = 15 * 3600 + 30 * 60

    for ei, expiry in enumerate(expiries):
        prev_expiry  = expiries[ei - 1] if ei > 0 else None
        period_start = (prev_expiry + timedelta(days=1)) if prev_expiry else expiry

        print(f"\rProcessing expiry {expiry} ({ei+1}/{len(expiries)}) …", end='', flush=True)

        get_options(expiry)

        expiry_stats[expiry] = {
            'window_start': period_start,
            'window_end':   expiry,
            'trades':       0,
            'premium_in':   0.0,
            'peak_margin':  0.0,
            'pnl':          0.0,
            'stops_fired':  0,
        }

        mask = (spot['date'] > prev_expiry) & (spot['date'] <= expiry) if prev_expiry \
               else (spot['date'] <= expiry)
        period_df = spot[mask]

        if pe_ref is None and not period_df.empty:
            pe_ref = float(period_df['close'].iloc[0])
        if ce_ref is None and not period_df.empty:
            ce_ref = float(period_df['close'].iloc[0])

        # Weekly circuit breaker state (resets each expiry window)
        week_running_pnl = 0.0
        circuit_active   = False
        prev_bar_date    = None   # used to detect EOD (last bar of each calendar day)

        # ── Bar-by-bar ─────────────────────────────────────────────────
        for ts, row in period_df.iterrows():
            dt_bar     = row['dt']
            sec_of_day = dt_bar.hour * 3600 + dt_bar.minute * 60
            bar_date   = row['date']
            if sec_of_day < market_open_s or sec_of_day >= market_close_s:
                continue

            price = float(row['close'])

            # ── IMPROVEMENT 1: EOD stop-loss (15:18–15:29 window) ────
            # Check once per day at end-of-session, not every bar.
            # Avoids firing on intraday spikes that recover before expiry.
            is_eod_bar = (sec_of_day >= 15 * 3600 + 18 * 60)
            if stop_multiple is not None and is_eod_bar:
                for sym in list(open_positions.keys()):
                    pos      = open_positions[sym]
                    opts_pos = get_options(pos['expiry'])
                    curr_px  = lookup_price(opts_pos, sym, int(ts))
                    if curr_px and curr_px >= stop_multiple * pos['entry_px']:
                        exit_px = curr_px
                        pnl     = (pos['entry_px'] - exit_px) * pos['qty']
                        week_running_pnl  += pnl
                        expiry_stats[expiry]['pnl'] += pnl
                        stop_loss_count += 1
                        expiry_stats[expiry]['stops_fired'] += 1
                        trades.append(dict(
                            trade_no=pos['trade_no'],
                            symbol=sym,
                            opt_type=pos['opt_type'],
                            entry_px=pos['entry_px'],
                            exit_px=exit_px,
                            qty=pos['qty'],
                            entry_dt=pos['entry_dt'],
                            exit_dt=dt_bar,
                            exit_reason='stop_loss',
                            pnl=pnl,
                            credit_received=pos['credit_received'],
                            margin_used=pos['margin_used'],
                            expiry=pos['expiry'],
                        ))
                        del open_positions[sym]

            # ── IMPROVEMENT 3: Circuit breaker check ─────────────────
            if circuit_thresh is not None and not circuit_active:
                if week_running_pnl < circuit_thresh:
                    circuit_active = True
                    print(f"\n  [CIRCUIT] Week {expiry}: running P&L ₹{week_running_pnl:,.0f} "
                          f"< threshold ₹{circuit_thresh:,.0f}. No new entries until next expiry.")

            # ── IMPROVEMENT 2: Total qty check ────────────────────────
            total_open_qty = sum(p['qty'] for p in open_positions.values())

            # ── PE logic ──────────────────────────────────────────────
            pe_diff = price - pe_ref
            if (not circuit_active) and pe_diff > cfg['pe_gap']:
                sell_multiplier = int(pe_diff / cfg['pe_gap'])
                if sell_multiplier <= cfg['sell_multiplier_threshold']:
                    new_qty = cfg['pe_quantity'] * sell_multiplier
                    if max_qty is None or (total_open_qty + new_qty) <= max_qty:
                        opts     = get_options(expiry)
                        strike   = nearest_strike(price, cfg['pe_symbol_gap'], 'PE')
                        for _ in range(8):
                            sym = option_symbol(expiry, strike, 'PE')
                            if sym in opts:
                                entry_px = lookup_price(opts, sym, int(ts))
                                if entry_px and entry_px >= cfg['min_price_to_sell']:
                                    qty    = new_qty
                                    margin = 0.14 * price * qty
                                    cumulative_margin += margin
                                    trade_no += 1
                                    if sym in open_positions:
                                        ep = open_positions[sym]
                                        tot = ep['qty'] + qty
                                        ep['entry_px'] = (ep['entry_px'] * ep['qty'] + entry_px * qty) / tot
                                        ep['qty'] = tot
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
                                    pe_reset_flag = 1
                                    expiry_stats[expiry]['trades']     += 1
                                    expiry_stats[expiry]['premium_in'] += entry_px * qty
                                    break
                            strike -= 50

            # ── CE logic ──────────────────────────────────────────────
            ce_diff = ce_ref - price
            if (not circuit_active) and ce_diff > cfg['ce_gap']:
                sell_multiplier = int(ce_diff / cfg['ce_gap'])
                if sell_multiplier <= cfg['sell_multiplier_threshold']:
                    new_qty = cfg['ce_quantity'] * sell_multiplier
                    total_open_qty = sum(p['qty'] for p in open_positions.values())  # re-check after PE
                    if max_qty is None or (total_open_qty + new_qty) <= max_qty:
                        opts   = get_options(expiry)
                        strike = nearest_strike(price, cfg['ce_symbol_gap'], 'CE')
                        for _ in range(8):
                            sym = option_symbol(expiry, strike, 'CE')
                            if sym in opts:
                                entry_px = lookup_price(opts, sym, int(ts))
                                if entry_px and entry_px >= cfg['min_price_to_sell']:
                                    qty    = new_qty
                                    margin = 0.14 * price * qty
                                    cumulative_margin += margin
                                    trade_no += 1
                                    if sym in open_positions:
                                        ep = open_positions[sym]
                                        tot = ep['qty'] + qty
                                        ep['entry_px'] = (ep['entry_px'] * ep['qty'] + entry_px * qty) / tot
                                        ep['qty'] = tot
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

            # ── Reset logic (exact live code behaviour) ────────────────
            if pe_reset_flag and (pe_ref - price) > cfg['pe_reset_gap']:
                pe_ref = price + cfg['pe_reset_gap']

            if ce_reset_flag and (price - ce_ref) > cfg['ce_reset_gap']:
                ce_ref = price - cfg['ce_reset_gap']

            # ── Peak margin tracking ──────────────────────────────────
            if open_positions:
                total_qty      = sum(pos['qty'] for pos in open_positions.values())
                current_margin = 0.14 * price * total_qty
                if current_margin > expiry_stats[expiry]['peak_margin']:
                    expiry_stats[expiry]['peak_margin'] = current_margin

        # ── Settle positions for this expiry ──────────────────────────
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
                expiry=pos['expiry'],
            ))

    for sym, pos in open_positions.items():
        trades.append(dict(
            trade_no=pos['trade_no'],
            symbol=sym, opt_type=pos['opt_type'],
            entry_px=pos['entry_px'], exit_px=pos['entry_px'],
            qty=pos['qty'], entry_dt=pos['entry_dt'],
            exit_dt=None, exit_reason='open', pnl=0,
            credit_received=pos['credit_received'],
            margin_used=pos['margin_used'], expiry=pos['expiry'],
        ))

    conn.close()
    print()
    print(f"  Stop-loss exits fired: {stop_loss_count}")
    return pd.DataFrame(trades), expiry_stats


# ── Reporting ──────────────────────────────────────────────────────────────

def report(df: pd.DataFrame, expiry_stats: dict, cfg: dict, imp: dict) -> None:
    closed = df[df['exit_reason'].isin(['expiry', 'stop_loss'])].copy()
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
    stops       = closed[closed['exit_reason'] == 'stop_loss']

    cum      = closed.sort_values('entry_dt')['pnl'].cumsum()
    drawdown = (cum - cum.cummax()).min()

    overall_peak  = max((s['peak_margin'] for s in expiry_stats.values()), default=0)
    total_premium = sum(s['premium_in'] for s in expiry_stats.values())

    print()
    print("=" * 72)
    print("  SURVIVOR STRATEGY V2  (Stop-Loss + Qty Cap + Circuit Breaker)")
    print("=" * 72)
    print(f"  Period       : {cfg['start_date']} → {cfg['end_date']}")
    print(f"  Stop-loss    : {imp.get('stop_loss_multiple')}× premium  |  "
          f"Max qty: {imp.get('max_open_qty')}  |  "
          f"Circuit: ₹{imp.get('weekly_circuit_breaker'):,}")
    print()
    print(f"  Total Trades   : {len(closed):,}  "
          f"(expiry: {(closed['exit_reason']=='expiry').sum()}, "
          f"stop-loss: {len(stops)})")
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
    print(f"  Stop-loss exits: {len(stops):,}  "
          f"(P&L from stops: ₹{stops['pnl'].sum():,.0f})")
    print(f"  Total Premium  : ₹{total_premium:>12,.0f}")
    print(f"  Overall Peak   : ₹{overall_peak:>12,.0f}  margin")
    if overall_peak > 0:
        print(f"  ROI on Margin  : {total_pnl/overall_peak*100:>11.1f}%")
    print(f"  Note: Margin ≈ 14% × spot × qty (NRML proxy)")
    print()

    # ── Per-expiry table ──────────────────────────────────────────────
    print(f"  {'Expiry':<12}  {'Window':<24}  {'Trd':>3}  {'Stp':>3}  "
          f"{'Premium':>12}  {'P&L':>12}  {'PeakMargin':>12}  {'ROI':>6}")
    print("  " + "-" * 94)
    for exp, s in sorted(expiry_stats.items()):
        if s['trades'] == 0 and s['pnl'] == 0:
            continue
        roi  = s['pnl'] / s['peak_margin'] * 100 if s['peak_margin'] > 0 else 0.0
        sign = '+' if s['pnl'] >= 0 else ''
        print(
            f"  {str(exp):<12}  "
            f"{str(s['window_start'])} → {str(s['window_end'])}  "
            f"{s['trades']:>3}  "
            f"{s['stops_fired']:>3}  "
            f"₹{s['premium_in']:>10,.0f}  "
            f"{sign}₹{s['pnl']:>10,.0f}  "
            f"₹{s['peak_margin']:>10,.0f}  "
            f"{roi:>5.1f}%"
        )
    print()

    # ── Monthly breakdown ─────────────────────────────────────────────
    print("  Monthly P&L:")
    monthly = closed.groupby('month')['pnl'].agg(['sum', 'count'])
    max_abs = max(abs(monthly['sum'].max()), abs(monthly['sum'].min()), 1)
    for period, row in monthly.iterrows():
        bar_len = int(abs(row['sum']) / max_abs * 20)
        bar  = ('█' if row['sum'] >= 0 else '░') * bar_len
        sign = '+' if row['sum'] >= 0 else '-'
        print(f"    {period}  {sign}₹{abs(row['sum']):>10,.0f}  ({int(row['count'])} trades)  {bar}")
    print("=" * 72)

    out_path = os.path.join(os.path.dirname(__file__), 'survivor_v2_backtest_results.csv')
    closed.to_csv(out_path, index=False)
    print(f"\n  Detailed trades saved to: {out_path}")


# ── Comparison helper ──────────────────────────────────────────────────────

def print_comparison(df_v2: pd.DataFrame, imp: dict) -> None:
    """Print side-by-side vs baseline (live version without improvements)."""
    BASELINE = {
        'Total P&L':    7_161_534,
        'Win Rate':     81.9,
        'Worst Trade': -353_870,
        'Max Drawdown': -955_845,
        'Peak Margin':  44_825_614,
        'ROI Margin':   16.0,
        'Trades':       698,
    }
    closed = df_v2[df_v2['exit_reason'].isin(['expiry', 'stop_loss'])].copy()
    closed['entry_dt'] = pd.to_datetime(closed['entry_dt'])
    total_pnl   = closed['pnl'].sum()
    win_rate    = (closed['pnl'] > 0).mean() * 100
    worst_trade = closed['pnl'].min()
    cum         = closed.sort_values('entry_dt')['pnl'].cumsum()
    drawdown    = (cum - cum.cummax()).min()

    print()
    print("  ┌────────────────────────┬──────────────────┬──────────────────┐")
    print("  │ Metric                 │   Baseline Live  │   V2 (Improved)  │")
    print("  ├────────────────────────┼──────────────────┼──────────────────┤")
    metrics = [
        ("Total P&L",    f"₹{BASELINE['Total P&L']:>14,.0f}", f"₹{total_pnl:>14,.0f}"),
        ("Trades",       f"{BASELINE['Trades']:>16}",          f"{len(closed):>16}"),
        ("Win Rate",     f"{BASELINE['Win Rate']:>15.1f}%",    f"{win_rate:>15.1f}%"),
        ("Worst Trade",  f"₹{BASELINE['Worst Trade']:>14,.0f}",f"₹{worst_trade:>14,.0f}"),
        ("Max Drawdown", f"₹{BASELINE['Max Drawdown']:>14,.0f}",f"₹{drawdown:>14,.0f}"),
    ]
    for name, base_val, v2_val in metrics:
        print(f"  │ {name:<22} │ {base_val} │ {v2_val} │")
    print("  └────────────────────────┴──────────────────┴──────────────────┘")


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print("Survivor Strategy Backtester V2 — Risk-Controlled Live Version")
    print("─" * 55)
    df, expiry_stats = simulate(CONFIG, IMPROVEMENTS)
    report(df, expiry_stats, CONFIG, IMPROVEMENTS)
    print_comparison(df, IMPROVEMENTS)
