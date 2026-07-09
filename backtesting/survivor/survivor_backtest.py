#!/usr/bin/env python
"""
Survivor Options-Selling Strategy — Historical Backtest
=======================================================

Ports the *decision logic* of the live OpenAlgo "Survivor" strategy and drives it
with 5 years of 1-minute NIFTY data (dhanloader dataset). The live SDK calls
(quotes / instruments / placeorder / ordermargin / funds) are replaced by:

  - Driving price feed : data/INDEX_SPOT/NIFTY_clean.csv   (clean IST spot, 1-min)
  - Option premiums    : data/NIFTY/chunks/WEEK/1/<offset>/<CE|PE>/<cycle>.csv
                         indexed by ABSOLUTE strike (strike_abs), since the live
                         strategy picks strikes off the *current* spot while the
                         dataset's ATM offsets are pegged to a daily-fixed ATM.

Strategy behaviour (unchanged from the live script):
  - Sell an OTM PE when spot rises past `pe_gap` above the PE reference.
  - Sell an OTM CE when spot falls past `ce_gap` below the CE reference.
  - Strike chosen `*_symbol_gap` away from spot; walk closer while premium < min.
  - Multiplier scales qty when a single step jumps several gaps (capped).
  - Pull-back reset nudges a reference back toward spot after a trade.

Backtest model (per user decisions):
  - Carry overnight (NRML); hold to the WEEKLY expiry, then settle and AUTO-ROLL
    into the next weekly. Continuous 5-year run, equity accumulates.
  - Weekly-expiry days are DETECTED FROM THE DATA (front-weekly ATM premium
    collapses to ~0), not from a hand-kept calendar.
  - Net P&L: brokerage + STT/exchange/GST/stamp + slippage on entry/exit.

Outputs (written next to this file):
  survivor_trades.csv      per-leg trade log
  survivor_summary.json    aggregate metrics
  survivor_equity.html     equity curve + drawdown

Run:  uv run python backtesting/survivor/survivor_backtest.py
"""
from __future__ import annotations

import json
from datetime import time as dtime, date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ============================================================================
# PATHS
# ============================================================================
DATA_ROOT = Path("/Users/bond7/Desktop/Project/dhanloader/data/NIFTY/chunks")
SPOT_FILE = Path("/Users/bond7/Desktop/Project/dhanloader/data/INDEX_SPOT/NIFTY_clean.csv")
OUT_DIR = Path(__file__).resolve().parent

EXPIRY_FLAG = "WEEK"   # trade the near weekly
EXPIRY_CODE = "1"
OFFSETS = ["ATM"] + [f"ATMm{i}" for i in range(1, 11)] + [f"ATMp{i}" for i in range(1, 11)]
IST_OFFSET = pd.Timedelta(hours=5, minutes=30)   # option datetimes are UTC

# ============================================================================
# STRATEGY CONFIG  (ported verbatim from the live CONFIG; env-overridable for sweeps)
# ============================================================================
import os


def _envf(name, default):
    v = os.environ.get(name)
    return type(default)(v) if v is not None else default


PE_GAP = _envf("PE_GAP", 20)
CE_GAP = _envf("CE_GAP", 20)
PE_SYMBOL_GAP = _envf("PE_SYMBOL_GAP", 200)
CE_SYMBOL_GAP = _envf("CE_SYMBOL_GAP", 200)
# Position size is expressed in LOTS; the actual unit quantity uses the NIFTY
# lot size *in effect on the trade date* (SEBI changed it over the period).
PE_LOTS = _envf("PE_LOTS", 1)
CE_LOTS = _envf("CE_LOTS", 1)
_LOT_FIXED = int(os.environ["LOT_SIZE_FIXED"]) if os.environ.get("LOT_SIZE_FIXED") else 0


