#!/usr/bin/env python
"""
Run the UNMODIFIED `survivor_backtest_ultimate.py` engine on the dhanloader
5-year NIFTY dataset (instead of db/historify.duckdb).

How: the ultimate engine reads ALL market data through a `DataLoader` object.
We implement a drop-in `DhanloaderDataLoader` with the same interface that serves
data from the dhanloader CSV dataset, then monkey-patch it into the ultimate
module before instantiating its `Backtester`. The ultimate's strategy logic
(VIX-dynamic multiplier cap, 0.5% gap protection, 13:30 gamma zone, OTM-adjusted
margin, 10x stop-loss, weekly-expiry settlement) runs verbatim.

Lot size is served date-aware (25 -> 75 -> 65 per SEBI), i.e. the lot fix.
VIX is proxied by the dhanloader ATM implied-vol (`iv` column).

Run:  uv run python backtesting/survivor/survivor_ultimate_dhanloader.py
"""
from __future__ import annotations

import importlib.util
import sys
import types
from bisect import bisect_left, bisect_right
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------- dhanloader paths
DATA_ROOT = Path("/Users/bond7/Desktop/Project/dhanloader/data/NIFTY/chunks")
SPOT_FILE = Path("/Users/bond7/Desktop/Project/dhanloader/data/INDEX_SPOT/NIFTY_clean.csv")
FLAG = "WEEK"
OFFSETS = ["ATM"] + [f"ATMm{i}" for i in range(1, 11)] + [f"ATMp{i}" for i in range(1, 11)]
IST = pd.Timedelta(hours=5, minutes=30)
EXPIRY_ATM_THRESHOLD = 3.0
TYPE_DIR = {"CE": "CALL", "PE": "PUT"}

BACKTEST_START = "2024-07-01"
BACKTEST_END = "2026-06-30"
RESULTS_DIR = str(Path(__file__).resolve().parent / "ultimate_dhanloader_results")


def nifty_lot_size(d: date) -> int:
    if d >= date(2026, 1, 1):
        return 65
    if d >= date(2024, 11, 20):
        return 75
    return 25


