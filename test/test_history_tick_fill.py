"""History fills the days the broker has not published from the tick store."""

from datetime import datetime

import pandas as pd
import pytz

import database.ticks_store as ticks_store
from services import history_service as hs

IST = pytz.timezone("Asia/Kolkata")


def ts(day: str, hhmm: str) -> int:
    return int(IST.localize(datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M")).timestamp())


def bar(t: int, close: float) -> dict:
    return {
        "timestamp": t,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 0,
        "oi": 0,
    }


BROKER = [bar(ts("2026-11-20", "09:15"), 25784.75), bar(ts("2026-11-20", "15:25"), 25800.6)]


def setup(monkeypatch, tmp_path, days):
    for d in days:
        (tmp_path / f"date={d}").mkdir()
    monkeypatch.setenv("TICKS_RECORDER_ENABLED", "true")
    monkeypatch.setattr(ticks_store, "data_dir", lambda: str(tmp_path))
    asked = []

    def fake_ohlcv(symbol, exchange, interval, start, end):
        asked.append(datetime.fromtimestamp(start, IST).date().isoformat())
        day = datetime.fromtimestamp(start, IST).date().isoformat()
        return pd.DataFrame([bar(ts(day, "09:15"), 25366.4)])

    monkeypatch.setattr(ticks_store, "get_ohlcv", fake_ohlcv)
    return asked


def test_the_last_session_the_broker_lacks_comes_from_the_tick_store(monkeypatch, tmp_path):
    asked = setup(monkeypatch, tmp_path, ["2026-11-20", "2026-11-23"])
    ok, body, status = hs._fill_from_ticks(
        (True, {"status": "success", "data": list(BROKER)}, 200),
        "NIFTY",
        "NSE_INDEX",
        "5m",
        "2026-11-20",
        "2026-11-24",
    )
    assert ok and status == 200
    assert asked == ["2026-11-23"]  # 20 Nov came from the broker and is not asked for
    days = [datetime.fromtimestamp(b["timestamp"], IST).date().isoformat() for b in body["data"]]
    assert days == ["2026-11-20", "2026-11-20", "2026-11-23"]
    assert body["data"][:2] == BROKER


def test_nothing_missing_leaves_the_answer_as_it_was(monkeypatch, tmp_path):
    asked = setup(monkeypatch, tmp_path, ["2026-11-20"])
    result = (True, {"status": "success", "data": list(BROKER)}, 200)
    assert (
        hs._fill_from_ticks(result, "NIFTY", "NSE_INDEX", "5m", "2026-11-20", "2026-11-24")
        is result
    )
    assert asked == []


def test_daily_bars_and_a_recorder_that_is_off_are_left_alone(monkeypatch, tmp_path):
    asked = setup(monkeypatch, tmp_path, ["2026-11-23"])
    result = (True, {"status": "success", "data": list(BROKER)}, 200)
    assert (
        hs._fill_from_ticks(result, "NIFTY", "NSE_INDEX", "D", "2026-11-20", "2026-11-24") is result
    )
    monkeypatch.setenv("TICKS_RECORDER_ENABLED", "false")
    assert (
        hs._fill_from_ticks(result, "NIFTY", "NSE_INDEX", "5m", "2026-11-20", "2026-11-24")
        is result
    )
    assert asked == []


def test_a_day_outside_the_range_is_not_filled(monkeypatch, tmp_path):
    asked = setup(monkeypatch, tmp_path, ["2026-11-18", "2026-11-23"])
    hs._fill_from_ticks(
        (True, {"status": "success", "data": list(BROKER)}, 200),
        "NIFTY",
        "NSE_INDEX",
        "5m",
        "2026-11-20",
        "2026-11-22",
    )
    assert asked == []


def test_index_bars_after_the_1530_close_are_dropped(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path, ["2026-11-23"])
    monkeypatch.setattr(
        ticks_store,
        "get_ohlcv",
        lambda *a: pd.DataFrame(
            [bar(ts("2026-11-23", hm), 1.0) for hm in ("15:25", "15:30", "15:35")]
        ),
    )
    _, body, _ = hs._fill_from_ticks(
        (True, {"status": "success", "data": list(BROKER)}, 200),
        "NIFTY",
        "NSE_INDEX",
        "5m",
        "2026-11-20",
        "2026-11-24",
    )
    assert [b["timestamp"] for b in body["data"][2:]] == [ts("2026-11-23", "15:25")]
