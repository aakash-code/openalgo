#!/usr/bin/env python
"""
CPR Option Selling Strategy — NIFTY Backtester
==============================================
Faithful replication of the TradingView "CPR Option Sale Strategy" (© DeuceDavis,
MPL-2.0) applied to NIFTY weekly options, using NIFTY spot 1m + expired NFO option
1m bars from db/historify.duckdb.

Logic (per the Pine indicator):
  * Weekly CPR is built from the PREVIOUS completed week's H/L/C (daily chart ->
    weekly pivots, pivotPeriodShift = 1).
        centralPivot = (H + L + C) / 3
        topPivot / bottomPivot  = CPR band (sorted)
        tR1 = 2*CP - L ,  tS1 = 2*CP - H        (traditional R1/S1)
  * At each week's open the entry spot vs the CPR band decides the trade:
        spot > topPivot     -> Sell Put   (bull put spread)
        spot < bottomPivot  -> Sell Call  (bear call spread)
        inside the band     -> Sell IC    (iron condor)
  * Strikes (snapped to the NIFTY 50 grid, exactly as the Pine `spreadValue`=50):
        callStrike = ceil( (spot<tS1 ? topPivot    : avg(CP, max(prevH, tR1))) /50)*50
        putStrike  = floor((spot>tR1 ? bottomPivot : avg(CP, min(prevL, tS1))) /50)*50
        IC: icCall = ceil(tR1/50)*50 ,  icPut = floor(tS1/50)*50
    Each short leg is hedged by one strike (the Pine hedges +/- spreadValue=50);
    we sweep the hedge WIDTH to show its effect on risk:reward.

Two exit models are reported side by side:
  * FAITHFUL  : hold to weekly expiry, settle at spot intrinsic. No SL, no costs.
                Matches the indicator's on-chart win% definition.
  * REALISTIC : same entries, but an intra-week stop (loss >= SL_MULT x credit),
                per-leg slippage, and per-leg brokerage/taxes.

Usage:
    uv run python backtesting/cpr_option_selling/cpr_backtest.py
Env overrides (optional):
    SL_MULT=2.0  SLIPPAGE_PTS=1.0  COST_RS_PER_LEG=30  START=2022-01-01  END=2026-12-31
"""

import json
import math
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

DB_PATH = str(Path(__file__).resolve().parent.parent.parent / "db" / "historify.duckdb")
OUT_DIR = Path(__file__).resolve().parent / "results"

# ── Cost / risk model (realistic leg) ────────────────────────────────────────
SL_MULT = float(os.environ.get("SL_MULT", 2.0))          # SL when loss >= SL_MULT x net credit
SLIPPAGE_PTS = float(os.environ.get("SLIPPAGE_PTS", 1.0))  # premium points lost per leg per fill
COST_RS_PER_LEG = float(os.environ.get("COST_RS_PER_LEG", 30))  # brokerage+taxes, Rs/leg/side
MIN_CREDIT_PTS = 2.0                                       # skip near-zero-credit spreads
GRID = 50                                                  # NIFTY strike interval
HEDGE_WIDTHS = [50, 100, 200, 300]                         # 50 = faithful; rest = sweep

START = os.environ.get("START", "2022-01-01")
END = os.environ.get("END", "2026-12-31")

