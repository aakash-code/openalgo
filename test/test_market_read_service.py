"""The market read service: fed by the recorder, one quote a minute, events once.

Runs over the recorded morning of 21-Oct-2026 (test/data), stored into a fresh
Writer Flow record exactly as the recorder stores it.
"""

from __future__ import annotations

import gzip
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import pytz
from flask import Flask

import database.oi_flow_db as db
import services.market_read_service as mr

FIXTURE = Path(__file__).parent / "data" / "market_read_21oct2026.json.gz"
DAY = date(2026, 10, 21)
EXPIRIES = {"NIFTY": "27OCT26", "BANKNIFTY": "27OCT26", "SENSEX": "22OCT26"}
FUT = ("NIFTY27OCT26FUT", "NIFTY23NOV26FUT")


def _t(hm: str) -> datetime:
    return datetime.fromisoformat(f"2026-10-21T{hm}:00")


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    """The morning's options stored once, in one bulk insert, for each test to copy."""
    import pandas as pd

    path = tmp_path_factory.mktemp("record") / "oi_flow.duckdb"
    doc = json.loads(gzip.open(FIXTURE, "rt").read())
    rows = []
    for u, samples in doc["samples"].items():
        for t, k, side, oi, ltp, vol, spot in samples:
            rows.append(
                {
                    "trade_date": DAY,
                    "sample_time": _t(t),
                    "underlying": u,
                    "exchange": "BFO" if u == "SENSEX" else "NFO",
                    "expiry": EXPIRIES[u],
                    "strike": k,
                    "side": side,
                    "symbol": f"{u}{EXPIRIES[u]}{int(k)}{side}",
                    "oi": oi,
                    "ltp": ltp,
                    "volume": vol,
                    "spot": spot,
                    "lot_size": 65,
                    "source": "live",
                }
            )
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("OI_FLOW_DATABASE_PATH", str(path))
        db.init_oi_flow_database()
        frame = pd.DataFrame(rows, columns=list(db.SAMPLE_COLUMNS))  # noqa: F841 - read by DuckDB
        with db.get_connection() as conn:
            conn.execute("INSERT INTO oi_flow_samples SELECT * FROM frame")
    return path, doc


@pytest.fixture
def recorded(tmp_path, monkeypatch, template):
    """A fresh copy of the stored morning, the broker faked, the service fresh."""
    import shutil

    source, doc = template
    shutil.copy(source, tmp_path / "oi_flow.duckdb")
    monkeypatch.setenv("OI_FLOW_DATABASE_PATH", str(tmp_path / "oi_flow.duckdb"))
    market = {t: (f, oi, v) for t, f, oi, v in doc["market"]}

    calls = {"quotes": [], "history": [], "telegram": [], "emitted": []}

    def multiquotes(symbols, api_key=None):
        calls["quotes"].append([s["symbol"] for s in symbols])
        hm = calls["now"].strftime("%H:%M")
        fut, oi, vix = market.get(hm, (None, None, None))
        results = [
            {"symbol": FUT[0], "exchange": "NFO", "data": {"ltp": fut, "oi": oi - 3_000_000}},
            {"symbol": FUT[1], "exchange": "NFO", "data": {"ltp": fut + 90, "oi": 3_000_000}},
            {"symbol": "INDIAVIX", "exchange": "NSE_INDEX", "data": {"ltp": vix}},
            {
                "symbol": "NIFTY",
                "exchange": "NSE_INDEX",
                "data": {"ltp": 1, "prev_close": doc["prev_close"]},
            },
        ]
        return True, {"status": "success", "results": results}, 200

    def history(symbol, exchange, interval, start, end, api_key=None, background=False):
        calls["history"].append((symbol, background))
        return True, {"status": "success", "data": []}, 200

    import database.auth_db as auth
    import extensions
    import services.history_service as hs
    import services.option_symbol_service as oss
    import services.quotes_service as qs

    monkeypatch.setattr(qs, "get_multiquotes", multiquotes)
    monkeypatch.setattr(hs, "get_history", history)
    monkeypatch.setattr(auth, "get_first_available_api_key", lambda: "k")
    monkeypatch.setattr(
        oss,
        "near_month_futures",
        lambda base, exchange, count=1: [
            {"symbol": s, "exchange": "NFO", "expiry": ""} for s in FUT
        ][:count],
    )
    monkeypatch.setattr(
        extensions,
        "emit_from_any_thread",
        lambda event, data: calls["emitted"].append((event, data)),
    )
    monkeypatch.setattr(mr, "_send_telegram", lambda text: calls["telegram"].append(text))
    monkeypatch.setattr(mr, "_settings", None)
    monkeypatch.setattr(mr, "_latest", None)
    monkeypatch.setattr(mr, "_quoted_minute", None)
    monkeypatch.setattr(mr, "_telegram_last", None)
    mr._filled_at.clear()
    mr._telegram_pending.clear()
    return calls


