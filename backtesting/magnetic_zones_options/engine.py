"""Parametrized Magnetic Zones options-selling backtest engine.

A "unit" is one credit position managed as a whole (entry credit, TP, SL, settle):
  * range_fade  -> one unit straddling both zones (short strangle / iron condor)
  * touch_fade  -> up to two independent units (CE side at R2, PE side at S2),
                   each opened only when spot enters that zone band

Each unit is reported in two modes (cpr_option_selling convention):
  * FAITHFUL  : hold to expiry (overnight) or square-off (intraday), settle at
                intrinsic / close. No SL/TP, no costs.
  * REALISTIC : intra-period TP (premium decay) / SL (loss >= sl_mult*credit),
                per-leg slippage and full Indian charges.

No look-ahead: every option fill uses option_price_at(ts) (at-or-before) and the
entry is taken at the bar at/after the entry time. One unit per zone-side per period.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from .costs import SLIPPAGE_PTS, leg_charges
from .data_loader import DataLoader, _ts, nearest_strike
from .zones import period_zones


@dataclass
class Config:
    entry: str            # 'range_fade' | 'touch_fade'
    structure: str        # 'naked' | 'hedged'
    timeframe: str        # 'daily_intraday' | 'weekly_overnight' | 'monthly_overnight'
    lots: int = 10
    hedge_width_pts: int = 200
    tp_pct: float = 0.50          # close when premium decays >= this fraction of credit
    sl_mult: float = 2.0          # close when loss >= sl_mult * credit
    entry_h: int = 9
    entry_m: int = 20
    squareoff_h: int = 15
    squareoff_m: int = 20
    checkpoint_h: int = 15        # overnight daily SL/TP checkpoint
    checkpoint_m: int = 15
    min_credit_pts: float = 3.0
    capital: float = 1e7

    @property
    def name(self) -> str:
        tf = {"daily_intraday": "D-intra", "weekly_overnight": "W-ovn",
              "monthly_overnight": "M-ovn"}[self.timeframe]
        return f"{self.entry}|{self.structure}|{tf}"

    @property
    def intraday(self) -> bool:
        return self.timeframe == "daily_intraday"


# ---------------------------------------------------------------------------
# Leg / unit helpers
# ---------------------------------------------------------------------------
def _build_legs(cfg: Config, expiry: date, side: str, zones: pd.Series, loader: DataLoader):
    """Return the leg list for a zone side ('CE' at R2, 'PE' at S2)."""
    if side == "CE":
        short_k = nearest_strike(zones["R2"])
        legs = [{"right": "CE", "side": "short", "strike": short_k}]
        if cfg.structure == "hedged":
            legs.append({"right": "CE", "side": "long", "strike": short_k + cfg.hedge_width_pts})
    else:  # PE at S2
        short_k = nearest_strike(zones["S2"])
        legs = [{"right": "PE", "side": "short", "strike": short_k}]
        if cfg.structure == "hedged":
            legs.append({"right": "PE", "side": "long", "strike": short_k - cfg.hedge_width_pts})
    for lg in legs:
        lg["symbol"] = loader.option_symbol(expiry, lg["strike"], lg["right"])
    return legs


def _entry_credit(loader: DataLoader, legs: list[dict], ts: int) -> float | None:
    """Net credit (points) and fills each leg's entry_px; None if any leg has no price."""
    credit = 0.0
    for lg in legs:
        px = loader.option_price_at(lg["symbol"], ts)
        if px is None:
            return None
        lg["entry_px"] = px
        credit += px if lg["side"] == "short" else -px
    return credit


def _value_at(loader: DataLoader, legs: list[dict], ts: int):
    """Cost-to-close (points) at ts and per-leg prices; None if any leg missing."""
    val, pxs = 0.0, []
    for lg in legs:
        px = loader.option_price_at(lg["symbol"], ts)
        if px is None:
            return None, None
        pxs.append(px)
        val += px if lg["side"] == "short" else -px
    return val, pxs


