"""Charting an underlying that is not listed starts recording it.

What has to hold: a chart of TECHM27OCT26FUT on NFO starts TECHM on NSE (an
index future starts the index), a listed underlying is left to the list, the
number recorded this way is capped even when charts ask at once, an
underlying with no options is dropped and said so, one nobody charts any more
is let go, the history fill leaves room for the chart's own history loads, and
only the chart's request starts anything.
"""

import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import pytz
from flask import Flask

import database.oi_flow_db as db
import services.oi_flow_recorder_service as rec
import services.writer_flow_service as svc
from utils.thread_safe_cache import LockedTTLCache

IST = ZoneInfo("Asia/Kolkata")
FRIDAY_1031 = datetime(2026, 10, 16, 10, 31, 5, tzinfo=IST)
INDICES = {("NIFTY", "NSE_INDEX"), ("SENSEX", "BSE_INDEX")}
STOCKS = {"TECHM", "INFY", "SBIN", "TCS", "RELIANCE", "HDFCBANK"}


class FakeScheduler:
    def __init__(self):
        self.jobs = []

    def add_job(self, func, trigger=None, args=(), id=None, **_):
        self.jobs.append((func, args, id))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("OI_FLOW_DATABASE_PATH", str(tmp_path / "oi_flow.duckdb"))
    monkeypatch.setenv("OI_FLOW_UNDERLYINGS", "NIFTY:NSE_INDEX")
    monkeypatch.setenv("OI_FLOW_RECORDER_ENABLED", "true")
    monkeypatch.setenv("OI_FLOW_STRIKES_AROUND_ATM", "1")
    monkeypatch.delenv("OI_FLOW_ON_DEMAND_MAX", raising=False)
    monkeypatch.setattr(rec, "_api_key", lambda: "k")
    monkeypatch.setattr(
        rec, "_calendar_window", lambda day, ex: (rec.dtime(9, 15), rec.dtime(15, 30))
    )
    monkeypatch.setattr(rec, "FILL_REST_SECONDS", 0)
    monkeypatch.setattr(rec, "_on_demand", LockedTTLCache(maxsize=rec.ON_DEMAND_CAP, ttl=1800))
    monkeypatch.setattr(rec, "_no_options", LockedTTLCache(maxsize=256, ttl=3600))
    scheduler = FakeScheduler()
    monkeypatch.setattr(rec, "_scheduler", scheduler)
    rec._expiry_cache.clear()
    rec._gap_tries.clear()
    rec._sessions.clear()
    monkeypatch.setattr(rec, "_gap_day", None)

    import database.token_db as tdb

    def get_token(symbol, exchange):
        if (symbol, exchange) in INDICES or (symbol in STOCKS and exchange in ("NSE", "BSE")):
            return "1"
        return None

    monkeypatch.setattr(tdb, "get_token", get_token)
    svc.forget_views()
    db.init_oi_flow_database()
    yield scheduler


@pytest.fixture
def fake_broker(monkeypatch):
    calls = {"chain": [], "history": []}

    def get_expiry_dates(symbol, exchange, instrumenttype, api_key=None):
        if symbol == "SBIN":
            return False, {"status": "error", "message": "none"}, 404
        return True, {"status": "success", "data": ["27-OCT-26"]}, 200

    def get_option_chain(
        underlying, exchange, expiry, strike_count, api_key, with_quotes=True, **_
    ):
        calls["chain"].append((underlying, exchange))
        return (
            True,
            {
                "underlying_ltp": 1450.0,
                "chain": [
                    {
                        "strike": k,
                        "ce": {
                            "symbol": f"{underlying}27OCT26{k}CE",
                            "oi": 9000,
                            "ltp": 20.0,
                            "lotsize": 600,
                        },
                        "pe": {
                            "symbol": f"{underlying}27OCT26{k}PE",
                            "oi": 7000,
                            "ltp": 18.0,
                            "lotsize": 600,
                        },
                    }
                    for k in (1440, 1460)
                ],
            },
            200,
        )

    def get_history(symbol, exchange, interval, start, end, api_key=None, background=False, **_):
        calls["history"].append((symbol, exchange, background))
        base = int(datetime(2026, 10, 16, 9, 15, tzinfo=IST).timestamp())
        return (
            True,
            {
                "data": [
                    {"timestamp": base + 60 * i, "close": 20.0 + i, "oi": 9000 + i}
                    for i in range(3)
                ]
            },
            200,
        )

    import services.expiry_service as es
    import services.history_service as hs
    import services.option_chain_service as ocs

    monkeypatch.setattr(es, "get_expiry_dates", get_expiry_dates)
    monkeypatch.setattr(ocs, "get_option_chain", get_option_chain)
    monkeypatch.setattr(hs, "get_history", get_history)
    return calls


def test_a_stock_future_chart_starts_recording_the_stock(isolated, fake_broker, monkeypatch):
    assert rec.request_recording("TECHM", "NFO") == "started"
    assert ("TECHM", "NSE") in rec.recorded_underlyings()
    func, args, _ = isolated.jobs[0]
    monkeypatch.setattr(
        rec, "datetime", type("D", (datetime,), {"now": staticmethod(lambda tz=None: FRIDAY_1031)})
    )
    func(*args)
    assert ("TECHM", "NSE") in fake_broker["chain"]
    held = {s["underlying"] for s in db.available_sessions()}
    assert "TECHM" in held
    # The day so far came from history too, behind the shared queue.
    assert any(sym.startswith("TECHM27OCT26") and bg for sym, _, bg in fake_broker["history"])
    # From now on the minute beat records it with the listed ones.
    fake_broker["chain"].clear()
    rec.record_once(FRIDAY_1031.replace(minute=40))
    assert [u for u, _ in fake_broker["chain"]] == ["NIFTY", "TECHM"]