def nifty_lot_size(d: date) -> int:
    """NIFTY F&O lot size by SEBI timeline:
       25 (pre-20 Nov 2024) -> 75 (20 Nov 2024) -> 65 (1 Jan 2026).
       Set env LOT_SIZE_FIXED to force a flat lot (to reproduce older runs)."""
    if _LOT_FIXED:
        return _LOT_FIXED
    if d >= date(2026, 1, 1):
        return 65
    if d >= date(2024, 11, 20):
        return 75
    return 25
MIN_PRICE_TO_SELL = _envf("MIN_PRICE_TO_SELL", 15)
SELL_MULTIPLIER_THRESHOLD = _envf("SELL_MULTIPLIER_THRESHOLD", 5)
PE_RESET_GAP = 30
CE_RESET_GAP = 30
PE_START_POINT = 0      # 0 => use first observed spot
CE_START_POINT = 0
STRIKE_STEP = 50

# ============================================================================
# BACKTEST CONFIG
# ============================================================================
# Backtest date window (None = full history). Override via env START_DATE/END_DATE.
START_DATE = os.environ.get("START_DATE")   # e.g. "2024-06-01"
END_DATE = os.environ.get("END_DATE")        # e.g. "2026-06-01"

EXIT_MODE = "intrinsic"          # "intrinsic" (settle at expiry) | "time"
EXIT_TIME = dtime(15, 15)        # used only when EXIT_MODE == "time"
EXPIRY_ATM_THRESHOLD = 3.0       # front-weekly ATM close <= this at EOD => expiry day

# ---- RISK OVERLAY (0 = disabled) -------------------------------------------
# Per-leg stop-loss: buy back a short leg once its premium reaches MULT x the
# price it was sold at (caps the runaway leg in a sharp move).
STOP_LOSS_MULT = _envf("STOP_LOSS_MULT", 0.0)     # e.g. 2.0 = stop at 2x premium
# Daily kill-switch: if the day's P&L (realised + MTM) falls below -CAP, square
# off ALL open legs and stop selling for the rest of the session.
DAILY_LOSS_CAP = _envf("DAILY_LOSS_CAP", 0.0)     # Rs, e.g. 400000

# Costs (approximate Indian F&O option-selling charges; tweak as needed)
SLIPPAGE_PER_UNIT = 0.50         # Rs per option unit, each side
BROKERAGE_PER_ORDER = 20.0       # flat, per order (2 orders per round-trip leg)
STT_SELL_PCT = 0.001             # 0.10% on the sell-side premium value
EXCH_TXN_PCT = 0.0003503         # NSE options, both sides
SEBI_PCT = 0.000001              # Rs 10 / crore
STAMP_PCT = 0.00003              # buy side
GST_PCT = 0.18                   # on (brokerage + txn + sebi)
MARGIN_PCT = 0.12                # estimate: margin/short leg = spot*qty*MARGIN_PCT


def leg_charges(entry_value: float, exit_value: float) -> float:
    """Round-trip charges for one short leg (sell to open, buy to close)."""
    brokerage = 2 * BROKERAGE_PER_ORDER
    stt = STT_SELL_PCT * entry_value
    txn = EXCH_TXN_PCT * (entry_value + exit_value)
    sebi = SEBI_PCT * (entry_value + exit_value)
    stamp = STAMP_PCT * exit_value
    gst = GST_PCT * (brokerage + txn + sebi)
    return brokerage + stt + txn + sebi + stamp + gst


# ============================================================================
# DATA LOADING
# ============================================================================

def load_spot() -> pd.DataFrame:
    """Clean IST spot feed -> DataFrame[ts, spot, date, is_last_of_day]."""
    df = pd.read_csv(SPOT_FILE, usecols=["datetime", "close"])
    df["ts"] = pd.to_datetime(df["datetime"])
    df["spot"] = df["close"].astype(float)
    df["date"] = df["ts"].dt.date
    if START_DATE:
        df = df[df["ts"] >= pd.to_datetime(START_DATE)]
    if END_DATE:
        df = df[df["ts"] < pd.to_datetime(END_DATE)]
    df = df.sort_values("ts").reset_index(drop=True)
    # mark the last bar of each trading day (settlement point for intrinsic mode)
    df["is_last_of_day"] = df["date"] != df["date"].shift(-1)
    return df[["ts", "spot", "date", "is_last_of_day"]]


