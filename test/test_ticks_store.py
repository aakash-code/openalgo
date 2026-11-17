"""Tick store (Parquet lake + DuckDB resample) round-trip and correctness tests."""

from datetime import UTC, date, datetime, timezone

import pytest

import database.ticks_store as ts


@pytest.fixture
def lake(tmp_path, monkeypatch):
    monkeypatch.setenv("TICKS_DATA_DIR", str(tmp_path / "ticks"))
    # Reset the module-level writer handle / sequence between tests.
    ts._writer_handle = None
    ts._seq = 0
    assert ts.claim_writer()
    yield ts
    ts.release_writer()


def _tick(ltt_ms, ltp, volume, oi, with_depth=True):
    data = {
        "ltp": ltp,
        "average_price": ltp,
        "volume": volume,
        "oi": oi,
        "total_buy_quantity": 10,
        "total_sell_quantity": 20,
        "open": 100.0,
        "high": 120.0,
        "low": 90.0,
        "close": 99.0,
        "ltt": ltt_ms,
        "timestamp": ltt_ms + 15,
    }
    if with_depth:
        data["depth"] = {
            "buy": [{"price": ltp - 0.1, "quantity": 5}, {"price": ltp - 0.2, "quantity": 9}],
            "sell": [{"price": ltp + 0.1, "quantity": 7}],
        }
    return data


def _write_minute(lake, symbol, base_ms, n, v0=1000, oi0=50000):
    rows = [
        lake.row_from_tick(
            symbol,
            "NFO",
            "NIFTY",
            _tick(base_ms + i * 1000, 100.0 + (i % 5), v0 + i * 5, oi0 + i),
            date(2026, 11, 18),
        )
        for i in range(n)
    ]
    return lake.write_batch(rows)


def test_write_and_query_roundtrip(lake):
    base = int(datetime(2026, 11, 18, 4, 0, tzinfo=UTC).timestamp() * 1000)
    assert _write_minute(lake, "NIFTY18NOV2524000CE", base, 30) == 30

    df = lake.query_ticks("NIFTY18NOV2524000CE", "2026-11-18", "2026-11-18")
    assert len(df) == 30
    row = df.iloc[0]
    # Depth flattened; order counts null (Upstox sends none).
    assert row["bid_px_1"] == pytest.approx(99.9)
    assert int(row["bid_qty_1"]) == 5
    assert int(row["bid_qty_2"]) == 9
    import pandas as pd

    assert pd.isna(row["bid_ord_1"])  # Upstox sends no order counts
    assert int(row["oi"]) == 50000
    # Hive partition columns recovered from the path.
    assert str(row["underlying"]) == "NIFTY"


def test_query_filters_by_symbol_and_date(lake):
    base = int(datetime(2026, 11, 18, 4, 0, tzinfo=UTC).timestamp() * 1000)
    _write_minute(lake, "AAA18NOV2524000CE", base, 10)
    _write_minute(lake, "BBB18NOV2524000CE", base, 7)
    assert len(lake.query_ticks("AAA18NOV2524000CE", "2026-11-18", "2026-11-18")) == 10
    assert (
        len(
            lake.query_ticks(["AAA18NOV2524000CE", "BBB18NOV2524000CE"], "2026-11-18", "2026-11-18")
        )
        == 17
    )
    # A date with no data is empty, not an error.
    assert lake.query_ticks("AAA18NOV2524000CE", "2026-11-19", "2026-11-19").empty


def test_resample_to_1m_bars(lake):
    # Two whole UTC minutes, 60 ticks each; volume is cumulative.
    base = int(datetime(2026, 11, 18, 4, 0, tzinfo=UTC).timestamp() * 1000)
    _write_minute(lake, "NIFTY18NOV2524000CE", base, 120, v0=1000, oi0=50000)

    bars = lake.get_ohlcv("NIFTY18NOV2524000CE", "NFO", "1m", base // 1000 - 5, base // 1000 + 200)
    assert len(bars) == 2
    first = bars.iloc[0]
    # ltp cycles 100..104; high is 104, low 100.
    assert first["high"] == pytest.approx(104.0)
    assert first["low"] == pytest.approx(100.0)
    # Cumulative volume delta across the 60 ticks of the minute = 59*5 = 295.
    assert int(first["volume"]) == 295
    # OI is the last value in the bucket.
    assert int(first["oi"]) == 50059


def test_compaction_preserves_rows_and_reduces_files(lake):
    base = int(datetime(2026, 11, 18, 4, 0, tzinfo=UTC).timestamp() * 1000)
    for k in range(4):  # four separate part-files
        _write_minute(lake, "NIFTY18NOV2524000CE", base + k * 60000, 10)
    assert lake.data_stats()["files"] == 4
    assert lake.compact_day("2026-11-18") == 1
    assert lake.data_stats()["files"] == 1
    assert len(lake.query_ticks("NIFTY18NOV2524000CE", "2026-11-18", "2026-11-18")) == 40


def test_row_from_tick_handles_missing_fields(lake):
    row = lake.row_from_tick("X", "NFO", "NIFTY", {"ltp": 5.0}, date(2026, 11, 18))
    assert row["ltp"] == 5.0
    assert row["oi"] is None
    assert row["bid_px_1"] is None
    # ltt in seconds is normalised up to ms.
    row2 = lake.row_from_tick(
        "X", "NFO", "NIFTY", {"ltp": 5.0, "ltt": 1_700_000_000}, date(2026, 11, 18)
    )
    assert row2["ts_exch"] == 1_700_000_000_000


def test_empty_lake_reads_are_safe(lake):
    assert lake.query_ticks("NONE", "2026-11-18", "2026-11-18").empty
    assert lake.get_ohlcv("NONE", "NFO", "1m").empty
    assert lake.available_days() == []