def _settle_value(legs: list[dict], spot_exp: float):
    """Intrinsic cost-to-close at expiry and per-leg intrinsic prices."""
    val, pxs = 0.0, []
    for lg in legs:
        if lg["right"] == "PE":
            intr = max(lg["strike"] - spot_exp, 0.0)
        else:
            intr = max(spot_exp - lg["strike"], 0.0)
        pxs.append(intr)
        val += intr if lg["side"] == "short" else -intr
    return val, pxs


def _find_touch(loader: DataLoader, level: float, half: float, days: list[date]):
    """First 1m spot bar (epoch) whose price enters [level-half, level+half]."""
    for d in days:
        s = loader.spot_1m_day(d)
        if s.empty:
            continue
        hit = s[(s["close"] >= level - half) & (s["close"] <= level + half)]
        if not hit.empty:
            return int(hit.iloc[0]["ts"]), d
    return None, None


# ---------------------------------------------------------------------------
# Exit resolution
# ---------------------------------------------------------------------------
def _exit_intraday(loader: DataLoader, cfg: Config, unit: dict, day: date):
    """REALISTIC intraday exit: first TP/SL on 1m grid, else square-off close."""
    legs = unit["legs"]
    entry_ts = unit["entry_ts"]
    sq_ts = _ts(day, cfg.squareoff_h, cfg.squareoff_m)
    # Per-leg 1m series within [entry_ts, sq_ts], aligned on a common minute grid.
    cols = {}
    for i, lg in enumerate(legs):
        s = loader.option_1m_day(lg["symbol"], day)
        s = s[(s.index >= entry_ts) & (s.index <= sq_ts)]
        cols[i] = s
    if any(s.empty for s in cols.values()):
        return None
    grid = sorted(set().union(*[set(s.index) for s in cols.values()]))
    aligned = {i: cols[i].reindex(grid).ffill() for i in cols}
    frame = pd.DataFrame(aligned).dropna()
    if frame.empty:
        return None
    credit = unit["credit"]
    tp_level = (1.0 - cfg.tp_pct) * credit          # exit_value at/below => target
    sl_level = (1.0 + cfg.sl_mult) * credit         # exit_value at/above => stop
    signs = [1 if lg["side"] == "short" else -1 for lg in legs]
    for _ts_, row in frame.iterrows():
        val = sum(signs[i] * row[i] for i in range(len(legs)))
        if val <= tp_level:
            return val, [row[i] for i in range(len(legs))], "TARGET", day
        if val >= sl_level:
            return val, [row[i] for i in range(len(legs))], "STOPLOSS", day
    last = frame.iloc[-1]
    val = sum(signs[i] * last[i] for i in range(len(legs)))
    return val, [last[i] for i in range(len(legs))], "SQUAREOFF", day


def _exit_overnight(loader: DataLoader, cfg: Config, unit: dict, expiry: date,
                    scan_days: list[date], spot_exp: float):
    """REALISTIC overnight exit: daily TP/SL checkpoints, else settle at expiry."""
    legs = unit["legs"]
    credit = unit["credit"]
    tp_level = (1.0 - cfg.tp_pct) * credit
    sl_level = (1.0 + cfg.sl_mult) * credit
    for d in scan_days:
        ts = _ts(d, cfg.checkpoint_h, cfg.checkpoint_m)
        val, pxs = _value_at(loader, legs, ts)
        if val is None:
            continue
        if val <= tp_level:
            return val, pxs, "TARGET", d
        if val >= sl_level:
            return val, pxs, "STOPLOSS", d
    val, pxs = _settle_value(legs, spot_exp)
    return val, pxs, "EXPIRY", expiry