# ============================================================================
# Drop-in DataLoader serving the dhanloader dataset
# ============================================================================
class DhanloaderDataLoader:
    def __init__(self, db_path: str | None = None):
        sp = pd.read_csv(SPOT_FILE, usecols=["datetime", "open", "high", "low", "close"])
        sp["ts"] = pd.to_datetime(sp["datetime"])
        sp["date"] = sp["ts"].dt.date
        sp = sp.sort_values("ts").reset_index(drop=True)
        self._spot = sp
        self._spot_by_date = {d: g.reset_index(drop=True) for d, g in sp.groupby("date")}
        self._dates = sorted(self._spot_by_date)

        self._cycles = self._list_cycles()          # [(name, start_date, end_date)]
        self._expiries, self._atm_iv = self._detect_expiries_and_iv()
        self._exp_sorted = sorted(self._expiries)

        self._pmap_cache: dict = {}                  # (cycle,r,T) -> {date:{ts:{K:px}}}
        self._strike_cache: dict = {}                # (cycle,r,T) -> set(K)

    # ---- static dataset structure ----
    def _list_cycles(self):
        d = DATA_ROOT / FLAG / "1" / "ATM" / "CALL"
        out = []
        for p in sorted(d.glob("*.csv")):
            a, b = p.stem.split("_")
            out.append((p.stem, pd.to_datetime(a).date(), pd.to_datetime(b).date()))
        return out

    def _cycle_for(self, day: date):
        for name, a, b in self._cycles:
            if a <= day <= b:
                return name
        return None

    def _detect_expiries_and_iv(self):
        expiries, atm_iv = set(), {}
        for name, _, _ in self._cycles:
            frames = {}
            for ot in ("CALL", "PUT"):
                fp = DATA_ROOT / FLAG / "1" / "ATM" / ot / f"{name}.csv"
                if fp.exists():
                    df = pd.read_csv(fp, usecols=["datetime", "close", "iv"])
                    df["ts"] = pd.to_datetime(df["datetime"]) + IST
                    df["date"] = df["ts"].dt.date
                    frames[ot] = df
            if "CALL" not in frames or "PUT" not in frames:
                continue
            cl = frames["CALL"].groupby("date")["close"].last()
            pl = frames["PUT"].groupby("date")["close"].last()
            iv = frames["CALL"].groupby("date")["iv"].mean()
            for d in cl.index:
                if min(cl.get(d, 1e9), pl.get(d, 1e9)) <= EXPIRY_ATM_THRESHOLD:
                    expiries.add(d)
                v = iv.get(d)
                if v is not None and v > 0:
                    atm_iv[d] = float(v)
        return expiries, atm_iv

    # ---- option price maps (lazy, cached) ----
    def _price_map(self, cycle: str, r: int, otype: str):
        key = (cycle, r, otype)
        if key in self._pmap_cache:
            return self._pmap_cache[key]
        by_day: dict = {}
        strikes: set = set()
        tdir = TYPE_DIR[otype]
        for off in OFFSETS:
            fp = DATA_ROOT / FLAG / str(r) / off / tdir / f"{cycle}.csv"
            if not fp.exists():
                continue
            df = pd.read_csv(fp, usecols=["datetime", "close", "strike_abs"])
            ts = pd.to_datetime(df["datetime"]) + IST
            K = df["strike_abs"].round().astype(int)
            cl = df["close"].astype(float)
            for t, k, c in zip(ts, K, cl):
                d = t.date()
                by_day.setdefault(d, {}).setdefault(t, {})[k] = c
                strikes.add(int(k))
        self._pmap_cache[key] = by_day
        self._strike_cache[key] = strikes
        return by_day

    def _parse(self, symbol: str):
        body = symbol[5:]                            # strip 'NIFTY'
        exp = datetime.strptime(body[:7], "%d%b%y").date()
        rest = body[7:]
        return exp, int(rest[:-2]), rest[-2:]

    def _rank(self, day: date, exp: date) -> int:
        front = self.get_nifty_weekly_expiry(day)
        try:
            i_exp = self._exp_sorted.index(exp)
        except ValueError:
            i_exp = bisect_left(self._exp_sorted, exp)
        try:
            i_front = self._exp_sorted.index(front)
        except ValueError:
            i_front = bisect_left(self._exp_sorted, front)
        return i_exp - i_front + 1

    # ---- interface used by the ultimate Backtester ----
    def load_valid_option_days(self, start, end):
        return None

    def schema_check(self):
        return None

    def get_trading_dates(self, start, end):
        s = pd.to_datetime(start).date()
        e = pd.to_datetime(end).date()
        return [d for d in self._dates if s <= d <= e]

    def load_spot_day(self, day: date):
        g = self._spot_by_date.get(day)
        return g if g is not None else pd.DataFrame(columns=["ts", "close"])

    def get_previous_close(self, symbol, exchange, ref_date: date):
        i = bisect_left(self._dates, ref_date)
        if i == 0:
            return None
        prev = self._dates[i - 1]
        return float(self._spot_by_date[prev].iloc[-1]["close"])

    def get_vix_level(self, day: date) -> float:
        for k in range(0, 6):                        # nearest available <= day
            v = self._atm_iv.get(day - timedelta(days=k))
            if v is not None:
                return v
        return 15.0

    def get_nifty_weekly_expiry(self, ref_date: date) -> date:
        i = bisect_left(self._exp_sorted, ref_date)
        return self._exp_sorted[i] if i < len(self._exp_sorted) else self._exp_sorted[-1]

    def get_next_expiry(self, current_expiry: date) -> date:
        i = bisect_right(self._exp_sorted, current_expiry)
        return self._exp_sorted[i] if i < len(self._exp_sorted) else current_expiry + timedelta(days=7)

    def get_lot_size(self, expiry: date, contract_type: str = "CE") -> int:
        return nifty_lot_size(expiry)

    def is_valid_option_day(self, symbol: str, day: date) -> bool:
        exp, K, otype = self._parse(symbol)
        cyc = self._cycle_for(day)
        if cyc is None:
            return False
        r = self._rank(day, exp)
        if r < 1 or r > 4:
            return False
        by_day = self._price_map(cyc, r, otype)
        return day in by_day and K in self._strike_cache.get((cyc, r, otype), set())

    def get_price_at_or_before(self, symbol: str, ts: pd.Timestamp) -> float:
        exp, K, otype = self._parse(symbol)
        day = ts.date()
        cyc = self._cycle_for(day)
        if cyc is None:
            return 0.0
        r = self._rank(day, exp)
        if r < 1 or r > 4:
            return 0.0
        by_day = self._price_map(cyc, r, otype)
        day_map = by_day.get(day)
        if not day_map:
            return 0.0
        row = day_map.get(ts)
        if row and K in row:
            return row[K]
        # at-or-before fallback within the day (rare missing minute)
        prev = [t for t in day_map if t <= ts and K in day_map[t]]
        return day_map[max(prev)][K] if prev else 0.0

    def load_option_day(self, symbol: str, day: date) -> pd.DataFrame:
        exp, K, otype = self._parse(symbol)
        cyc = self._cycle_for(day)
        if cyc is None:
            return pd.DataFrame(columns=["close"])
        r = self._rank(day, exp)
        if r < 1 or r > 4:
            return pd.DataFrame(columns=["close"])
        by_day = self._price_map(cyc, r, otype).get(day, {})
        rows = [(t, row[K]) for t, row in by_day.items() if K in row]
        if not rows:
            return pd.DataFrame(columns=["close"])
        df = pd.DataFrame(rows, columns=["ts", "close"]).sort_values("ts").set_index("ts")
        return df

    def get_last_price_of_day(self, symbol: str, day: date) -> float:
        df = self.load_option_day(symbol, day)
        return float(df.iloc[-1]["close"]) if not df.empty else 0.0


# ============================================================================
# Import the ultimate engine and inject our loader
# ============================================================================
def _load_ultimate_module():
    # stub duckdb so the module imports even without the DB driver
    if "duckdb" not in sys.modules:
        sys.modules["duckdb"] = types.ModuleType("duckdb")
    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root / "survivor_backtest_ultimate.py"
    spec = importlib.util.spec_from_file_location("survivor_backtest_ultimate", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["survivor_backtest_ultimate"] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    ult = _load_ultimate_module()
    # inject dhanloader loader + our window + output dir
    ult.DataLoader = DhanloaderDataLoader
    ult.DB_PATH = ""
    ult.BACKTEST_START = BACKTEST_START
    ult.BACKTEST_END = BACKTEST_END
    ult.RESULTS_DIR = RESULTS_DIR
    Path(RESULTS_DIR).mkdir(parents=True, exist_ok=True)

    bt = ult.BacktestEngine()                        # __init__ -> DataLoader(DB_PATH) = ours
    bt.run()


if __name__ == "__main__":
    main()
