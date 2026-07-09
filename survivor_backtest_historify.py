#!/usr/bin/env python
"""
Survivor Options Strategy — Full Backtest Engine
=================================================
Adapted for historify.duckdb schema.

SCHEMA ASSUMPTIONS:
  market_data table
    symbol, exchange, interval, timestamp (Unix epoch seconds),
    open, high, low, close, volume, oi

  NIFTY spot  → symbol='NIFTY', exchange='NSE_INDEX', interval='1m'
  NIFTY opts  → exchange='NFO',  interval='1m',  symbol like 'NIFTY13APR2624150PE'

  expired_fno_contracts table
    openalgo_symbol, trading_symbol, expiry_date,
    contract_type, strike_price, lot_size

DESIGN PRINCIPLES (no look-ahead bias):
  - Data is loaded per-day, per-symbol. Each minute only sees prices
    up to and including that minute's close.
  - Signal triggers at minute M → execution price = close of minute M
    (market-order assumption; change to open of M+1 if preferred).
  - Option prices are looked up from DuckDB at the exact trigger timestamp.
  - Positions are updated with market prices from actual DB data, never
    future prices.

EXPIRY HANDLING:
  - On expiry day (Thursday): open positions for the expiring contract
    are settled at the last traded price of that day (≈ 15:29 candle).
  - Any NEW signals on an expiry day use the NEXT week's expiry symbols.
  - symbol prefixes are auto-generated from the expiry date — no manual
    config needed per expiry.

CAPITAL TRACKING:
  - Margin per trade is estimated using a simplified SPAN formula.
  - Cumulative margin, peak concurrent margin, and per-expiry peak margin
    are all tracked.
  - Three JSON output files per run:
      survivor_backtest_results/trades.json
      survivor_backtest_results/daily_stats.json
      survivor_backtest_results/expiry_stats.json

USAGE:
  Edit BACKTEST_START, BACKTEST_END, DB_PATH, and STRATEGY below, then:
      python survivor_backtest_historify.py
"""

import duckdb
import json
import pandas as pd
from datetime import datetime, date, timedelta
from pathlib import Path
from collections import defaultdict
import sys

import warnings

warnings.filterwarnings("ignore")

# =============================================================================
# ① BACKTEST DATE RANGE
# =============================================================================

BACKTEST_START = "2024-10-01"
BACKTEST_END   = "2026-04-16"

# =============================================================================
# ② DATABASE CONFIG
# =============================================================================

DB_PATH = "db/historify.duckdb"

# =============================================================================
# ③ STRATEGY PARAMETERS
# =============================================================================

STRATEGY = {
    "pe_gap":                    20,
    "ce_gap":                    20,
    "pe_symbol_gap":            200,   # PE strike = spot − 200
    "ce_symbol_gap":            200,   # CE strike = spot + 200
    "pe_quantity":               75,   # qty per multiplier step (1 lot)
    "ce_quantity":               75,
    "min_price_to_sell":         15,
    "sell_multiplier_threshold":  5,
    "pe_reset_gap":              30,
    "ce_reset_gap":              30,
    "pe_start_point":             0,   # 0 = use opening price
    "ce_start_point":             0,
    "lot_size":                  75,   # NIFTY lot size fallback (DB-fetched per expiry)
    "strike_interval":           50,   # NIFTY strike interval in points
}

# =============================================================================
# ④ MARGIN ESTIMATION
#
#   Formula: margin = (span_pct + exposure_pct) × notional
#   where   notional = spot × lot_size × num_lots
#   OTM benefit: margin is reduced slightly for strikes farther from spot.
#
#   At NIFTY 24000, lot_size 75, qty 75 (1 lot):
#     notional   = 24000 × 75 = Rs 18,00,000
#     5.5% margin = Rs 99,000 per lot  ← reasonable for NRML
# =============================================================================

MARGIN_CFG = {
    "span_pct":        0.040,   # 4.0%  SPAN
    "exposure_pct":    0.015,   # 1.5%  exposure
    "otm_max_benefit": 0.25,    # max 25% reduction for deep OTM strikes
}

RESULTS_DIR = "survivor_backtest_results"

# =============================================================================
# ⑤ BAR-COUNT FILTER FOR OPTION CANDLES
#
#   WHY options get skipped:
#     Brokers / data feeds only write a 1-min candle when there is actual
#     trading activity in that minute.  OTM strikes, newly-listed contracts,
#     and near-expiry options all have gaps — so their bar count is naturally
#     below 375 even though valid price data exists.
#
#   The old code used  bar_count = 375  (exact match) which filtered out
#   almost every 2026 contract.
#
#   The new code uses  bar_count >= MIN_OPTION_BARS  (minimum threshold).
#     • Set to 1  → accept any option that traded at least once that day.
#                   Entry price is still guarded by min_price_to_sell (≥15),
#                   so truly illiquid options with no price get skipped anyway.
#     • Set to 50 → tighter: option must have traded at least 50 minutes.
#     • Set to 375 → original strict behaviour (not recommended).
#
#   Recommended: 1  (the min_price_to_sell check is the real quality gate)
# =============================================================================