def list_cycles() -> list[str]:
    """Sorted WEEK/1 cycle names, e.g. '2021-06-01_2021-06-30'."""
    d = DATA_ROOT / EXPIRY_FLAG / EXPIRY_CODE / "ATM" / "CALL"
    return sorted(p.stem for p in d.glob("*.csv"))


def load_chunk(cycle: str):
    """
    Load one ~monthly chunk of WEEK/1 options.

    Returns:
      call_map : dict[ts] -> dict[int strike -> close]
      put_map  : dict[ts] -> dict[int strike -> close]
      expiry_dates : set[date] where the front-weekly ATM collapsed (expiry days)
    """
    call_frames, put_frames = [], []
    atm_call = atm_put = None
    for off in OFFSETS:
        for ot in ("CALL", "PUT"):
            fp = DATA_ROOT / EXPIRY_FLAG / EXPIRY_CODE / off / ot / f"{cycle}.csv"
            if not fp.exists():
                continue
            df = pd.read_csv(fp, usecols=["datetime", "close", "strike_abs"])
            df["ts"] = pd.to_datetime(df["datetime"]) + IST_OFFSET
            df["strike"] = df["strike_abs"].round().astype(int)
            df["close"] = df["close"].astype(float)
            (call_frames if ot == "CALL" else put_frames).append(df)
            if off == "ATM":
                if ot == "CALL":
                    atm_call = df
                else:
                    atm_put = df

    def build_map(frames):
        if not frames:
            return {}
        big = pd.concat(frames, ignore_index=True)
        return {ts: dict(zip(g["strike"], g["close"]))
                for ts, g in big.groupby("ts")}

    call_map = build_map(call_frames)
    put_map = build_map(put_frames)

    # detect expiry days: last bar of each date where ATM straddle leg ~ 0
    expiry_dates = set()
    if atm_call is not None and atm_put is not None:
        for df, label in ((atm_call, "c"), (atm_put, "p")):
            df["date"] = df["ts"].dt.date
        cl = atm_call.sort_values("ts").groupby(atm_call["ts"].dt.date)["close"].last()
        pl = atm_put.sort_values("ts").groupby(atm_put["ts"].dt.date)["close"].last()
        for d in cl.index:
            if min(cl.get(d, 1e9), pl.get(d, 1e9)) <= EXPIRY_ATM_THRESHOLD:
                expiry_dates.add(d)
    return call_map, put_map, expiry_dates


# ============================================================================
# STRIKE SELECTION  (ported from _find_option_strike)
# ============================================================================

def find_option_strike(option_type: str, spot: float, gap: float,
                       premium_map: dict[int, float]):
    """
    Find a sellable strike `gap` away from spot, walking CLOSER while the premium
    is below MIN_PRICE_TO_SELL or the strike is outside the available grid.
    Returns (strike:int, premium:float) or (None, 0.0).
    """
    temp_gap = gap
    while temp_gap > 0:
        if option_type == "PE":
            target = int(round((spot - temp_gap) / STRIKE_STEP) * STRIKE_STEP)
        else:
            target = int(round((spot + temp_gap) / STRIKE_STEP) * STRIKE_STEP)
        prem = premium_map.get(target)
        if prem is None:                      # strike outside stored grid
            temp_gap -= STRIKE_STEP
            continue
        if prem >= MIN_PRICE_TO_SELL:
            return target, prem
        temp_gap -= STRIKE_STEP                # premium too small, move closer
    return None, 0.0


# ============================================================================
# SIMULATION
# ============================================================================