def _update(calls, hm, second=6):
    calls["now"] = _t(hm)
    return mr.update(now=_t(hm) + timedelta(seconds=second))


def test_a_read_costs_one_batch_quote_a_minute(recorded):
    _update(recorded, "10:10")
    _update(recorded, "10:10", second=40)  # BANKNIFTY and SENSEX stored later in the minute
    assert recorded["quotes"] == [[FUT[0], FUT[1], "INDIAVIX", "NIFTY"]]
    # History once, as background work, for the futures and India VIX only.
    assert recorded["history"] == [(FUT[0], True), (FUT[1], True), ("INDIAVIX", True)]


def test_events_are_announced_once(recorded):
    first = _update(recorded, "10:10")
    assert first["headline"].startswith("Call writers lead NIFTY")
    announced = [e for _, data in recorded["emitted"] for e in data["events"]]
    assert {e["kind"] for e in announced} >= {"flow_call", "support_to_resistance"}
    recorded["emitted"].clear()
    _update(recorded, "10:10", second=40)
    ((_, again),) = recorded["emitted"]
    assert again["events"] == []  # nothing new: the same events are not sent twice


def test_a_restart_sends_no_telegram_for_the_morning_it_reads_again(recorded):
    """Events first seen long after they happened are stored, not sent."""
    _update(recorded, "10:23")
    sent = "\n".join(recorded["telegram"])
    assert "Call writers took over" not in sent  # 09:55, 28 minutes earlier


def test_telegram_sends_at_most_one_message_every_ten_minutes(recorded):
    ev = lambda hm, title: {"time": _t(hm), "title": title, "reason": "r"}  # noqa: E731
    mr._queue_telegram([ev("10:00", "first")], now=_t("10:00"))
    mr._queue_telegram([ev("10:03", "second")], now=_t("10:03"))
    mr._queue_telegram([ev("10:06", "third")], now=_t("10:06"))
    assert len(recorded["telegram"]) == 1
    mr._queue_telegram([], now=_t("10:11"))
    assert len(recorded["telegram"]) == 2
    assert "second" in recorded["telegram"][1] and "third" in recorded["telegram"][1]


def test_combined_futures_open_interest_needs_both_months(recorded):
    quotes = [
        {"sample_time": _t("09:20"), "symbol": FUT[0], "ltp": 23000.0, "oi": 100},
        {"sample_time": _t("09:21"), "symbol": FUT[0], "ltp": 23010.0, "oi": 90},
        {"sample_time": _t("09:21"), "symbol": FUT[1], "ltp": 23100.0, "oi": 30},
        {"sample_time": _t("09:22"), "symbol": FUT[0], "ltp": 23020.0, "oi": 80},
    ]
    market, _ = mr.market_minutes("NIFTY", quotes)
    assert [(m.fut, m.fut_oi) for m in market] == [(23000.0, None), (23010.0, 120), (23020.0, 110)]


def test_switches_persist_and_unknown_ones_are_refused(recorded, monkeypatch):
    assert mr.settings() == {"enabled": True, "telegram": True, "sound": True}
    mr.update_settings({"telegram": False})
    monkeypatch.setattr(mr, "_settings", None)  # a restart: read back from the record
    assert mr.settings()["telegram"] is False
    with pytest.raises(ValueError):
        mr.update_settings({"loud": True})


def test_the_recorder_hook_only_queues(recorded, monkeypatch):
    started = []
    monkeypatch.setattr(mr, "_ensure_worker", lambda: started.append(1))
    monkeypatch.setattr(mr, "_queued", False)
    while not mr._jobs.empty():
        mr._jobs.get_nowait()
    mr.on_minute("NIFTY")
    mr.on_minute("BANKNIFTY")  # one waiting read covers both
    mr.on_minute("CRUDEOIL")  # not read
    assert mr._jobs.qsize() == 1 and started == [1]
    mr._jobs.get_nowait()
    monkeypatch.setattr(mr, "_queued", False)
    mr.update_settings({"enabled": False})
    mr.on_minute("NIFTY")
    assert mr._jobs.empty()


def test_the_hook_never_raises(monkeypatch):
    monkeypatch.setattr(mr, "settings", lambda: 1 / 0)
    mr.on_minute("NIFTY")


def test_the_worker_stops(recorded, monkeypatch):
    monkeypatch.setattr(mr, "update", lambda: None)
    mr._ensure_worker()
    worker = mr._worker
    mr.stop_worker()
    assert not worker.is_alive()


