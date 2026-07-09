#!/usr/bin/env python
"""
NIFTY Credit Spread (Short Strangle) Backtester — Lightweight
=============================================================
Uses NIFTY spot (1m) + expired options data from DuckDB historify.
Overnight hold. Queries one symbol at a time to avoid OOM.

Usage: uv run python backtesting/nifty_credit_spread_backtest.py
"""

import duckdb
import pandas as pd
from datetime import datetime, date, timedelta
from pathlib import Path
import warnings
warnings.filterwarnings('ignore')

DB_PATH = str(Path(__file__).resolve().parent.parent / "db" / "historify.duckdb")

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════
INITIAL_CAPITAL = 500000
MAX_LOTS = 2
ENTRY_TIME_H, ENTRY_TIME_M = 9, 30
PE_DISTANCE_PCT = 3.0
CE_DISTANCE_PCT = 3.0
TARGET_PCT = 50.0
STOP_LOSS_PCT = 100.0
DTE_MIN, DTE_MAX = 1, 7

MONTH_MAP = {'JAN':'01','FEB':'02','MAR':'03','APR':'04','MAY':'05','JUN':'06',
             'JUL':'07','AUG':'08','SEP':'09','OCT':'10','NOV':'11','DEC':'12'}


def lot_size(d):
    return 75 if d >= date(2024, 7, 26) else 50


def round_strike(price, step=50):
    return int(round(price / step) * step)


def parse_expiry_code(code):
    try:
        return date(2000 + int(code[5:7]), int(MONTH_MAP[code[2:5]]), int(code[:2]))
    except:
        return None