# ---------------------------------------------------------------------------
# Per-unit P&L (both modes) -> two trade dicts
# ---------------------------------------------------------------------------
def _record(cfg: Config, unit: dict, expiry: date, lot: int,
            faithful_val, faithful_pxs, faithful_reason, faithful_day,
            real_val, real_pxs, real_reason, real_day):
    qty = lot * cfg.lots
    legs = unit["legs"]
    credit = unit["credit"]

    # FAITHFUL — no slippage, no costs.
    f_pts = credit - faithful_val
    faithful = {
        "side": unit["side"], "entry_day": unit["entry_day"].isoformat(),
        "expiry": expiry.isoformat(), "strikes": "/".join(str(l["strike"]) for l in legs),
        "credit": round(credit, 2), "exit_val": round(faithful_val, 2),
        "reason": faithful_reason, "exit_day": faithful_day.isoformat() if isinstance(faithful_day, date) else str(faithful_day),
        "pnl_pts": round(f_pts, 2), "pnl": round(f_pts * qty, 0), "win": f_pts > 0,
    }

    # REALISTIC — slippage on every fill + full charges.
    nlegs = len(legs)
    eff_credit = credit - SLIPPAGE_PTS * nlegs
    eff_exit = real_val + SLIPPAGE_PTS * nlegs
    r_pts = eff_credit - eff_exit
    charges = sum(
        leg_charges(lg["side"], lg["entry_px"], px, qty)
        for lg, px in zip(legs, real_pxs)
    )
    r_pnl = r_pts * qty - charges
    realistic = {
        "side": unit["side"], "entry_day": unit["entry_day"].isoformat(),
        "expiry": expiry.isoformat(), "strikes": "/".join(str(l["strike"]) for l in legs),
        "credit": round(credit, 2), "exit_val": round(real_val, 2),
        "reason": real_reason, "exit_day": real_day.isoformat() if isinstance(real_day, date) else str(real_day),
        "charges": round(charges, 0), "pnl_pts": round(r_pts, 2),
        "pnl": round(r_pnl, 0), "win": r_pnl > 0,
    }
    return faithful, realistic