@pytest.fixture
def client(recorded):
    from blueprints.writer_flow import writer_flow_bp

    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="market-read-tests")
    app.register_blueprint(writer_flow_bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["logged_in"] = True
        s["user"] = "trader"
        s["login_time"] = datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()
    return c


def test_the_panel_reads_a_stored_day(client):
    body = client.get("/writerflow/api/read?date=2026-10-21").get_json()
    assert body["status"] == "success"
    assert body["minute"] == "10:23" and body["expiry"] == "27OCT26"
    assert body["headline"].startswith("Call writers lead NIFTY")
    assert body["events"][0]["time"] == "09:18" and isinstance(body["events"][0]["epoch"], int)
    assert body["settings"]["sound"] is True


def test_the_panel_says_when_nothing_is_recorded(client):
    body = client.get("/writerflow/api/read?date=2026-10-20").get_json()
    assert body["status"] == "empty" and "Nothing recorded" in body["message"]


def test_the_switches_route(client):
    r = client.post("/writerflow/api/read/settings", json={"sound": False})
    assert r.get_json()["settings"]["sound"] is False
    bad = client.post("/writerflow/api/read/settings", json={"sound": "off"})
    assert bad.status_code == 400 and "on or off" in bad.get_json()["message"]


def _stub_read(events):
    return {
        "underlying": "NIFTY",
        "date": DAY,
        "minute": events[-1]["time"] if events else _t("10:00"),
        "headline": "h",
        "state": {},
        "events": events,
        "series": [],
    }


def _major(hm, kind, key=""):
    return {
        "id": f"{kind}:{key}:{hm.replace(':', '')}",
        "kind": kind,
        "time": _t(hm),
        "level": "major",
        "title": f"{kind} {key}",
        "reason": "r",
    }


def test_a_held_back_telegram_goes_out_without_waiting_for_another_event(recorded, monkeypatch):
    """A turn at 10:03, three minutes after a message at 10:00, waited for the
    next event to carry it; on a quiet afternoon that event never came."""
    reads = {}
    monkeypatch.setattr(mr, "compute", lambda day: reads["now"])
    reads["now"] = _stub_read([_major("10:00", "flow_call")])
    _update(recorded, "10:00")
    reads["now"] = _stub_read([_major("10:00", "flow_call"), _major("10:03", "vix_up")])
    _update(recorded, "10:03")
    assert len(recorded["telegram"]) == 1
    _update(recorded, "10:11")  # nothing new since 10:03
    assert len(recorded["telegram"]) == 2 and "vix" in recorded["telegram"][1]


def test_a_turn_that_moves_a_minute_is_not_announced_again(recorded):
    """A minute filled from history later can move a turn from 09:55 to 09:54;
    it is the same turn, and Telegram must not hear about it twice."""
    first = db.store_read_events("NIFTY", DAY, [_major("09:55", "flow_call")])
    moved = db.store_read_events("NIFTY", DAY, [_major("09:54", "flow_call")])
    later = db.store_read_events("NIFTY", DAY, [_major("10:30", "flow_call")])
    other_strike = db.store_read_events(
        "NIFTY",
        DAY,
        [
            _major("10:07", "support_to_resistance", "23,000"),
            _major("10:08", "support_to_resistance", "23,100"),
        ],
    )
    assert (len(first), len(moved), len(later), len(other_strike)) == (1, 0, 1, 2)


def test_the_worker_releases_its_sessions_after_every_read(monkeypatch):
    """It looks up futures and the API key through scoped sessions each minute,
    on a thread no request teardown reaches."""
    import utils.db_sessions as sessions

    released = []
    monkeypatch.setattr(sessions, "remove_all_scoped_sessions", lambda: released.append(1))
    monkeypatch.setattr(mr, "update", lambda: 1 / 0)  # even when the read fails
    import queue

    jobs = queue.Queue()
    monkeypatch.setattr(mr, "_jobs", jobs)
    jobs.put("NIFTY")
    jobs.put(mr._STOP)
    mr._loop()
    assert released == [1]


def test_the_worker_reads_on_the_hub_and_survives_a_read_that_never_ends(monkeypatch):
    """Under eventlet the worker is a real thread and the read is green work:
    called directly it waited on the hub forever (22-Oct, from 10:52), and
    every later minute found a read still queued and was dropped."""
    import queue

    import utils.db_sessions as sessions

    handed, released = [], []
    monkeypatch.setattr(sessions, "remove_all_scoped_sessions", lambda: released.append(1))
    monkeypatch.setattr(mr, "update", lambda: handed.append("read"))

    def on_hub(fn, *, timeout):
        handed.append(timeout)
        if len(handed) == 1:
            raise TimeoutError  # the first read never comes back
        fn()  # the session is released inside the call that ran on the hub

    monkeypatch.setattr(mr.real_threading, "run_on_hub", on_hub)
    jobs = queue.Queue()
    monkeypatch.setattr(mr, "_jobs", jobs)
    monkeypatch.setattr(mr, "_queued", True)
    jobs.put("NIFTY")
    jobs.put("NIFTY")
    jobs.put(mr._STOP)
    mr._loop()
    assert handed == [mr.READ_TIMEOUT, mr.READ_TIMEOUT, "read"]
    assert released == [1] and mr._queued is False


def test_telegram_text_is_escaped_for_markdown():
    text = mr.telegram_text([{"time": _t("10:00"), "title": "a_b *c*", "reason": "[x]"}])
    assert "a\\_b \\*c\\*" in text and "\\[x]" in text
