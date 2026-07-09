"""
NIFTY Data Integrity Checker
=============================
Checks DuckDB (historify.duckdb) for missing NIFTY spot and options data.
Compares against expected NSE trading calendar (excluding weekends + holidays).

Usage:
    cd /Users/bond7/Desktop/Project/openalgo
    uv run python backtesting/nifty_options_selling/check_data_gaps.py
"""

import sys, os
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pandas as pd

# ── Config ────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = PROJECT_ROOT / "db" / "historify.duckdb"

# Backtest analysis range
RANGE_START = date(2024, 10, 1)
RANGE_END   = date(2026, 3, 31)

# NSE holidays (NSE/NFO closed) — from database/market_calendar_db.py
NSE_HOLIDAYS = {
    # 2024 (only dates in our analysis range Oct-Dec 2024)
    date(2024, 10, 2),   # Gandhi Jayanti
    date(2024, 11, 1),   # Diwali (Muhurat — special session)
    date(2024, 11, 15),  # Guru Nanak Jayanti
    date(2024, 11, 20),  # Maharashtra State Election
    date(2024, 12, 25),  # Christmas
    # 2025
    date(2025, 2, 26),   # Maha Shivaratri
    date(2025, 3, 14),   # Holi
    date(2025, 3, 31),   # Id-Ul-Fitr
    date(2025, 4, 10),   # Shri Mahavir Jayanti
    date(2025, 4, 14),   # Dr. Ambedkar Jayanti
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 1),    # Maharashtra Day
    date(2025, 8, 15),   # Independence Day
    date(2025, 8, 27),   # Janmashtami
    date(2025, 10, 2),   # Gandhi Jayanti
    date(2025, 10, 21),  # Dussehra
    date(2025, 10, 22),  # Diwali Amavasya / Additional holiday
    date(2025, 11, 1),   # Diwali (Muhurat)
    date(2025, 11, 5),   # Diwali Balipratipada / Bhai Dooj
    date(2025, 11, 14),  # Guru Nanak Jayanti
    date(2025, 12, 25),  # Christmas
    # 2026
    date(2026, 1, 15),   # Municipal Corp Election
    date(2026, 1, 26),   # Republic Day
    date(2026, 3, 3),    # Holi
    date(2026, 3, 26),   # Shri Ram Navami
    date(2026, 3, 31),   # Shri Mahavir Jayanti
}


def expected_trading_days(start: date, end: date) -> set:
    """Generate set of expected trading dates (weekdays minus holidays)."""
    days = set()
    d = start
    while d <= end:
        if d.weekday() < 5 and d not in NSE_HOLIDAYS:  # Mon-Fri, not holiday
            days.add(d)
        d += timedelta(days=1)
    return days


def group_consecutive(dates: list) -> list:
    """Group sorted dates into consecutive ranges (allowing weekend gaps)."""
    if not dates:
        return []
    ranges = []
    start = dates[0]
    prev = dates[0]
    for d in dates[1:]:
        # If gap > 3 calendar days, start new range
        if (d - prev).days > 3:
            ranges.append((start, prev))
            start = d
        prev = d
    ranges.append((start, prev))
    return ranges