# ---------------------------------------------------------------------------
# Period enumeration
# ---------------------------------------------------------------------------
def _periods(cfg: Config, loader: DataLoader, start: str, end: str):
    """Yield dicts: {zones, entry_day, expiry, exit_days, sides_days}.

    exit_days  = trading days used for overnight SL/TP checkpoints (entry+1..expiry-1).
    sides_days = trading days to scan for a touch (touch_fade).
    """
    td = loader.trading_days(start, end)
    td_set = set(td)
    daily = loader.daily_spot()
    start_d = pd.Timestamp(start).date()
    end_d = pd.Timestamp(end).date()

    if cfg.timeframe == "daily_intraday":
        zdf = period_zones(daily)
        for d in td:
            key = pd.Timestamp(d)
            if key not in zdf.index:
                continue
            expiry = loader.nearest_expiry(d)
            if expiry is None:
                continue
            yield {"zones": zdf.loc[key], "entry_day": d, "expiry": expiry,
                   "exit_days": [], "sides_days": [d]}

    elif cfg.timeframe == "weekly_overnight":
        zdf = period_zones(loader.weekly_bars())
        for expiry in loader.weekly_expiries:
            if not (start_d <= expiry <= end_d):
                continue
            monday = expiry - timedelta(days=expiry.weekday())
            key = pd.Timestamp(monday)
            if key not in zdf.index:
                continue
            entry_day = next((monday + timedelta(days=o) for o in range(7)
                              if (monday + timedelta(days=o)) < expiry
                              and (monday + timedelta(days=o)) in td_set), None)
            if entry_day is None:
                continue
            scan = [t for t in td if entry_day < t < expiry]
            touch = [t for t in td if entry_day <= t <= expiry]
            yield {"zones": zdf.loc[key], "entry_day": entry_day, "expiry": expiry,
                   "exit_days": scan, "sides_days": touch}

    else:  # monthly_overnight
        zdf = period_zones(loader.monthly_bars())
        for expiry in loader.monthly_expiries:
            if not (start_d <= expiry <= end_d):
                continue
            mstart = date(expiry.year, expiry.month, 1)
            key = pd.Timestamp(mstart)
            if key not in zdf.index:
                continue
            entry_day = next((mstart + timedelta(days=o) for o in range(20)
                              if (mstart + timedelta(days=o)) < expiry
                              and (mstart + timedelta(days=o)) in td_set), None)
            if entry_day is None:
                continue
            scan = [t for t in td if entry_day < t < expiry]
            touch = [t for t in td if entry_day <= t <= expiry]
            yield {"zones": zdf.loc[key], "entry_day": entry_day, "expiry": expiry,
                   "exit_days": scan, "sides_days": touch}


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------
def run(cfg: Config, loader: DataLoader, start: str, end: str):
    """Run one config. Returns (faithful_trades, realistic_trades, skips)."""
    faithful, realistic = [], []
    skips = {"no_premium": 0, "low_credit": 0, "no_touch": 0, "no_exit": 0, "no_spot_exp": 0}

    for p in _periods(cfg, loader, start, end):
        zones, entry_day, expiry = p["zones"], p["entry_day"], p["expiry"]
        lot = loader.lot_size(expiry)

        # Which zone sides to trade, and how a unit is grouped.
        if cfg.entry == "range_fade":
            unit_specs = [("BOTH", ["CE", "PE"])]
        else:  # touch_fade -> independent CE and PE units
            unit_specs = [("CE", ["CE"]), ("PE", ["PE"])]

        # Expiry spot for overnight settlement (once per period).
        spot_exp = None
        if not cfg.intraday:
            spot_exp = loader.spot_at(expiry, 15, 25) or loader.spot_eod(expiry)
            if spot_exp is None:
                skips["no_spot_exp"] += 1
                continue

        for unit_side, sides in unit_specs:
            # --- determine entry timestamp + legs ---
            legs: list[dict] = []
            if cfg.entry == "range_fade":
                entry_ts = _ts(entry_day, cfg.entry_h, cfg.entry_m)
                for s in sides:
                    legs += _build_legs(cfg, expiry, s, zones, loader)
                unit_entry_day = entry_day
            else:  # touch_fade single side: wait for the zone touch
                level = zones["R2"] if unit_side == "CE" else zones["S2"]
                t_ts, t_day = _find_touch(loader, level, zones["half"], p["sides_days"])
                if t_ts is None:
                    skips["no_touch"] += 1
                    continue
                entry_ts = t_ts
                unit_entry_day = t_day
                legs += _build_legs(cfg, expiry, unit_side, zones, loader)

            credit = _entry_credit(loader, legs, entry_ts)
            if credit is None:
                skips["no_premium"] += 1
                continue
            if credit < cfg.min_credit_pts:
                skips["low_credit"] += 1
                continue

            unit = {"side": unit_side, "legs": legs, "entry_ts": entry_ts,
                    "entry_day": unit_entry_day, "credit": credit}

            # --- resolve exits (both modes) ---
            if cfg.intraday:
                # FAITHFUL: exit at square-off close.
                sq_ts = _ts(unit_entry_day, cfg.squareoff_h, cfg.squareoff_m)
                f = _value_at(loader, legs, sq_ts)
                if f[0] is None:
                    f_val, f_pxs = _value_or_eod(loader, legs, unit_entry_day)
                    if f_val is None:
                        skips["no_exit"] += 1
                        continue
                    f_reason, f_day = "SQUAREOFF", unit_entry_day
                else:
                    f_val, f_pxs, f_reason, f_day = f[0], f[1], "SQUAREOFF", unit_entry_day
                # REALISTIC: TP/SL on 1m grid.
                r = _exit_intraday(loader, cfg, unit, unit_entry_day)
                if r is None:
                    r_val, r_pxs, r_reason, r_day = f_val, f_pxs, "SQUAREOFF", unit_entry_day
                else:
                    r_val, r_pxs, r_reason, r_day = r
            else:
                # Overnight scan days strictly after this unit's entry day.
                scan = [d for d in p["exit_days"] if d > unit_entry_day]
                f_val, f_pxs = _settle_value(legs, spot_exp)
                f_reason, f_day = "EXPIRY", expiry
                r_val, r_pxs, r_reason, r_day = _exit_overnight(
                    loader, cfg, unit, expiry, scan, spot_exp)

            ft, rt = _record(cfg, unit, expiry, lot,
                             f_val, f_pxs, f_reason, f_day,
                             r_val, r_pxs, r_reason, r_day)
            faithful.append(ft)
            realistic.append(rt)

    return faithful, realistic, skips


def _value_or_eod(loader: DataLoader, legs: list[dict], day: date):
    """Fallback close-out using each leg's last trade of the day."""
    val, pxs = 0.0, []
    for lg in legs:
        px = loader.option_eod(lg["symbol"], day)
        if px is None:
            return None, None
        pxs.append(px)
        val += px if lg["side"] == "short" else -px
    return val, pxs
