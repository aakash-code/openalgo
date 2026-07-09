"""Read-only DuckDB reader for the Magnetic Zones backtest.

Adapted from ``survivor_backtest_historify.py``'s ``DataLoader`` and
``backtesting/cpr_option_selling/cpr_backtest.py`` data layer. Same schema facts:

  * table:   market_data  (timestamp = Unix epoch seconds)
  * spot:    symbol='NIFTY', exchange='NSE_INDEX', interval='1m'
  * options: exchange='NFO', interval='1m', symbol = NIFTY{DDMMMYY}{STRIKE}{CE|PE}
  * catalog: expired_fno_contracts (per-expiry lot_size, strikes)

Timestamp handling mirrors the existing backtests verbatim (``_ts`` builds the
target epoch from local wall-clock; day filters use ``DATE(to_timestamp(...))``),
so results are directly comparable to cpr_option_selling / nifty_options_selling.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

DB_PATH = str(Path(__file__).resolve().parent.parent.parent / "db" / "historify.duckdb")

MONTH_MAP = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
             "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}

GRID = 50  # NIFTY strike interval


def _ts(d: date, h: int, m: int) -> int:
    """Target epoch (seconds) for a date + wall-clock HH:MM (local tz, as cpr)."""
    return int(datetime(d.year, d.month, d.day, h, m).timestamp())


def nearest_strike(level: float) -> int:
    return int(round(level / GRID) * GRID)


def parse_expiry_code(code: str) -> date | None:
    try:
        return date(2000 + int(code[5:7]), MONTH_MAP[code[2:5]], int(code[:2]))
    except Exception:
        return None


def expiry_code(d: date) -> str:
    return d.strftime("%d%b%y").upper()


class DataLoader:
    def __init__(self, db_path: str = DB_PATH):
        self.conn = duckdb.connect(db_path, read_only=True)
        self._lot_cache: dict = {}
        self._opt_day_cache: dict = {}
        self._daily: pd.DataFrame | None = None
        self._weekly: pd.DataFrame | None = None
        self._monthly: pd.DataFrame | None = None
        self._expiries: list[date] = self._load_expiries()
        self._monthly_expiries: list[date] = self._derive_monthly_expiries()

    def close(self):
        self.conn.close()

    # ---- expiries -------------------------------------------------------
    def _load_expiries(self) -> list[date]:
        rows = self.conn.execute(
            r"""SELECT DISTINCT REGEXP_EXTRACT(symbol,'NIFTY(\d{2}[A-Z]{3}\d{2})',1) code
                FROM market_data
                WHERE symbol LIKE 'NIFTY%CE' AND exchange='NFO' AND interval='1m'"""
        ).fetchall()
        out = sorted({d for (c,) in rows if c and (d := parse_expiry_code(c))})
        return out

    def _derive_monthly_expiries(self) -> list[date]:
        """Monthly expiry = the LAST weekly expiry within each calendar month."""
        by_month: dict[tuple[int, int], date] = {}
        for e in self._expiries:
            key = (e.year, e.month)
            if key not in by_month or e > by_month[key]:
                by_month[key] = e
        return sorted(by_month.values())

    @property
    def weekly_expiries(self) -> list[date]:
        return self._expiries

    @property
    def monthly_expiries(self) -> list[date]:
        return self._monthly_expiries

    def nearest_expiry(self, ref: date) -> date | None:
        """First weekly expiry on/after ref (0DTE allowed on expiry day)."""
        for e in self._expiries:
            if e >= ref:
                return e
        return None

    def monthly_expiry_for(self, ref: date) -> date | None:
        for e in self._monthly_expiries:
            if e >= ref:
                return e
        return None

    # ---- spot bars ------------------------------------------------------
    def daily_spot(self) -> pd.DataFrame:
        """Daily OHLC for NIFTY spot (aggregated from 1m), indexed by date."""
        if self._daily is None:
            rows = self.conn.execute(
                """SELECT CAST(to_timestamp(timestamp) AS DATE) d,
                          arg_min(open, timestamp) o, MAX(high) h, MIN(low) l,
                          arg_max(close, timestamp) c
                   FROM market_data
                   WHERE symbol='NIFTY' AND exchange='NSE_INDEX' AND interval='1m'
                   GROUP BY d ORDER BY d"""
            ).fetchall()
            df = pd.DataFrame(rows, columns=["d", "o", "h", "l", "c"])
            df["d"] = pd.to_datetime(df["d"])
            self._daily = df.set_index("d")
        return self._daily

    def weekly_bars(self) -> pd.DataFrame:
        """ISO-week OHLC indexed by the week's Monday (prior-week zones use shift)."""
        if self._weekly is None:
            d = self.daily_spot()
            g = d.resample("W-MON", label="left", closed="left")
            self._weekly = pd.DataFrame(
                {"o": g["o"].first(), "h": g["h"].max(), "l": g["l"].min(), "c": g["c"].last()}
            ).dropna()
        return self._weekly

    def monthly_bars(self) -> pd.DataFrame:
        """Calendar-month OHLC indexed by month start (prior-month zones use shift)."""
        if self._monthly is None:
            d = self.daily_spot()
            g = d.resample("MS")
            self._monthly = pd.DataFrame(
                {"o": g["o"].first(), "h": g["h"].max(), "l": g["l"].min(), "c": g["c"].last()}
            ).dropna()
        return self._monthly

    def trading_days(self, start: str, end: str) -> list[date]:
        d = self.daily_spot()
        mask = (d.index >= pd.Timestamp(start)) & (d.index <= pd.Timestamp(end))
        return [t.date() for t in d.index[mask]]

    # ---- intraday spot --------------------------------------------------
    def spot_1m_day(self, day: date) -> pd.DataFrame:
        """1-min NIFTY spot for a day: columns ts (epoch), close. Ascending."""
        rows = self.conn.execute(
            """SELECT timestamp AS ts, close
               FROM market_data
               WHERE symbol='NIFTY' AND exchange='NSE_INDEX' AND interval='1m'
                 AND DATE(to_timestamp(timestamp)) = ?
               ORDER BY timestamp""",
            [str(day)],
        ).fetchdf()
        return rows

    def spot_at(self, d: date, h: int = 9, m: int = 20, window: int = 600) -> float | None:
        """NIFTY spot close near a wall-clock time (single-row, +/- window sec)."""
        ts = _ts(d, h, m)
        row = self.conn.execute(
            """SELECT close FROM market_data
               WHERE symbol='NIFTY' AND exchange='NSE_INDEX' AND interval='1m'
                 AND timestamp BETWEEN ? AND ?
               ORDER BY timestamp LIMIT 1""",
            [ts - window, ts + window],
        ).fetchone()
        return float(row[0]) if row else None

    def spot_eod(self, d: date) -> float | None:
        row = self.conn.execute(
            """SELECT close FROM market_data
               WHERE symbol='NIFTY' AND exchange='NSE_INDEX' AND interval='1m'
                 AND DATE(to_timestamp(timestamp)) = ?
               ORDER BY timestamp DESC LIMIT 1""",
            [str(d)],
        ).fetchone()
        return float(row[0]) if row else None

    # ---- option data ----------------------------------------------------
    @staticmethod
    def option_symbol(expiry: date, strike: int, right: str) -> str:
        return f"NIFTY{expiry_code(expiry)}{int(strike)}{right.upper()}"

    def option_price_at(self, symbol: str, ts: int, window: int = 600) -> float | None:
        """Last option close at-or-before ts (no look-ahead), within `window` sec back.

        Mirrors cpr's tolerance: we accept a fill from up to `window` seconds
        earlier when the exact minute did not trade. Never looks forward.
        """
        row = self.conn.execute(
            """SELECT close FROM market_data
               WHERE symbol=? AND exchange='NFO' AND interval='1m'
                 AND timestamp BETWEEN ? AND ?
               ORDER BY timestamp DESC LIMIT 1""",
            [symbol, ts - window, ts],
        ).fetchone()
        return float(row[0]) if row else None

    def option_1m_day(self, symbol: str, day: date) -> pd.Series:
        """1-min option closes for a day as a Series indexed by epoch (cached)."""
        key = (symbol, day)
        if key not in self._opt_day_cache:
            df = self.conn.execute(
                """SELECT timestamp AS ts, close
                   FROM market_data
                   WHERE symbol=? AND exchange='NFO' AND interval='1m'
                     AND DATE(to_timestamp(timestamp)) = ?
                   ORDER BY timestamp""",
                [symbol, str(day)],
            ).fetchdf()
            s = pd.Series(dtype=float) if df.empty else df.set_index("ts")["close"]
            self._opt_day_cache[key] = s
        return self._opt_day_cache[key]

    def option_eod(self, symbol: str, day: date) -> float | None:
        row = self.conn.execute(
            """SELECT close FROM market_data
               WHERE symbol=? AND exchange='NFO' AND interval='1m'
                 AND DATE(to_timestamp(timestamp)) = ?
               ORDER BY timestamp DESC LIMIT 1""",
            [symbol, str(day)],
        ).fetchone()
        return float(row[0]) if row else None

    # ---- metadata -------------------------------------------------------
    def lot_size(self, expiry: date) -> int:
        if expiry in self._lot_cache:
            return self._lot_cache[expiry]
        row = self.conn.execute(
            """SELECT lot_size FROM expired_fno_contracts
               WHERE openalgo_symbol='NIFTY' AND expiry_date=? AND contract_type='CE'
               LIMIT 1""",
            [str(expiry)],
        ).fetchone()
        lot = int(row[0]) if row else _lot_schedule(expiry)
        self._lot_cache[expiry] = lot
        return lot


def _lot_schedule(d: date) -> int:
    """Fallback NIFTY lot size by SEBI timeline: 25 -> 75 (2024-11-20) -> 65 (2026-01-01)."""
    if d >= date(2026, 1, 1):
        return 65
    if d >= date(2024, 11, 20):
        return 75
    return 25