def test_an_index_future_chart_starts_the_index_not_a_stock(isolated):
    assert rec.request_recording("SENSEX", "BFO") == "started"
    assert dict(rec.recorded_underlyings())["SENSEX"] == "BSE_INDEX"


def test_a_listed_underlying_is_left_to_the_list(isolated):
    assert rec.request_recording("NIFTY", "NFO") == "listed"
    assert isolated.jobs == []


def test_nothing_starts_while_the_recorder_is_off(isolated, monkeypatch):
    monkeypatch.setattr(rec, "_scheduler", None)
    assert rec.request_recording("TECHM", "NFO") == "off"
    monkeypatch.setattr(rec, "_scheduler", isolated)
    monkeypatch.setenv("OI_FLOW_ON_DEMAND_MAX", "0")
    assert rec.request_recording("TECHM", "NFO") == "off"
    assert rec.recorded_underlyings() == [("NIFTY", "NSE_INDEX")]


def test_an_unknown_name_is_not_recorded(isolated):
    assert rec.request_recording("NOSUCH", "NFO") == "unknown"
    assert isolated.jobs == []


def test_the_cap_refuses_rather_than_dropping_one_being_watched(isolated, monkeypatch):
    monkeypatch.setenv("OI_FLOW_ON_DEMAND_MAX", "2")
    assert rec.request_recording("TECHM", "NFO") == "started"
    assert rec.request_recording("INFY", "NFO") == "started"
    assert rec.request_recording("TCS", "NFO") == "full"
    assert rec.request_recording("TECHM", "NFO") == "recording"
    assert {u for u, _ in rec.recorded_underlyings()} == {"NIFTY", "TECHM", "INFY"}


def test_charts_asking_at_once_cannot_pass_the_cap(isolated, monkeypatch):
    monkeypatch.setenv("OI_FLOW_ON_DEMAND_MAX", "2")
    names = ["TECHM", "INFY", "TCS", "RELIANCE", "HDFCBANK"]
    start = threading.Barrier(len(names))
    answers = {}

    def ask(name):
        start.wait()
        answers[name] = rec.request_recording(name, "NFO")

    threads = [threading.Thread(target=ask, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert sorted(answers.values()).count("started") == 2
    assert len(rec._on_demand) == 2


def test_an_underlying_with_no_options_is_dropped_and_said_so(isolated, fake_broker):
    assert rec.request_recording("SBIN", "NFO") == "started"
    func, args, _ = isolated.jobs[0]
    func(*args)
    assert "SBIN" not in dict(rec.recorded_underlyings())
    assert rec.request_recording("SBIN", "NFO") == "no_options"
    assert len(isolated.jobs) == 1


def test_an_underlying_nobody_charts_any_more_is_let_go(isolated, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(
        rec, "_on_demand", LockedTTLCache(maxsize=rec.ON_DEMAND_CAP, ttl=1800, timer=lambda: now[0])
    )
    rec.request_recording("TECHM", "NFO")
    # A chart asking every minute keeps it.
    for _ in range(40):
        now[0] += 60
        assert rec.request_recording("TECHM", "NFO") == "recording"
    now[0] += 1801
    assert "TECHM" not in dict(rec.recorded_underlyings())


def test_the_history_fill_rests_between_calls(isolated, fake_broker, monkeypatch):
    # `rec.time` is the process-wide time module, so every thread's sleep lands
    # here while it is patched: the scheduler's, and any a previous test left
    # running. Only the fill's own rests count; one stray sleep from another
    # thread made this fail in the full suite and pass on its own.
    rests = []
    me = threading.get_ident()
    monkeypatch.setattr(rec, "FILL_REST_SECONDS", 0.7)
    monkeypatch.setattr(
        rec.time, "sleep", lambda s: rests.append(s) if threading.get_ident() == me else None
    )
    other = threading.Thread(target=lambda: time.sleep(0.01))
    other.start()
    other.join()
    rec.request_recording("TECHM", "NFO")
    rec.fill_gaps(FRIDAY_1031)
    legs = [h for h in fake_broker["history"] if h[0].endswith(("CE", "PE"))]
    assert legs and len(rests) == len(legs) and set(rests) == {0.7}


@pytest.fixture
def client():
    from blueprints.writer_flow import writer_flow_bp

    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="writer-flow-on-demand-tests")
    app.register_blueprint(writer_flow_bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["logged_in"] = True
        s["user"] = "trader"
        s["login_time"] = datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()
    return c


def test_the_chart_is_told_recording_started_and_the_page_starts_nothing(isolated, client):
    page = client.get("/writerflow/api/flow?underlying=TECHM&step=60").get_json()
    assert page["status"] == "empty" and isolated.jobs == []
    chart = client.get(
        "/writerflow/api/flow?underlying=TECHM&step=60&lite=1&exchange=NFO"
    ).get_json()
    assert chart["status"] == "empty" and chart["recording"] is True
    assert chart["message"].startswith("Started recording TECHM")
    again = client.get(
        "/writerflow/api/flow?underlying=TECHM&step=60&lite=1&exchange=NFO"
    ).get_json()
    assert again["message"].startswith("Recording TECHM")
    # A past day is read, never started.
    client.get("/writerflow/api/flow?underlying=INFY&step=60&date=2026-10-15&exchange=NFO")
    assert "INFY" not in dict(rec.recorded_underlyings())