MIN_OPTION_BARS = 1   # minimum candles an option must have on a given day


# =============================================================================
# HELPERS
# =============================================================================

def estimate_margin(spot: float, strike: int, qty: int, lot_size: int) -> float:
    """Simplified SPAN margin for a naked short NIFTY option."""
    num_lots  = qty / lot_size
    notional  = spot * lot_size * num_lots
    base_pct  = MARGIN_CFG["span_pct"] + MARGIN_CFG["exposure_pct"]
    otm_dist  = abs(strike - spot) / spot
    otm_disc  = min(MARGIN_CFG["otm_max_benefit"], otm_dist * 3)
    return round(notional * base_pct * (1.0 - otm_disc), 2)


def expiry_to_prefix(expiry: date) -> str:
    """e.g. date(2024,4,11) → 'NIFTY11APR24'"""
    return f"NIFTY{expiry.strftime('%d%b%y').upper()}"


def round_to_strike(price: float, interval: int = 50) -> int:
    return int(round(price / interval) * interval)


def build_option_symbol(expiry: date, strike: int, opt_type: str) -> str:
    """e.g. 'NIFTY11APR2424000CE'"""
    return f"{expiry_to_prefix(expiry)}{strike}{opt_type}"


def ts_to_epoch(ts: pd.Timestamp) -> int:
    """Convert pandas Timestamp to Unix epoch seconds (integer)."""
    return int(ts.timestamp())


# =============================================================================
# DATABASE LAYER  —  hardcoded for historify.duckdb schema
# =============================================================================