class Survivor:
    def __init__(self):
        self.initialized = False
        self.current_lot_size = 75    # set per session from nifty_lot_size(date)
        self.pe_ref = 0.0
        self.ce_ref = 0.0
        self.pe_reset_flag = 0
        self.ce_reset_flag = 0

        self.open_positions: list[dict] = []
        self.trades: list[dict] = []
        self.realised_pnl = 0.0
        self.total_credit = 0.0
        self.total_charges = 0.0

        self.current_margin = 0.0
        self.peak_margin = 0.0
        self.max_concurrent = 0

        # risk overlay state
        self.day_anchor = None       # equity at session start (for kill-switch)
        self.halted = False          # kill-switch tripped for the day
        self.n_stops = 0             # legs closed by stop-loss
        self.n_kills = 0             # kill-switch trips

        self.equity_curve: list[tuple] = []   # (ts, equity)

    # ---- per-session re-anchor (mirrors live: state file resets daily) --
    def start_day(self, spot):
        """Re-anchor PE/CE references at each new session's first bar, exactly
        as the live strategy does (its date-keyed state file is ignored when not
        from today, so refs reinit to the current LTP every morning). Open
        positions are NOT touched — they carry overnight to weekly expiry."""
        self.pe_ref = PE_START_POINT or spot
        self.ce_ref = CE_START_POINT or spot
        self.pe_reset_flag = 0
        self.ce_reset_flag = 0
        self.initialized = True

    # ---- per-minute tick ------------------------------------------------
    def process_tick(self, ts, spot, call_at_ts, put_at_ts):
        self._handle_pe(ts, spot, put_at_ts)
        self._handle_ce(ts, spot, call_at_ts)
        self._reset_refs(spot)

    def _handle_pe(self, ts, spot, put_at_ts):
        if spot <= self.pe_ref:
            return
        diff = round(spot - self.pe_ref, 0)
        if diff <= PE_GAP:
            return
        mult = int(diff / PE_GAP)
        if mult > SELL_MULTIPLIER_THRESHOLD:
            return
        self.pe_ref += PE_GAP * mult
        qty = mult * PE_LOTS * self.current_lot_size
        strike, prem = find_option_strike("PE", spot, PE_SYMBOL_GAP, put_at_ts)
        if strike is not None:
            self._open_short("PE", strike, qty, prem, ts, spot)
            self.pe_reset_flag = 1

    def _handle_ce(self, ts, spot, call_at_ts):
        if spot >= self.ce_ref:
            return
        diff = round(self.ce_ref - spot, 0)
        if diff <= CE_GAP:
            return
        mult = int(diff / CE_GAP)
        if mult > SELL_MULTIPLIER_THRESHOLD:
            return
        self.ce_ref -= CE_GAP * mult
        qty = mult * CE_LOTS * self.current_lot_size
        strike, prem = find_option_strike("CE", spot, CE_SYMBOL_GAP, call_at_ts)
        if strike is not None:
            self._open_short("CE", strike, qty, prem, ts, spot)
            self.ce_reset_flag = 1

    def _reset_refs(self, spot):
        if (self.pe_ref - spot) > PE_RESET_GAP and self.pe_reset_flag:
            self.pe_ref = spot + PE_RESET_GAP
        if (spot - self.ce_ref) > CE_RESET_GAP and self.ce_reset_flag:
            self.ce_ref = spot - CE_RESET_GAP

    # ---- positions ------------------------------------------------------
    def _open_short(self, side, strike, qty, prem, ts, spot):
        fill = max(0.05, prem - SLIPPAGE_PER_UNIT)     # receive less on a sell
        margin = spot * qty * MARGIN_PCT
        self.open_positions.append({
            "side": side, "strike": strike, "qty": qty,
            "entry_ts": ts, "entry_prem": fill,
            "entry_value": fill * qty, "entry_spot": spot, "margin": margin,
        })
        self.total_credit += fill * qty
        self.current_margin += margin
        self.peak_margin = max(self.peak_margin, self.current_margin)
        self.max_concurrent = max(self.max_concurrent, len(self.open_positions))

    def _current_premium(self, pos, spot, call_at_ts, put_at_ts):
        """Live buy-back premium for a leg: market quote if in grid, else intrinsic."""
        m = call_at_ts if pos["side"] == "CE" else put_at_ts
        cur = m.get(pos["strike"])
        if cur is None:                                # outside grid -> intrinsic
            cur = (max(0.0, spot - pos["strike"]) if pos["side"] == "CE"
                   else max(0.0, pos["strike"] - spot))
        return cur

    def _close_leg(self, pos, exit_pu, ts, reason):
        exit_value = exit_pu * pos["qty"]
        charges = leg_charges(pos["entry_value"], exit_value)
        gross = (pos["entry_prem"] - exit_pu) * pos["qty"]
        net = gross - charges
        self.realised_pnl += net
        self.total_charges += charges
        self.current_margin -= pos["margin"]
        self.trades.append({
            "side": pos["side"], "strike": pos["strike"], "qty": pos["qty"],
            "entry_ts": pos["entry_ts"], "entry_prem": round(pos["entry_prem"], 2),
            "exit_ts": ts, "exit_prem": round(exit_pu, 2),
            "gross_pnl": round(gross, 2), "charges": round(charges, 2),
            "net_pnl": round(net, 2), "reason": reason,
            "margin": round(pos["margin"], 2), "entry_spot": round(pos["entry_spot"], 2),
        })

    def _exit_value_per_unit(self, pos, spot, call_at_ts, put_at_ts):
        """Per-unit buy-back value at expiry settlement."""
        if EXIT_MODE == "time":
            m = call_at_ts if pos["side"] == "CE" else put_at_ts
            v = m.get(pos["strike"])
            if v is not None:
                return v + SLIPPAGE_PER_UNIT           # pay more on a buy
        if pos["side"] == "CE":                        # intrinsic settlement
            return max(0.0, spot - pos["strike"])
        return max(0.0, pos["strike"] - spot)

    def settle_all(self, ts, spot, call_at_ts, put_at_ts):
        for pos in self.open_positions:
            exit_pu = self._exit_value_per_unit(pos, spot, call_at_ts, put_at_ts)
            self._close_leg(pos, exit_pu, ts, "expiry")
        self.open_positions.clear()
        self.current_margin = 0.0

    # ---- risk overlay: per-leg stop-loss + daily kill-switch ------------
    def risk_overlay(self, ts, spot, call_at_ts, put_at_ts, new_day):
        """Run stop-loss + kill-switch. Returns True if new selling is allowed."""
        if new_day:
            self.halted = False
            self.day_anchor = None

        # one pass: mark to market and collect stop-loss hits
        unreal = 0.0
        stops = []
        for pos in self.open_positions:
            cur = self._current_premium(pos, spot, call_at_ts, put_at_ts)
            unreal += (pos["entry_prem"] - cur) * pos["qty"]
            if STOP_LOSS_MULT and cur >= STOP_LOSS_MULT * pos["entry_prem"]:
                stops.append((pos, cur))

        for pos, cur in stops:                         # execute stop-losses
            unreal -= (pos["entry_prem"] - cur) * pos["qty"]
            self._close_leg(pos, cur + SLIPPAGE_PER_UNIT, ts, "stop")
            self.open_positions.remove(pos)
            self.n_stops += 1

        equity_now = self.realised_pnl + unreal
        if self.day_anchor is None:
            self.day_anchor = equity_now

        # daily kill-switch: flatten everything and stop selling for the day
        if (DAILY_LOSS_CAP and not self.halted
                and equity_now - self.day_anchor <= -DAILY_LOSS_CAP):
            for pos in list(self.open_positions):
                cur = self._current_premium(pos, spot, call_at_ts, put_at_ts)
                self._close_leg(pos, cur + SLIPPAGE_PER_UNIT, ts, "kill")
            self.open_positions.clear()
            self.halted = True
            self.n_kills += 1

        return not self.halted

    # ---- mark to market for the equity curve ----------------------------
    def mtm(self, spot, call_at_ts, put_at_ts):
        unreal = sum((pos["entry_prem"]
                      - self._current_premium(pos, spot, call_at_ts, put_at_ts))
                     * pos["qty"] for pos in self.open_positions)
        return self.realised_pnl + unreal