def main():
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)

    con = duckdb.connect(str(DB_PATH), read_only=True)

    print("=" * 70)
    print("  NIFTY DATA INTEGRITY REPORT")
    print(f"  Analysis range: {RANGE_START} to {RANGE_END}")
    print("=" * 70)

    expected = expected_trading_days(RANGE_START, RANGE_END)
    print(f"\nExpected trading days (weekdays - holidays): {len(expected)}")

    # ── 1. NIFTY Spot Data ────────────────────────────────────────────────
    start_ts = int(pd.Timestamp(RANGE_START).timestamp())
    end_ts = int(pd.Timestamp(RANGE_END, hour=23, minute=59).timestamp())

    spot_df = con.execute("""
        SELECT DISTINCT CAST(to_timestamp(timestamp) AS DATE) as d
        FROM market_data
        WHERE symbol='NIFTY' AND exchange='NSE_INDEX' AND interval='1m'
          AND timestamp >= ? AND timestamp <= ?
        ORDER BY d
    """, [start_ts, end_ts]).fetchdf()
    spot_dates = set(pd.to_datetime(spot_df['d']).dt.date)

    # ── 2. NIFTY Options Data ─────────────────────────────────────────────
    opts_df = con.execute("""
        SELECT DISTINCT CAST(to_timestamp(timestamp) AS DATE) as d
        FROM market_data
        WHERE symbol LIKE 'NIFTY%' AND exchange='NFO' AND interval='1m'
          AND timestamp >= ? AND timestamp <= ?
        ORDER BY d
    """, [start_ts, end_ts]).fetchdf()
    opts_dates = set(pd.to_datetime(opts_df['d']).dt.date)

    # ── 3. Analysis ──────────────────────────────────────────────────────
    missing_spot = sorted(expected - spot_dates)
    missing_opts = sorted(expected - opts_dates)
    missing_spot_with_opts = sorted(set(missing_spot) & opts_dates)
    missing_both = sorted(set(missing_spot) & set(missing_opts))

    spot_pct = len(spot_dates & expected) / len(expected) * 100
    opts_pct = len(opts_dates & expected) / len(expected) * 100

    print(f"\n{'─' * 70}")
    print(f"  NIFTY SPOT DATA (NSE_INDEX, 1m)")
    print(f"{'─' * 70}")
    print(f"  Available : {len(spot_dates & expected)} / {len(expected)} days ({spot_pct:.1f}%)")
    print(f"  Missing   : {len(missing_spot)} days")

    if missing_spot:
        ranges = group_consecutive(missing_spot)
        print(f"\n  Missing date ranges:")
        for s, e in ranges:
            count = len([d for d in missing_spot if s <= d <= e])
            has_opts = any(d in opts_dates for d in missing_spot if s <= d <= e)
            opts_tag = " ← OPTIONS DATA AVAILABLE" if has_opts else " ← OPTIONS ALSO MISSING"
            if s == e:
                print(f"    {s} (1 day){opts_tag}")
            else:
                print(f"    {s} → {e} ({count} trading days){opts_tag}")

        print(f"\n  Day-by-day missing dates:")
        for d in missing_spot:
            in_opts = "✓ opts" if d in opts_dates else "✗ opts"
            print(f"    {d} ({d.strftime('%A')})  [{in_opts}]")

    print(f"\n{'─' * 70}")
    print(f"  NIFTY OPTIONS DATA (NFO, 1m)")
    print(f"{'─' * 70}")
    print(f"  Available : {len(opts_dates & expected)} / {len(expected)} days ({opts_pct:.1f}%)")
    print(f"  Missing   : {len(missing_opts)} days")

    if missing_opts:
        ranges = group_consecutive(missing_opts)
        print(f"\n  Missing date ranges:")
        for s, e in ranges:
            count = len([d for d in missing_opts if s <= d <= e])
            if s == e:
                print(f"    {s} (1 day)")
            else:
                print(f"    {s} → {e} ({count} trading days)")

    # ── 4. Expired F&O Contract Status ────────────────────────────────────
    print(f"\n{'─' * 70}")
    print(f"  EXPIRED F&O CONTRACT DOWNLOAD STATUS")
    print(f"{'─' * 70}")

    expiry_df = con.execute("""
        SELECT expiry_date, contract_type,
               COUNT(*) as total,
               SUM(CASE WHEN data_fetched THEN 1 ELSE 0 END) as fetched
        FROM expired_fno_contracts
        WHERE openalgo_symbol LIKE 'NIFTY%'
          AND contract_type IN ('CE', 'PE')
        GROUP BY expiry_date, contract_type
        ORDER BY expiry_date, contract_type
    """).fetchdf()

    if len(expiry_df) > 0:
        incomplete = []
        for _, row in expiry_df.iterrows():
            pct = row['fetched'] / row['total'] * 100 if row['total'] > 0 else 0
            if pct < 90:
                incomplete.append(row)

        if incomplete:
            print(f"\n  INCOMPLETE expiries (<90% downloaded):")
            for row in incomplete:
                pct = row['fetched'] / row['total'] * 100
                status = "CRITICAL" if pct < 10 else "WARNING"
                exp_str = str(row['expiry_date']).split()[0]  # strip time
                print(f"    {exp_str} {row['contract_type']}: "
                      f"{int(row['fetched'])}/{int(row['total'])} ({pct:.1f}%) — {status}")
        else:
            print(f"\n  All {len(expiry_df)} expiry-type combos have ≥90% data downloaded ✓")

        # Summary
        total_contracts = expiry_df['total'].sum()
        fetched_contracts = expiry_df['fetched'].sum()
        print(f"\n  Total contracts: {int(fetched_contracts)}/{int(total_contracts)} "
              f"({fetched_contracts/total_contracts*100:.1f}%)")

    # ── 5. Candle count per day (spot) — check for partial days ───────────
    print(f"\n{'─' * 70}")
    print(f"  SPOT DATA QUALITY CHECK (candles per day)")
    print(f"{'─' * 70}")

    candle_counts = con.execute("""
        SELECT CAST(to_timestamp(timestamp) AS DATE) as d,
               COUNT(*) as candles
        FROM market_data
        WHERE symbol='NIFTY' AND exchange='NSE_INDEX' AND interval='1m'
          AND timestamp >= ? AND timestamp <= ?
        GROUP BY d
        ORDER BY d
    """, [start_ts, end_ts]).fetchdf()

    if len(candle_counts) > 0:
        median_candles = candle_counts['candles'].median()
        low_days = candle_counts[candle_counts['candles'] < median_candles * 0.5]
        print(f"  Median candles/day: {int(median_candles)}")
        print(f"  Days with <50% candles (partial data): {len(low_days)}")
        if len(low_days) > 0:
            for _, row in low_days.iterrows():
                print(f"    {row['d']}: {int(row['candles'])} candles "
                      f"({row['candles']/median_candles*100:.0f}% of normal)")

    # ── 6. Action Summary ────────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print(f"  ACTION REQUIRED")
    print(f"{'=' * 70}")

    actions = []
    if missing_spot_with_opts:
        ranges = group_consecutive(missing_spot_with_opts)
        range_strs = [f"{s} to {e}" for s, e in ranges]
        actions.append(
            f"1. Re-download NIFTY (NSE_INDEX) spot data via Historify UI\n"
            f"     Missing ranges: {', '.join(range_strs)}\n"
            f"     Total: {len(missing_spot_with_opts)} trading days"
        )
    critical = [r for r in incomplete if r['fetched'] / r['total'] * 100 < 50]
    if critical:
        actions.append(
            f"2. Re-download expired F&O data for CRITICAL expiries:\n"
            + "\n".join(f"     {str(r['expiry_date']).split()[0]} {r['contract_type']}: "
                        f"{int(r['fetched'])}/{int(r['total'])}" for r in critical)
        )

    if actions:
        for a in actions:
            print(f"\n  {a}")
        print(f"\n  After fixing, re-run this script to verify completeness.")
    else:
        print(f"\n  No action required — all data is complete! ✓")

    print()
    con.close()


if __name__ == "__main__":
    main()