class DataLoader:
    """
    Thin read-only wrapper around historify.duckdb.

    Key schema facts baked in:
      - table:     market_data
      - time col:  timestamp  (Unix epoch seconds, INTEGER)
      - spot:      symbol='NIFTY', exchange='NSE_INDEX', interval='1m'
      - options:   exchange='NFO', interval='1m'
    """

    def __init__(self, db_path: str):
        self.conn = duckdb.connect(db_path, read_only=True)
        self._lot_size_cache: dict = {}         # (expiry, contract_type) → lot_size
        self._valid_days_cache: set | None = None
        self._all_expiries: list[date] = self._load_all_expiries()
        print(f"[DB] Loaded {len(self._all_expiries)} unique expiries from DB.")
        if self._all_expiries:
            print(f"[DB] First 3 expiries: {self._all_expiries[:3]}")
            print(f"[DB] Last 3 expiries: {self._all_expiries[-3:]}")

    def _load_all_expiries(self) -> list[date]:
        """Fetch all unique NIFTY expiry dates from the DB."""
        df = self.conn.execute("""
            SELECT DISTINCT expiry_date
            FROM expired_fno_contracts
            WHERE upstox_key = 'NSE_INDEX|Nifty 50'
            ORDER BY expiry_date
        """).fetchdf()
        return [d.date() for d in pd.to_datetime(df["expiry_date"])]

    def get_nifty_weekly_expiry(self, ref_date: date) -> date:
        """Return the nearest expiry date from DB >= ref_date."""
        for exp in self._all_expiries:
            if exp >= ref_date:
                return exp
        # Fallback to Thursday logic if DB is empty
        days_ahead = (3 - ref_date.weekday()) % 7
        return ref_date if days_ahead == 0 else ref_date + timedelta(days=days_ahead)

    def get_next_expiry(self, current_expiry: date) -> date:
        """Return the first expiry from DB > current_expiry."""
        for exp in self._all_expiries:
            if exp > current_expiry:
                return exp
        # Fallback
        return self.get_nifty_weekly_expiry(current_expiry + timedelta(days=1))

    # ------------------------------------------------------------------
    # Schema sanity check
    # ------------------------------------------------------------------

    def schema_check(self):
        print("\n=== DB SCHEMA CHECK ===")
        tables = self.conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main'"
        ).fetchdf()
        print(f"Tables: {tables['table_name'].tolist()}")

        for t in ["market_data", "expired_fno_contracts"]:
            cols = self.conn.execute(
                f"SELECT column_name, data_type FROM information_schema.columns "
                f"WHERE table_name='{t}'"
            ).fetchdf()
            print(f"\nColumns in '{t}':\n{cols.to_string(index=False)}")

        sample = self.conn.execute(
            "SELECT DISTINCT symbol, exchange, interval FROM market_data LIMIT 20"
        ).fetchdf()
        print(f"\nSample (symbol, exchange, interval):\n{sample.to_string(index=False)}")
        print("======================\n")

    # ------------------------------------------------------------------
    # Trading calendar
    # ------------------------------------------------------------------

    def get_trading_dates(self, start: str, end: str) -> list:
        """All days with NIFTY spot data in [start, end]."""
        df = self.conn.execute(f"""
            SELECT DISTINCT DATE(to_timestamp(timestamp)) AS dt
            FROM market_data
            WHERE symbol   = 'NIFTY'
              AND exchange  = 'NSE_INDEX'
              AND interval  = '1m'
              AND DATE(to_timestamp(timestamp)) BETWEEN '{start}' AND '{end}'
            ORDER BY dt
        """).fetchdf()
        return [d.date() for d in pd.to_datetime(df["dt"])]

    # ------------------------------------------------------------------
    # Spot (index) data
    # ------------------------------------------------------------------

    def load_spot_day(self, day: date) -> pd.DataFrame:
        """All 1-min candles for NIFTY spot on a given day, ascending."""
        df = self.conn.execute(f"""
            SELECT
                to_timestamp(timestamp) AS ts,
                open,
                close
            FROM market_data
            WHERE symbol   = 'NIFTY'
              AND exchange  = 'NSE_INDEX'
              AND interval  = '1m'
              AND DATE(to_timestamp(timestamp)) = '{day}'
            ORDER BY timestamp
        """).fetchdf()
        df["ts"] = pd.to_datetime(df["ts"])
        return df

    # ------------------------------------------------------------------
    # Option data
    # ------------------------------------------------------------------

    def load_option_day(self, symbol: str, day: date) -> pd.DataFrame:
        """All 1-min closes for an option on a given day, indexed by ts."""
        df = self.conn.execute(f"""
            SELECT
                to_timestamp(timestamp) AS ts,
                close
            FROM market_data
            WHERE symbol   = '{symbol}'
              AND exchange  = 'NFO'
              AND interval  = '1m'
              AND DATE(to_timestamp(timestamp)) = '{day}'
            ORDER BY timestamp
        """).fetchdf()
        if df.empty:
            return df
        df["ts"] = pd.to_datetime(df["ts"])
        return df.set_index("ts")

    def get_price_at_or_before(self, symbol: str, ts: pd.Timestamp) -> float:
        """
        Last available close for `symbol` up to and including `ts`.
        Uses epoch integer comparison — no look-ahead.
        """
        epoch = ts_to_epoch(ts)
        day   = ts.date()
        result = self.conn.execute(f"""
            SELECT close
            FROM market_data
            WHERE symbol    = '{symbol}'
              AND exchange   = 'NFO'
              AND interval   = '1m'
              AND timestamp  <= {epoch}
              AND DATE(to_timestamp(timestamp)) = '{day}'
            ORDER BY timestamp DESC
            LIMIT 1
        """).fetchone()
        return float(result[0]) if result else 0.0

    def get_last_price_of_day(self, symbol: str, day: date) -> float:
        """Last traded price for expiry settlement (or EOB close)."""
        # Try NFO first (option), then NSE_INDEX (spot fallback)
        for exchange in ("NFO", "NSE_INDEX"):
            result = self.conn.execute(f"""
                SELECT close
                FROM market_data
                WHERE symbol   = '{symbol}'
                  AND exchange  = '{exchange}'
                  AND interval  = '1m'
                  AND DATE(to_timestamp(timestamp)) = '{day}'
                ORDER BY timestamp DESC
                LIMIT 1
            """).fetchone()
            if result:
                return float(result[0])
        return 0.0

    # ------------------------------------------------------------------
    # Metadata from expired_fno_contracts
    # ------------------------------------------------------------------

    def get_lot_size(self, expiry: date, contract_type: str = "CE") -> int:
        """
        Fetch lot size for a given NIFTY expiry from expired_fno_contracts.
        Falls back to STRATEGY['lot_size'] if not found.
        """
        key = (expiry, contract_type)
        if key in self._lot_size_cache:
            return self._lot_size_cache[key]

        result = self.conn.execute(f"""
            SELECT lot_size
            FROM expired_fno_contracts
            WHERE openalgo_symbol = 'NIFTY'
              AND expiry_date     = '{expiry}'
              AND contract_type   = '{contract_type}'
            LIMIT 1
        """).fetchone()

        lot = int(result[0]) if result else STRATEGY["lot_size"]
        self._lot_size_cache[key] = lot
        return lot

    # ------------------------------------------------------------------
    # Valid contract-day cache (375 bars per symbol per day)
    # ------------------------------------------------------------------

    def load_valid_option_days(self, start: str, end: str):
        """
        Populate cache of (symbol, date_str) pairs that have at least
        MIN_OPTION_BARS candles on that day.

        WHY NOT 375?
          Brokers only write a candle when a trade occurs in that minute.
          Illiquid / newly-listed strikes naturally have fewer bars even when
          real price data exists.  The old exact-match (= 375) was filtering
          out virtually all 2026 contracts.

          With MIN_OPTION_BARS = 1, any strike that traded at least once is
          accepted.  The  min_price_to_sell  guard (default ≥15) then rejects
          strikes that have no meaningful premium.
        """
        print(
            f"[DB] Loading valid option contract-days "
            f"(>= {MIN_OPTION_BARS} bars per symbol per day)…",
            flush=True,
        )
        df = self.conn.execute(f"""
            WITH cd AS (
                SELECT
                    symbol,
                    DATE(to_timestamp(timestamp)) AS trade_date,
                    COUNT(*)                       AS bar_count
                FROM market_data
                WHERE exchange = 'NFO'
                  AND interval = '1m'
                  AND (symbol LIKE 'NIFTY%CE' OR symbol LIKE 'NIFTY%PE')
                  AND DATE(to_timestamp(timestamp))
                        BETWEEN '{start}' AND '{end}'
                GROUP BY 1, 2
            )
            SELECT symbol, CAST(trade_date AS VARCHAR) AS trade_date
            FROM cd
            WHERE bar_count >= {MIN_OPTION_BARS}
        """).fetchdf()
        self._valid_days_cache = set(zip(df["symbol"], df["trade_date"]))
        print(
            f"[DB] {len(self._valid_days_cache):,} valid contract-day entries loaded.\n",
            flush=True,
        )

    def is_valid_option_day(self, symbol: str, day: date) -> bool:
        """True if this symbol has >= MIN_OPTION_BARS candles on `day`."""
        if self._valid_days_cache is None:
            return True   # cache not loaded — skip check
        return (symbol, str(day)) in self._valid_days_cache