def run():
    con = duckdb.connect(DB_PATH, read_only=True)

    # 1) Get all NIFTY spot trading days
    td_rows = con.execute("""
        SELECT DISTINCT CAST(MAKE_TIMESTAMP(CAST(timestamp AS BIGINT)*1000000) AS DATE) as d
        FROM market_data WHERE symbol='NIFTY' AND exchange='NSE_INDEX' AND interval='1m'
        ORDER BY d
    """).fetchall()
    trading_days = [r[0] if isinstance(r[0], date) else r[0].date() for r in td_rows]
    print(f"NIFTY spot trading days: {len(trading_days)} ({trading_days[0]} to {trading_days[-1]})")

    # 2) Get all expiry codes
    expiry_rows = con.execute("""
        SELECT DISTINCT REGEXP_EXTRACT(symbol, 'NIFTY(\\d{2}[A-Z]{3}\\d{2})', 1) as code
        FROM market_data WHERE symbol LIKE 'NIFTY%CE' AND exchange='NFO' AND interval='1m'
    """).fetchall()
    expiry_map = {}
    for (code,) in expiry_rows:
        if code:
            d = parse_expiry_code(code)
            if d:
                expiry_map[code] = d
    print(f"Expiries found: {len(expiry_map)}")

    # 3) Run
    capital = INITIAL_CAPITAL
    trades = []
    pos = None

    print(f"\n{'='*85}")
    print(f"  SHORT STRANGLE | PE:{PE_DISTANCE_PCT}% CE:{CE_DISTANCE_PCT}% OTM | Target:{TARGET_PCT}% SL:{STOP_LOSS_PCT}%")
    print(f"  Capital: ₹{INITIAL_CAPITAL:,} | Overnight Hold")
    print(f"{'='*85}\n")

    for td in trading_days:
        entry_ts = int(datetime(td.year, td.month, td.day, ENTRY_TIME_H, ENTRY_TIME_M).timestamp())
        eod_ts = int(datetime(td.year, td.month, td.day, 15, 15).timestamp())

        # ── MONITOR EXISTING POSITION ──
        if pos is not None:
            is_expiry = (td == pos['exp_date'])
            check_ts = eod_ts - 900 if is_expiry else eod_ts

            pe_price = _get_close(con, pos['pe_sym'], check_ts)
            ce_price = _get_close(con, pos['ce_sym'], check_ts)

            if pe_price is None and is_expiry:
                pe_price = 0.05
            if ce_price is None and is_expiry:
                ce_price = 0.05

            if pe_price is not None and ce_price is not None:
                cur_prem = pe_price + ce_price
                entry_prem = pos['premium']
                pnl_pct = ((entry_prem - cur_prem) / entry_prem) * 100

                exit_reason = None
                if is_expiry:
                    exit_reason = "EXPIRY"
                elif pnl_pct >= TARGET_PCT:
                    exit_reason = "TARGET"
                elif pnl_pct <= -STOP_LOSS_PCT:
                    exit_reason = "STOPLOSS"

                if exit_reason:
                    ls = lot_size(pos['entry_date'])
                    pnl = (entry_prem - cur_prem) * ls * pos['lots']
                    capital += pnl
                    hold = (td - pos['entry_date']).days

                    trades.append({
                        'entry': pos['entry_date'].isoformat(), 'exit': td.isoformat(),
                        'spot': pos['spot'], 'pe': pos['pe_strike'], 'ce': pos['ce_strike'],
                        'entry_prem': round(entry_prem, 2), 'exit_prem': round(cur_prem, 2),
                        'lots': pos['lots'], 'lot_size': ls,
                        'pnl': round(pnl, 2), 'pnl_pct': round(pnl_pct, 1),
                        'reason': exit_reason, 'capital': round(capital, 2), 'hold_days': hold,
                    })
                    pnl_s = f"₹{pnl:+,.0f}"
                    print(f"  EXIT {exit_reason:8s} | {pos['entry_date']}→{td} ({hold}d) | "
                          f"PE:{pos['pe_strike']} CE:{pos['ce_strike']} | Prem:{entry_prem:.0f}→{cur_prem:.0f} | "
                          f"{pnl_s} ({pnl_pct:+.0f}%) | Cap:₹{capital:,.0f}")
                    pos = None

        # ── ENTER NEW ──
        if pos is None:
            spot = _get_close(con, 'NIFTY', entry_ts, exchange='NSE_INDEX')
            if spot is None:
                continue

            # Find next expiry
            best = None
            for code, exp_d in expiry_map.items():
                dte = (exp_d - td).days
                if DTE_MIN <= dte <= DTE_MAX:
                    if best is None or dte < best[0]:
                        best = (dte, code, exp_d)
            if best is None:
                continue

            dte, exp_code, exp_date = best
            pe_strike = round_strike(spot * (1 - PE_DISTANCE_PCT / 100))
            ce_strike = round_strike(spot * (1 + CE_DISTANCE_PCT / 100))

            pe_sym = f"NIFTY{exp_code}{pe_strike}PE"
            ce_sym = f"NIFTY{exp_code}{ce_strike}CE"

            pe_price = _get_close(con, pe_sym, entry_ts)
            ce_price = _get_close(con, ce_sym, entry_ts)

            if pe_price is None or ce_price is None or pe_price + ce_price <= 5:
                continue

            premium = pe_price + ce_price
            lots = min(MAX_LOTS, max(1, int(capital / (spot * lot_size(td) * 0.12))))

            pos = {
                'entry_date': td, 'exp_code': exp_code, 'exp_date': exp_date,
                'spot': round(spot, 2), 'pe_strike': pe_strike, 'ce_strike': ce_strike,
                'pe_sym': pe_sym, 'ce_sym': ce_sym,
                'premium': premium, 'lots': lots,
            }
            print(f"  ENTER        | {td} | Spot:{spot:.0f} PE:{pe_strike} CE:{ce_strike} | "
                  f"Prem:{premium:.0f} | {lots}×{lot_size(td)} | Exp:{exp_code}(DTE:{dte})")

    con.close()

    # ═══════════════════════════════════════════════════════════════════════════
    # RESULTS
    # ═══════════════════════════════════════════════════════════════════════════
    if not trades:
        print("\nNo trades!")
        return

    df = pd.DataFrame(trades)
    w = df[df['pnl'] > 0]
    l = df[df['pnl'] <= 0]
    total = df['pnl'].sum()
    cum = df['pnl'].cumsum()
    max_dd = (cum - cum.cummax()).min()
    days_span = (pd.to_datetime(df['exit'].iloc[-1]) - pd.to_datetime(df['entry'].iloc[0])).days

    print(f"\n{'='*85}")
    print(f"  RESULTS | {df['entry'].iloc[0]} to {df['exit'].iloc[-1]}")
    print(f"{'='*85}")
    print(f"  Trades: {len(df)} | Win: {len(w)} ({len(w)/len(df)*100:.0f}%) | Lose: {len(l)} ({len(l)/len(df)*100:.0f}%)")
    print(f"  Total P&L: ₹{total:,.0f} ({total/INITIAL_CAPITAL*100:.1f}%)")
    print(f"  Avg Win: ₹{w['pnl'].mean():,.0f}" if len(w) else "  Avg Win: -")
    print(f"  Avg Loss: ₹{l['pnl'].mean():,.0f}" if len(l) else "  Avg Loss: -")
    if len(l) > 0 and l['pnl'].sum() != 0:
        print(f"  Profit Factor: {abs(w['pnl'].sum()/l['pnl'].sum()):.2f}")
    print(f"  Max Drawdown: ₹{max_dd:,.0f} ({max_dd/INITIAL_CAPITAL*100:.1f}%)")
    print(f"  Final Capital: ₹{capital:,.0f}")
    if days_span > 0:
        print(f"  CAGR: {((capital/INITIAL_CAPITAL)**(365/days_span)-1)*100:.1f}%")
    print(f"  Avg Hold: {df['hold_days'].mean():.1f} days")

    # Exit reasons
    print(f"\n  Exit Breakdown:")
    for r, g in df.groupby('reason'):
        print(f"    {r:10s}: {len(g):3d} trades | Avg P&L: ₹{g['pnl'].mean():,.0f}")

    # Monthly
    df['month'] = pd.to_datetime(df['entry']).dt.to_period('M')
    print(f"\n  Monthly P&L:")
    for m, p in df.groupby('month')['pnl'].sum().items():
        print(f"    {m}: ₹{p:>+10,.0f}")

    out = Path(__file__).parent / "nifty_credit_spread_results.json"
    df.drop(columns=['month']).to_json(out, orient='records', indent=2)
    print(f"\n  Saved: {out}")
    print("=" * 85)


def _get_close(con, symbol, ts, exchange='NFO', window=300):
    """Get close price near timestamp. Lightweight single-row query."""
    ex = exchange
    row = con.execute("""
        SELECT close FROM market_data
        WHERE symbol=? AND exchange=? AND interval='1m'
          AND timestamp BETWEEN ? AND ?
        ORDER BY timestamp LIMIT 1
    """, [symbol, ex, ts - window, ts + window]).fetchone()
    return float(row[0]) if row else None


if __name__ == "__main__":
    run()