# ============================================================================
# DRIVER
# ============================================================================

def run():
    print("Loading spot feed...")
    spot_df = load_spot()
    cycles = list_cycles()
    print(f"Spot bars: {len(spot_df):,} | option cycles: {len(cycles)}")

    sim = Survivor()
    prev_date = None

    n_expiries = 0
    for ci, cycle in enumerate(cycles, 1):
        start_s, end_s = cycle.split("_")
        start_d = pd.to_datetime(start_s).date()
        end_d = pd.to_datetime(end_s).date()

        # skip chunks entirely outside the date window
        if START_DATE and end_d < pd.to_datetime(START_DATE).date():
            continue
        if END_DATE and start_d >= pd.to_datetime(END_DATE).date():
            continue

        call_map, put_map, expiry_dates = load_chunk(cycle)
        n_expiries += len(expiry_dates)

        # spot rows whose date falls in this chunk's range, in order
        chunk_spot = spot_df[(spot_df["date"] >= start_d) & (spot_df["date"] <= end_d)]
        empty_map: dict[int, float] = {}

        for row in chunk_spot.itertuples(index=False):
            ts, spot, date, is_last = row.ts, row.spot, row.date, row.is_last_of_day
            call_at = call_map.get(ts, empty_map)
            put_at = put_map.get(ts, empty_map)
            if not call_at and not put_at:
                continue                               # no option data this minute

            new_day = date != prev_date
            if new_day:                                # new session -> re-anchor refs
                sim.start_day(spot)
                sim.current_lot_size = nifty_lot_size(date)   # SEBI lot-size timeline
                prev_date = date

            # risk overlay (stop-loss + kill-switch) runs every bar, before new sells
            allow_new = sim.risk_overlay(ts, spot, call_at, put_at, new_day)
            if allow_new:
                sim.process_tick(ts, spot, call_at, put_at)

            is_expiry = date in expiry_dates
            if EXIT_MODE == "time" and is_expiry and ts.time() >= EXIT_TIME \
                    and sim.open_positions:
                sim.settle_all(ts, spot, call_at, put_at)
            elif EXIT_MODE == "intrinsic" and is_expiry and is_last \
                    and sim.open_positions:
                sim.settle_all(ts, spot, call_at, put_at)

            if is_last:
                sim.equity_curve.append(
                    (ts, sim.mtm(spot, call_at, put_at), sim.current_margin))

        print(f"  [{ci:>2}/{len(cycles)}] {cycle}  "
              f"expiries={len(expiry_dates)}  trades={len(sim.trades):,}  "
              f"realised=Rs {sim.realised_pnl:,.0f}")

    # any positions still open at the very end -> settle at last seen state
    if sim.open_positions and sim.equity_curve:
        last_ts = sim.equity_curve[-1][0]
        last_row = spot_df.iloc[-1]
        sim.settle_all(last_ts, float(last_row["spot"]), {}, {})

    _report(sim, n_expiries, len(cycles))
    return sim