# =============================================================================
# POSITION
# =============================================================================

class Position:
    """One naked short option trade, from entry to close."""

    __slots__ = [
        "symbol", "option_type", "strike", "expiry",
        "qty", "lot_size", "multiplier",
        "entry_time", "entry_price", "nifty_at_entry",
        "margin_used",
        "current_price", "unrealised_pnl",
        "exit_time", "exit_price", "realised_pnl", "exit_reason",
    ]

    def __init__(self, *, symbol, option_type, strike, expiry,
                 qty, lot_size, multiplier, entry_time, entry_price,
                 nifty_at_entry, margin_used):
        self.symbol         = symbol
        self.option_type    = option_type
        self.strike         = strike
        self.expiry         = expiry
        self.qty            = qty
        self.lot_size       = lot_size
        self.multiplier     = multiplier
        self.entry_time     = entry_time
        self.entry_price    = entry_price
        self.nifty_at_entry = nifty_at_entry
        self.margin_used    = margin_used
        self.current_price  = entry_price
        self.unrealised_pnl = 0.0
        self.exit_time      = None
        self.exit_price     = None
        self.realised_pnl   = None
        self.exit_reason    = None

    def update_pnl(self, price: float):
        self.current_price  = price
        self.unrealised_pnl = (self.entry_price - price) * self.qty

    def close(self, exit_price: float, exit_time, reason: str):
        self.exit_price    = exit_price
        self.exit_time     = exit_time
        self.realised_pnl  = (self.entry_price - exit_price) * self.qty
        self.exit_reason   = reason
        self.unrealised_pnl = 0.0

    def to_dict(self) -> dict:
        return {
            "symbol":          self.symbol,
            "option_type":     self.option_type,
            "strike":          self.strike,
            "expiry":          str(self.expiry),
            "qty":             self.qty,
            "lots":            self.qty // self.lot_size,
            "multiplier":      self.multiplier,
            "entry_time":      str(self.entry_time),
            "entry_price":     round(self.entry_price, 2),
            "nifty_at_entry":  round(self.nifty_at_entry, 2),
            "margin_used":     round(self.margin_used, 2),
            "credit_received": round(self.entry_price * self.qty, 2),
            "exit_time":       str(self.exit_time) if self.exit_time else None,
            "exit_price":      round(self.exit_price, 2) if self.exit_price is not None else None,
            "realised_pnl":    round(self.realised_pnl, 2) if self.realised_pnl is not None else None,
            "exit_reason":     self.exit_reason,
        }


# =============================================================================
# SURVIVOR SIGNAL STATE  (identical logic to the live script)
# =============================================================================