MONTH_MAP = {'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
             'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12}
MONTH_ABBR = {v: k for k, v in MONTH_MAP.items()}


def nifty_lot_size(d: date) -> int:
    """NIFTY F&O lot size by SEBI timeline: 25 -> 75 (20 Nov 2024) -> 65 (1 Jan 2026)."""
    if d >= date(2026, 1, 1):
        return 65
    if d >= date(2024, 11, 20):
        return 75
    return 25


def ceil_grid(x):
    return int(math.ceil(x / GRID) * GRID)


def floor_grid(x):
    return int(math.floor(x / GRID) * GRID)


def parse_expiry_code(code):
    try:
        return date(2000 + int(code[5:7]), MONTH_MAP[code[2:5]], int(code[:2]))
    except Exception:
        return None


def expiry_code(d: date) -> str:
    return f"{d.day:02d}{MONTH_ABBR[d.month]}{d.year % 100:02d}"


def _ts(d: date, h, m):
    return int(datetime(d.year, d.month, d.day, h, m).timestamp())


def _get_close(con, symbol, ts, exchange='NFO', window=300):
    """Close price near a timestamp (single-row query, +/- window seconds)."""
    row = con.execute(
        """SELECT close FROM market_data
           WHERE symbol=? AND exchange=? AND interval='1m'
             AND timestamp BETWEEN ? AND ?
           ORDER BY timestamp LIMIT 1""",
        [symbol, exchange, ts - window, ts + window],
    ).fetchone()
    return float(row[0]) if row else None


def _spot(con, d: date, h=9, m=20):
    return _get_close(con, 'NIFTY', _ts(d, h, m), exchange='NSE_INDEX')


# ═══════════════════════════════════════════════════════════════════════════════
# Data loading
# ═══════════════════════════════════════════════════════════════════════════════
def load_spot_daily(con):
    rows = con.execute(
        """SELECT CAST(MAKE_TIMESTAMP(CAST(timestamp AS BIGINT)*1000000) AS DATE) d,
                  arg_min(open, timestamp) o, MAX(high) h, MIN(low) l,
                  arg_max(close, timestamp) c
           FROM market_data
           WHERE symbol='NIFTY' AND exchange='NSE_INDEX' AND interval='1m'
           GROUP BY d ORDER BY d"""
    ).fetchall()
    df = pd.DataFrame(rows, columns=['d', 'o', 'h', 'l', 'c'])
    df['d'] = pd.to_datetime(df['d'])
    return df.set_index('d')


def load_expiries(con):
    rows = con.execute(
        r"""SELECT DISTINCT REGEXP_EXTRACT(symbol,'NIFTY(\d{2}[A-Z]{3}\d{2})',1) code
            FROM market_data WHERE symbol LIKE 'NIFTY%CE'
              AND exchange='NFO' AND interval='1m'"""
    ).fetchall()
    out = {}
    for (code,) in rows:
        d = parse_expiry_code(code) if code else None
        if d:
            out[d] = code
    return dict(sorted(out.items()))


def weekly_bars(daily: pd.DataFrame) -> pd.DataFrame:
    """Aggregate daily spot OHLC into ISO-week bars, indexed by week's Monday."""
    g = daily.resample('W-MON', label='left', closed='left')
    wk = pd.DataFrame({'h': g['h'].max(), 'l': g['l'].min(),
                       'c': g['c'].last(), 'o': g['o'].first()}).dropna()
    return wk


# ═══════════════════════════════════════════════════════════════════════════════
# Strategy core
# ═══════════════════════════════════════════════════════════════════════════════
def cpr_levels(prevH, prevL, prevC):
    cp = (prevH + prevL + prevC) / 3
    bc = (prevH + prevL) / 2
    tc = (cp - bc) + cp
    bottom, top = (bc, tc) if bc <= tc else (tc, bc)
    tR1 = 2 * cp - prevL
    tS1 = 2 * cp - prevH
    return cp, top, bottom, tR1, tS1


def decide(spot, top, bottom):
    if spot > top:
        return 'SELL_PUT'
    if spot < bottom:
        return 'SELL_CALL'
    return 'SELL_IC'


def short_strikes(decision, spot, cp, top, bottom, prevH, prevL, tR1, tS1):
    """Return list of (right, short_strike) the Pine would sell."""
    if decision == 'SELL_PUT':
        calc = bottom if spot > tR1 else (cp + min(prevL, tS1)) / 2
        return [('PE', floor_grid(calc))]
    if decision == 'SELL_CALL':
        calc = top if spot < tS1 else (cp + max(prevH, tR1)) / 2
        return [('CE', ceil_grid(calc))]
    # IC
    return [('CE', ceil_grid(tR1)), ('PE', floor_grid(tS1))]


# ═══════════════════════════════════════════════════════════════════════════════
# Simulation
# ═══════════════════════════════════════════════════════════════════════════════
def settle_value(legs, spot_exp):
    """Intrinsic value (points) at expiry. kl=None => naked (uncapped) short."""
    val = 0.0
    for right, ks, kl in legs:
        if right == 'PE':
            val += max(ks - spot_exp, 0) - (max(kl - spot_exp, 0) if kl is not None else 0)
        else:
            val += max(spot_exp - ks, 0) - (max(spot_exp - kl, 0) if kl is not None else 0)
    return val


def run_for_width(con, daily, wk, expiries, width, hedged=True):
    trading_days = list(daily.index.date)
    td_set = set(trading_days)
    start_d = datetime.strptime(START, "%Y-%m-%d").date()
    end_d = datetime.strptime(END, "%Y-%m-%d").date()

    faithful, realistic = [], []
    skips = {'no_prev_week': 0, 'no_entry_day': 0, 'no_premium': 0, 'low_credit': 0}

    for exp_d, code in expiries.items():
        if not (start_d <= exp_d <= end_d):
            continue
        # Week (Monday) containing this expiry
        monday = exp_d - timedelta(days=exp_d.weekday())
        wk_key = pd.Timestamp(monday)
        prev_key = wk_key - pd.Timedelta(days=7)
        if prev_key not in wk.index:
            skips['no_prev_week'] += 1
            continue
        prevbar = wk.loc[prev_key]
        prevH, prevL, prevC = float(prevbar['h']), float(prevbar['l']), float(prevbar['c'])

        # Entry day = first trading day of the expiry's week, strictly before expiry
        entry_day = None
        for off in range(0, 7):
            cand = monday + timedelta(days=off)
            if cand >= exp_d:
                break
            if cand in td_set:
                entry_day = cand
                break
        if entry_day is None:
            skips['no_entry_day'] += 1
            continue

        spot = _spot(con, entry_day)
        if spot is None:
            skips['no_entry_day'] += 1
            continue

        cp, top, bottom, tR1, tS1 = cpr_levels(prevH, prevL, prevC)
        decision = decide(spot, top, bottom)
        shorts = short_strikes(decision, spot, cp, top, bottom, prevH, prevL, tR1, tS1)

        # Build legs (short + hedge), fetch entry premiums
        legs = []           # (right, short_strike, long_strike)
        entry_ts = _ts(entry_day, 9, 20)
        gross_credit = 0.0
        ok = True
        for right, ks in shorts:
            ssym = f"NIFTY{code}{ks}{right}"
            sp = _get_close(con, ssym, entry_ts)
            if sp is None:
                ok = False
                break
            if hedged:
                kl = ks - width if right == 'PE' else ks + width
                lsym = f"NIFTY{code}{kl}{right}"
                lp = _get_close(con, lsym, entry_ts)
                if lp is None:
                    ok = False
                    break
            else:
                kl, lsym, lp = None, None, 0.0
            gross_credit += (sp - lp)
            legs.append((right, ks, kl, ssym, lsym, sp, lp))
        if not ok:
            skips['no_premium'] += 1
            continue
        if gross_credit < MIN_CREDIT_PTS:
            skips['low_credit'] += 1
            continue

        lot = nifty_lot_size(entry_day)
        n_legs = len(legs) * (2 if hedged else 1)  # short (+ hedge) per spread
        settle_legs = [(r, ks, kl) for (r, ks, kl, *_rest) in legs]

        # ── FAITHFUL: settle at expiry intrinsic ──
        spot_exp = _spot(con, exp_d, 15, 14) or _spot(con, exp_d, 15, 0)
        if spot_exp is None:
            spot_exp = float(daily.loc[pd.Timestamp(exp_d), 'c']) if pd.Timestamp(exp_d) in daily.index else spot
        f_val = settle_value(settle_legs, spot_exp)
        f_pnl_pts = gross_credit - f_val
        faithful.append({
            'expiry': exp_d.isoformat(), 'entry': entry_day.isoformat(),
            'decision': decision, 'spot': round(spot, 1), 'spot_exp': round(spot_exp, 1),
            'credit': round(gross_credit, 2), 'settle': round(f_val, 2),
            'pnl_pts': round(f_pnl_pts, 2), 'pnl': round(f_pnl_pts * lot, 0),
            'lot': lot, 'win': f_pnl_pts > 0,
        })

        # ── REALISTIC: intra-week SL + slippage + costs ──
        eff_credit = gross_credit - SLIPPAGE_PTS * n_legs
        exit_reason, exit_val, exit_day = 'EXPIRY', f_val, exp_d
        # walk trading days from entry (exclusive) to expiry (exclusive) for SL
        cur = entry_day + timedelta(days=1)
        while cur < exp_d:
            if cur in td_set:
                cval, valid = 0.0, True
                for (right, ks, kl, ssym, lsym, *_p) in legs:
                    sp = _get_close(con, ssym, _ts(cur, 15, 15))
                    lp = 0.0
                    if lsym is not None:
                        lp = _get_close(con, lsym, _ts(cur, 15, 15))
                    if sp is None or lp is None:
                        valid = False
                        break
                    cval += (sp - lp)
                if valid:
                    pnl_now = eff_credit - cval
                    if pnl_now <= -SL_MULT * gross_credit:
                        exit_reason = 'STOPLOSS'
                        exit_val = cval + SLIPPAGE_PTS * n_legs  # buy-back slippage
                        exit_day = cur
                        break
            cur += timedelta(days=1)

        r_pnl_pts = eff_credit - exit_val
        brokerage = COST_RS_PER_LEG * n_legs * 2  # entry + exit, all legs
        r_pnl = r_pnl_pts * lot - brokerage
        realistic.append({
            'expiry': exp_d.isoformat(), 'entry': entry_day.isoformat(),
            'decision': decision, 'spot': round(spot, 1),
            'credit': round(gross_credit, 2), 'exit_val': round(exit_val, 2),
            'reason': exit_reason, 'exit_day': exit_day.isoformat(),
            'pnl_pts': round(r_pnl_pts, 2), 'pnl': round(r_pnl, 0),
            'lot': lot, 'win': r_pnl > 0,
        })

    return faithful, realistic, skips


# ═══════════════════════════════════════════════════════════════════════════════
# Reporting
# ═══════════════════════════════════════════════════════════════════════════════
def summarize(label, trades):
    if not trades:
        print(f"  {label}: no trades")
        return None
    df = pd.DataFrame(trades)
    wins = df[df['win']]
    total = df['pnl'].sum()
    cum = df['pnl'].cumsum()
    max_dd = (cum - cum.cummax()).min()
    pf = (wins['pnl'].sum() / abs(df[~df['win']]['pnl'].sum())
          if (~df['win']).any() and df[~df['win']]['pnl'].sum() != 0 else float('inf'))
    print(f"  {label}")
    print(f"    Trades {len(df)} | Win {len(wins)} ({len(wins)/len(df)*100:.1f}%) | "
          f"Net Rs{total:,.0f} | PF {pf:.2f} | MaxDD Rs{max_dd:,.0f}")
    print(f"    Avg/trade Rs{df['pnl'].mean():,.0f} | "
          f"AvgWin Rs{wins['pnl'].mean():,.0f} | "
          f"AvgLoss Rs{df[~df['win']]['pnl'].mean():,.0f}" if len(wins) and (~df['win']).any() else "")
    # decision split
    for dec, g in df.groupby('decision'):
        gw = g[g['win']]
        print(f"      {dec:9s}: {len(g):3d} | win {len(gw)/len(g)*100:4.0f}% | net Rs{g['pnl'].sum():>+10,.0f}")
    if 'reason' in df.columns:
        for r, g in df.groupby('reason'):
            print(f"      exit {r:8s}: {len(g):3d} | net Rs{g['pnl'].sum():>+10,.0f}")
    return {'trades': len(df), 'wins': int(len(wins)), 'win_pct': round(len(wins)/len(df)*100, 1),
            'net': round(total, 0), 'pf': round(pf, 2) if pf != float('inf') else None,
            'max_dd': round(max_dd, 0), 'avg': round(df['pnl'].mean(), 0)}


def main():
    print(f"DB: {DB_PATH}")
    con = duckdb.connect(DB_PATH, read_only=True)
    print("Loading spot daily + expiries ...")
    daily = load_spot_daily(con)
    wk = weekly_bars(daily)
    expiries = load_expiries(con)
    print(f"Spot days {len(daily)} ({daily.index[0].date()}->{daily.index[-1].date()}) | "
          f"weekly bars {len(wk)} | expiries {len(expiries)}")
    print(f"Window {START}->{END} | SL {SL_MULT}x credit | slippage {SLIPPAGE_PTS}pt/leg | "
          f"cost Rs{COST_RS_PER_LEG}/leg\n")

    OUT_DIR.mkdir(exist_ok=True)
    summary = {}
    for width in HEDGE_WIDTHS:
        tag = "FAITHFUL (50-wide)" if width == 50 else f"sweep width={width}"
        print(f"{'='*78}\n  HEDGE WIDTH {width}  [{tag}]\n{'='*78}")
        faithful, realistic, skips = run_for_width(con, daily, wk, expiries, width)
        sf = summarize("FAITHFUL  (hold-to-expiry, no costs)", faithful)
        sr = summarize("REALISTIC (intra-week SL + slippage + costs)", realistic)
        print(f"    skips: {skips}\n")
        summary[str(width)] = {'faithful': sf, 'realistic': sr, 'skips': skips}
        if width == 50:
            json.dump({'faithful': faithful, 'realistic': realistic},
                      open(OUT_DIR / "cpr_trades_w50.json", "w"), indent=1)
    # ── NAKED variant (same CPR signals/strikes, no hedge legs) ──
    print(f"{'#'*78}\n  NAKED (no hedge) — same signals/strikes, short legs only\n{'#'*78}")
    nf, nr, nskips = run_for_width(con, daily, wk, expiries, width=0, hedged=False)
    nsf = summarize("FAITHFUL  (hold-to-expiry, no costs)", nf)
    nsr = summarize("REALISTIC (intra-week SL + slippage + costs)", nr)
    # worst single trade = tail-risk proxy
    if nr:
        worst = min(nr, key=lambda t: t['pnl'])
        print(f"    worst single trade (realistic): Rs{worst['pnl']:,.0f} "
              f"[{worst['decision']} entry {worst['entry']} exp {worst['expiry']} {worst['reason']}]")
    if nf:
        worstf = min(nf, key=lambda t: t['pnl'])
        print(f"    worst single trade (faithful) : Rs{worstf['pnl']:,.0f} "
              f"[{worstf['decision']} entry {worstf['entry']}]")
    print(f"    skips: {nskips}\n")
    summary['naked'] = {'faithful': nsf, 'realistic': nsr, 'skips': nskips}
    json.dump({'faithful': nf, 'realistic': nr}, open(OUT_DIR / "cpr_trades_naked.json", "w"), indent=1)

    con.close()
    json.dump(summary, open(OUT_DIR / "cpr_summary.json", "w"), indent=2)
    print(f"Saved -> {OUT_DIR}/cpr_summary.json  &  cpr_trades_w50.json  &  cpr_trades_naked.json")


if __name__ == "__main__":
    main()