def _report(sim: Survivor, n_expiries: int, n_cycles: int):
    trades = pd.DataFrame(sim.trades)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    trades_path = OUT_DIR / "survivor_trades.csv"
    trades.to_csv(trades_path, index=False)

    # daily equity series (one row per trading day) for downstream analytics
    if sim.equity_curve:
        pd.DataFrame(sim.equity_curve, columns=["ts", "equity", "margin"]) \
            .to_csv(OUT_DIR / "survivor_equity.csv", index=False)

    wins = int((trades["net_pnl"] > 0).sum()) if not trades.empty else 0
    losses = int((trades["net_pnl"] <= 0).sum()) if not trades.empty else 0
    eq = pd.DataFrame(sim.equity_curve, columns=["ts", "equity", "margin"]) \
        if sim.equity_curve else pd.DataFrame(columns=["ts", "equity", "margin"])
    if not eq.empty:
        max_dd = float((eq["equity"] - eq["equity"].cummax()).min())
        avg_margin = float(eq.loc[eq["margin"] > 0, "margin"].mean() or 0)
        years = max((eq["ts"].iloc[-1] - eq["ts"].iloc[0]).days / 365.25, 1e-9)
    else:
        max_dd = avg_margin = 0.0
        years = 1.0

    annual_pnl = sim.realised_pnl / years
    summary = {
        "scope": f"WEEK/{EXPIRY_CODE} NIFTY, exit={EXIT_MODE}",
        "period_years": round(years, 2),
        "cycles_files": n_cycles,
        "weekly_expiries_detected": n_expiries,
        "total_legs": len(sim.trades),
        "winning_legs": wins,
        "losing_legs": losses,
        "win_rate_pct": round(100 * wins / len(sim.trades), 2) if sim.trades else 0,
        "total_credit_collected": round(sim.total_credit, 2),
        "total_charges": round(sim.total_charges, 2),
        "realised_net_pnl": round(sim.realised_pnl, 2),
        "annualised_net_pnl": round(annual_pnl, 2),
        "avg_margin_estimate": round(avg_margin, 2),
        "peak_margin_estimate": round(sim.peak_margin, 2),
        "max_concurrent_legs": sim.max_concurrent,
        "stop_loss_mult": STOP_LOSS_MULT,
        "daily_loss_cap": DAILY_LOSS_CAP,
        "legs_stopped_out": sim.n_stops,
        "killswitch_trips": sim.n_kills,
        "annual_return_on_avg_margin_pct": round(
            100 * annual_pnl / avg_margin, 2) if avg_margin else 0,
        "max_drawdown": round(max_dd, 2),
        "model_notes": (
            "Carry-overnight (NRML) to weekly expiry, auto-roll to next weekly. "
            "PE/CE references re-anchor to spot each session (mirrors the live "
            "date-keyed state reset) — required on 1-min bars, where the live 10s "
            "multiplier cap would otherwise strand a reference permanently. "
            "Margin and charges are estimates; no live broker in backtest."
        ),
    }
    with open(OUT_DIR / "survivor_summary.json", "w") as f:
        json.dump(summary, f, indent=2, default=str)

    # equity + drawdown chart
    if not eq.empty:
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                            row_heights=[0.7, 0.3], vertical_spacing=0.05,
                            subplot_titles=("Equity (cumulative net P&L, Rs)",
                                            "Drawdown (Rs)"))
        fig.add_trace(go.Scatter(x=eq["ts"], y=eq["equity"], name="Equity",
                                line=dict(color="#1f77b4")), row=1, col=1)
        fig.add_trace(go.Scatter(x=eq["ts"], y=eq["equity"] - eq["equity"].cummax(),
                                name="Drawdown", fill="tozeroy",
                                line=dict(color="#d62728")), row=2, col=1)
        fig.update_layout(title="Survivor Strategy — Backtest Equity",
                        height=720, showlegend=False)
        fig.write_html(OUT_DIR / "survivor_equity.html")

    print("\n" + "=" * 64)
    print("  SURVIVOR BACKTEST — SUMMARY")
    print("=" * 64)
    for k, v in summary.items():
        print(f"  {k:<28}: {v}")
    print("=" * 64)
    print(f"  trades  -> {trades_path}")
    print(f"  summary -> {OUT_DIR / 'survivor_summary.json'}")
    print(f"  equity  -> {OUT_DIR / 'survivor_equity.html'}")
    print("=" * 64)


if __name__ == "__main__":
    run()