class SurvivorState:
    """
    Tracks PE/CE reference values and emits signals.
    Created fresh each day — each day starts with a clean reference = opening price.
    """

    def __init__(self, cfg: dict, initial_price: float):
        self.cfg           = cfg
        self.pe_ref        = cfg["pe_start_point"] or initial_price
        self.ce_ref        = cfg["ce_start_point"] or initial_price
        self.pe_reset_flag = 0
        self.ce_reset_flag = 0

    # --- PE (sell when NIFTY rises) ---

    def check_pe(self, price: float):
        """Returns (signal: bool, multiplier: int)"""
        if price <= self.pe_ref:
            return False, 0
        diff = round(price - self.pe_ref, 0)
        if diff <= self.cfg["pe_gap"]:
            return False, 0
        mult = int(diff / self.cfg["pe_gap"])
        if mult > self.cfg["sell_multiplier_threshold"]:
            return False, 0
        return True, mult

    def confirm_pe(self, mult: int):
        self.pe_ref       += self.cfg["pe_gap"] * mult
        self.pe_reset_flag = 1

    # --- CE (sell when NIFTY falls) ---

    def check_ce(self, price: float):
        if price >= self.ce_ref:
            return False, 0
        diff = round(self.ce_ref - price, 0)
        if diff <= self.cfg["ce_gap"]:
            return False, 0
        mult = int(diff / self.cfg["ce_gap"])
        if mult > self.cfg["sell_multiplier_threshold"]:
            return False, 0
        return True, mult

    def confirm_ce(self, mult: int):
        self.ce_ref       -= self.cfg["ce_gap"] * mult
        self.ce_reset_flag = 1

    # --- Resets ---

    def apply_resets(self, price: float):
        if (self.pe_ref - price) > self.cfg["pe_reset_gap"] and self.pe_reset_flag:
            self.pe_ref = price + self.cfg["pe_reset_gap"]
        if (price - self.ce_ref) > self.cfg["ce_reset_gap"] and self.ce_reset_flag:
            self.ce_ref = price - self.cfg["ce_reset_gap"]


# =============================================================================
# BACKTEST ENGINE
# =============================================================================

class BacktestEngine:

    def __init__(self):
        self.loader = DataLoader(DB_PATH)
        self.scfg   = STRATEGY

        self.open_positions:   list = []
        self.closed_positions: list = []

        self.daily_stats:  list = []
        self.expiry_stats: dict = defaultdict(lambda: {
            "expiry":          None,
            "trades":          0,
            "total_credit":    0.0,
            "total_margin_in": 0.0,
            "peak_margin":     0.0,
            "realised_pnl":    0.0,
        })

        self.total_realised_pnl     = 0.0
        self.peak_concurrent_margin = 0.0

    # ------------------------------------------------------------------
    # PUBLIC: run
    # ------------------------------------------------------------------

    def run(self):
        # Pre-load valid option days (375-bar filter) for the full period
        self.loader.load_valid_option_days(BACKTEST_START, BACKTEST_END)

        trading_dates = self.loader.get_trading_dates(BACKTEST_START, BACKTEST_END)
        if not trading_dates:
            print(
                "ERROR: No trading dates found.\n"
                "Run loader.schema_check() to inspect your DB."
            )
            return

        print(
            f"Backtest: {BACKTEST_START} → {BACKTEST_END}  "
            f"({len(trading_dates)} trading days)\n"
        )

        for day in trading_dates:
            self._run_day(day)

        # Settle any positions still open at end-of-backtest
        if self.open_positions:
            print(
                f"\n[EOB] Settling {len(self.open_positions)} open positions "
                f"at end of backtest"
            )
            last_day = trading_dates[-1]
            for pos in list(self.open_positions):
                last_px = self.loader.get_last_price_of_day(pos.symbol, last_day)
                self._close_position(pos, last_px or 0.0, last_day, "END_OF_BACKTEST")

        Path(RESULTS_DIR).mkdir(parents=True, exist_ok=True)
        self._save_results()
        self._print_summary()

    # ------------------------------------------------------------------
    # CORE: one trading day
    # ------------------------------------------------------------------

    def _run_day(self, day: date):
        # ── Expiry context ──────────────────────────────────────────────
        this_expiry  = self.loader.get_nifty_weekly_expiry(day)
        is_expiry    = (this_expiry == day)
        trade_expiry = self.loader.get_next_expiry(this_expiry) if is_expiry else this_expiry

        # Fetch lot size for the trading expiry (DB-backed, cached)
        lot_size = self.loader.get_lot_size(trade_expiry, "CE")

        # ── Load spot ───────────────────────────────────────────────────
        spot_df = self.loader.load_spot_day(day)
        if spot_df.empty:
            return

        # Each day gets a fresh SurvivorState initialised to opening price
        opening_price = float(spot_df.iloc[0]["close"])
        state = SurvivorState(self.scfg, opening_price)

        # Pre-cache today's option closes for all open positions
        opt_cache: dict[str, pd.DataFrame] = {}
        for p in self.open_positions:
            opt_cache[p.symbol] = self.loader.load_option_day(p.symbol, day)

        day_new_trades = 0
        day_new_margin = 0.0
        day_new_credit = 0.0

        print(
            f"[{day}] {'EXPIRY ' if is_expiry else '       '}"
            f"Trade expiry: {expiry_to_prefix(trade_expiry):<14}  "
            f"Lot size: {lot_size}  "
            f"Open pos: {len(self.open_positions)}"
        )

        # ── Minute-by-minute replay — NO look-ahead ─────────────────────
        for _, row in spot_df.iterrows():
            ts    = row["ts"]
            price = float(row["close"])

            # 1. Update P&L on all open positions
            for pos in self.open_positions:
                cached = opt_cache.get(pos.symbol)
                if cached is not None and not cached.empty and ts in cached.index:
                    opt_px = float(cached.loc[ts, "close"])
                else:
                    opt_px = self.loader.get_price_at_or_before(pos.symbol, ts)
                if opt_px > 0:
                    pos.update_pnl(opt_px)

            # 2. Track peak concurrent margin
            concurrent = sum(p.margin_used for p in self.open_positions)
            if concurrent > self.peak_concurrent_margin:
                self.peak_concurrent_margin = concurrent

            # 3. Resets
            state.apply_resets(price)

            # 4. PE signal
            pe_sig, pe_mult = state.check_pe(price)
            if pe_sig:
                pe_qty    = pe_mult * self.scfg["pe_quantity"]
                pe_strike = round_to_strike(
                    price - self.scfg["pe_symbol_gap"],
                    self.scfg["strike_interval"]
                )
                pe_sym = build_option_symbol(trade_expiry, pe_strike, "PE")

                # Validate: must have a full session on today's date
                if not self.loader.is_valid_option_day(pe_sym, day):
                    print(
                        f"  [{ts.strftime('%H:%M')}] SKIP PE  {pe_sym}"
                        f"  (no data on this date — 0 bars)"
                    )
                    state.confirm_pe(pe_mult)   # still advance reference
                else:
                    # NO look-ahead: price at THIS timestamp
                    pe_entry = self.loader.get_price_at_or_before(pe_sym, ts)
                    state.confirm_pe(pe_mult)   # advance reference regardless

                    if pe_entry >= self.scfg["min_price_to_sell"]:
                        mgn = estimate_margin(price, pe_strike, pe_qty, lot_size)
                        pos = Position(
                            symbol=pe_sym, option_type="PE",
                            strike=pe_strike, expiry=trade_expiry,
                            qty=pe_qty, lot_size=lot_size,
                            multiplier=pe_mult, entry_time=ts,
                            entry_price=pe_entry, nifty_at_entry=price,
                            margin_used=mgn,
                        )
                        self.open_positions.append(pos)
                        opt_cache[pe_sym] = self.loader.load_option_day(pe_sym, day)

                        day_new_trades += 1
                        day_new_margin += mgn
                        day_new_credit += pe_entry * pe_qty
                        self._record_expiry_trade(trade_expiry, mgn, pe_entry * pe_qty)
                        print(
                            f"  [{ts.strftime('%H:%M')}] SELL PE  {pe_sym:<30}"
                            f"  x{pe_qty:>3}  @ {pe_entry:>7.2f}"
                            f"  Nifty={price:>8.2f}"
                            f"  Margin=Rs {mgn:>9,.0f}"
                            f"  Cumul.=Rs "
                            f"{sum(p.margin_used for p in self.open_positions):>10,.0f}"
                        )
                    else:
                        print(
                            f"  [{ts.strftime('%H:%M')}] SKIP PE  {pe_sym}"
                            f"  (premium {pe_entry:.2f} < min "
                            f"{self.scfg['min_price_to_sell']})"
                        )

            # 5. CE signal
            ce_sig, ce_mult = state.check_ce(price)
            if ce_sig:
                ce_qty    = ce_mult * self.scfg["ce_quantity"]
                ce_strike = round_to_strike(
                    price + self.scfg["ce_symbol_gap"],
                    self.scfg["strike_interval"]
                )
                ce_sym = build_option_symbol(trade_expiry, ce_strike, "CE")

                if not self.loader.is_valid_option_day(ce_sym, day):
                    print(
                        f"  [{ts.strftime('%H:%M')}] SKIP CE  {ce_sym}"
                        f"  (no data on this date — 0 bars)"
                    )
                    state.confirm_ce(ce_mult)
                else:
                    ce_entry = self.loader.get_price_at_or_before(ce_sym, ts)
                    state.confirm_ce(ce_mult)

                    if ce_entry >= self.scfg["min_price_to_sell"]:
                        mgn = estimate_margin(price, ce_strike, ce_qty, lot_size)
                        pos = Position(
                            symbol=ce_sym, option_type="CE",
                            strike=ce_strike, expiry=trade_expiry,
                            qty=ce_qty, lot_size=lot_size,
                            multiplier=ce_mult, entry_time=ts,
                            entry_price=ce_entry, nifty_at_entry=price,
                            margin_used=mgn,
                        )
                        self.open_positions.append(pos)
                        opt_cache[ce_sym] = self.loader.load_option_day(ce_sym, day)

                        day_new_trades += 1
                        day_new_margin += mgn
                        day_new_credit += ce_entry * ce_qty
                        self._record_expiry_trade(trade_expiry, mgn, ce_entry * ce_qty)
                        print(
                            f"  [{ts.strftime('%H:%M')}] SELL CE  {ce_sym:<30}"
                            f"  x{ce_qty:>3}  @ {ce_entry:>7.2f}"
                            f"  Nifty={price:>8.2f}"
                            f"  Margin=Rs {mgn:>9,.0f}"
                            f"  Cumul.=Rs "
                            f"{sum(p.margin_used for p in self.open_positions):>10,.0f}"
                        )
                    else:
                        print(
                            f"  [{ts.strftime('%H:%M')}] SKIP CE  {ce_sym}"
                            f"  (premium {ce_entry:.2f} < min "
                            f"{self.scfg['min_price_to_sell']})"
                        )

        # ── Expiry settlement (after last minute of expiry day) ──────────
        if is_expiry:
            expiring = [p for p in self.open_positions if p.expiry == this_expiry]
            if expiring:
                print(
                    f"\n  [EXPIRY SETTLE] {this_expiry} → "
                    f"settling {len(expiring)} positions"
                )
            for pos in list(expiring):
                last_px = self.loader.get_last_price_of_day(pos.symbol, day)
                # Option expired worthless → settle at 0
                if last_px <= 0:
                    last_px = 0.0
                settle_ts = datetime.combine(day, datetime.min.time()).replace(
                    hour=15, minute=29
                )
                self._close_position(pos, last_px, settle_ts, "EXPIRY")

        # ── Daily stats ──────────────────────────────────────────────────
        open_upnl      = sum(p.unrealised_pnl for p in self.open_positions)
        current_margin = sum(p.margin_used     for p in self.open_positions)
        self.daily_stats.append({
            "date":            str(day),
            "is_expiry_day":   is_expiry,
            "trade_expiry":    str(trade_expiry),
            "new_trades":      day_new_trades,
            "new_margin_used": round(day_new_margin, 2),
            "new_credit":      round(day_new_credit, 2),
            "open_positions":  len(self.open_positions),
            "current_margin":  round(current_margin, 2),
            "unrealised_pnl":  round(open_upnl, 2),
            "cumulative_rpnl": round(self.total_realised_pnl, 2),
        })

    # ------------------------------------------------------------------
    # HELPERS
    # ------------------------------------------------------------------

    def _close_position(self, pos: Position, exit_price: float,
                        exit_time, reason: str):
        pos.close(exit_price, exit_time, reason)
        self.total_realised_pnl += pos.realised_pnl
        self.expiry_stats[str(pos.expiry)]["realised_pnl"] += pos.realised_pnl

        self.open_positions.remove(pos)
        self.closed_positions.append(pos)

        sign = "+" if pos.realised_pnl >= 0 else ""
        print(
            f"  [CLOSE-{reason:<12}] {pos.symbol:<30}"
            f"  Entry={pos.entry_price:>7.2f}  Exit={exit_price:>7.2f}"
            f"  P&L=Rs {sign}{pos.realised_pnl:>9,.0f}"
        )

    def _record_expiry_trade(self, expiry: date, margin: float, credit: float):
        key = str(expiry)
        es  = self.expiry_stats[key]
        es["expiry"]           = str(expiry)
        es["trades"]          += 1
        es["total_margin_in"] += margin
        es["total_credit"]    += credit
        concurrent = sum(
            p.margin_used for p in self.open_positions if p.expiry == expiry
        )
        if concurrent > es["peak_margin"]:
            es["peak_margin"] = concurrent

    # ------------------------------------------------------------------
    # RESULTS
    # ------------------------------------------------------------------

    def _save_results(self):
        out = Path(RESULTS_DIR)

        # trades.json
        trades_out = {
            "backtest_period":        f"{BACKTEST_START} to {BACKTEST_END}",
            "generated_at":           datetime.now().isoformat(),
            "strategy_config":        STRATEGY,
            "margin_config":          MARGIN_CFG,
            "summary": {
                "total_trades":           len(self.closed_positions),
                "total_realised_pnl":     round(self.total_realised_pnl, 2),
                "peak_concurrent_margin": round(self.peak_concurrent_margin, 2),
                "total_credit_collected": round(
                    sum(p.entry_price * p.qty for p in self.closed_positions), 2
                ),
            },
            "trades": [p.to_dict() for p in self.closed_positions],
        }
        with open(out / "trades.json", "w") as f:
            json.dump(trades_out, f, indent=2, default=str)

        # daily_stats.json
        with open(out / "daily_stats.json", "w") as f:
            json.dump(self.daily_stats, f, indent=2, default=str)

        # expiry_stats.json
        exp_list = sorted(
            [v for v in self.expiry_stats.values() if v["expiry"]],
            key=lambda x: x["expiry"]
        )
        for e in exp_list:
            e["total_credit"]     = round(e["total_credit"], 2)
            e["total_margin_in"]  = round(e["total_margin_in"], 2)
            e["peak_margin"]      = round(e["peak_margin"], 2)
            e["realised_pnl"]     = round(e["realised_pnl"], 2)
            e["roi_on_peak_margin"] = (
                round(e["realised_pnl"] / e["peak_margin"] * 100, 2)
                if e["peak_margin"] > 0 else 0
            )
        with open(out / "expiry_stats.json", "w") as f:
            json.dump(exp_list, f, indent=2, default=str)

        print(f"\n[Saved] {out}/trades.json        ({len(self.closed_positions)} trades)")
        print(f"[Saved] {out}/daily_stats.json   ({len(self.daily_stats)} days)")
        print(f"[Saved] {out}/expiry_stats.json  ({len(exp_list)} expiries)")

    def _print_summary(self):
        closed  = self.closed_positions
        winners = [p for p in closed if (p.realised_pnl or 0) >  0]
        losers  = [p for p in closed if (p.realised_pnl or 0) <= 0]

        total_credit = sum(p.entry_price * p.qty for p in closed)
        total_margin = sum(p.margin_used for p in closed)
        avg_margin   = total_margin / len(closed) if closed else 0
        win_rate     = len(winners) / len(closed) * 100 if closed else 0
        avg_win  = (sum(p.realised_pnl for p in winners) / len(winners)) if winners else 0
        avg_loss = (sum(p.realised_pnl for p in losers)  / len(losers))  if losers  else 0

        print("\n" + "=" * 75)
        print(f"  SURVIVOR BACKTEST SUMMARY  [ {BACKTEST_START}  →  {BACKTEST_END} ]")
        print("=" * 75)
        print(f"  Total trades             : {len(closed):>6}")
        print(f"  Winners                  : {len(winners):>6}  ({win_rate:.1f}%)")
        print(f"  Losers                   : {len(losers):>6}")
        print(f"  Avg win  (per trade)     : Rs {avg_win:>12,.2f}")
        print(f"  Avg loss (per trade)     : Rs {avg_loss:>12,.2f}")
        print()
        print(f"  Total credit collected   : Rs {total_credit:>14,.2f}")
        print(f"  Total realised P&L       : Rs {self.total_realised_pnl:>14,.2f}")
        print(f"  Peak concurrent margin   : Rs {self.peak_concurrent_margin:>14,.2f}")
        print(f"  Avg margin per trade     : Rs {avg_margin:>14,.2f}")
        if self.peak_concurrent_margin > 0:
            roi = self.total_realised_pnl / self.peak_concurrent_margin * 100
            print(f"  Return on peak margin    : {roi:>13.2f}%")
        print()
        print(f"  {'Expiry':<15}  {'Trades':>6}  {'Credit':>12}  "
              f"{'P&L':>12}  {'Peak Margin':>13}  {'ROI%':>7}")
        print("  " + "-" * 73)
        exp_list = sorted(
            [v for v in self.expiry_stats.values() if v["expiry"]],
            key=lambda x: x["expiry"]
        )
        for e in exp_list:
            roi_e = (e["realised_pnl"] / e["peak_margin"] * 100
                     if e["peak_margin"] > 0 else 0)
            print(
                f"  {e['expiry']:<15}  "
                f"{e['trades']:>6}  "
                f"Rs {e['total_credit']:>10,.0f}  "
                f"Rs {e['realised_pnl']:>10,.0f}  "
                f"Rs {e['peak_margin']:>11,.0f}  "
                f"{roi_e:>6.1f}%"
            )
        print("=" * 75 + "\n")


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    # Uncomment to inspect your DB schema before first run:
    # DataLoader(DB_PATH).schema_check()

    print("=" * 75)
    print("  SURVIVOR STRATEGY  —  BACKTEST ENGINE  (historify.duckdb)")
    print(f"  Period   : {BACKTEST_START}  →  {BACKTEST_END}")
    print(f"  Database : {DB_PATH}")
    print(f"  PE gap / CE gap        : {STRATEGY['pe_gap']} / {STRATEGY['ce_gap']}")
    print(f"  PE dist / CE dist      : {STRATEGY['pe_symbol_gap']} / {STRATEGY['ce_symbol_gap']}")
    print(f"  Lot size (fallback)    : {STRATEGY['lot_size']}  "
          f"(actual fetched per expiry from expired_fno_contracts)")
    print(f"  Strike interval        : {STRATEGY['strike_interval']}")
    print(f"  Min option bars/day    : {MIN_OPTION_BARS}  "
          f"({'any data accepted' if MIN_OPTION_BARS <= 1 else 'bars required'})")
    print(f"  Margin est.            : "
          f"{(MARGIN_CFG['span_pct'] + MARGIN_CFG['exposure_pct']) * 100:.1f}% of notional")
    print("=" * 75 + "\n")

    engine = BacktestEngine()
    engine.run()
