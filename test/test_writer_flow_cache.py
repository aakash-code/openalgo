"""Writer Flow answers are built once per recorded write and shared.

What has to hold: a refresh with nothing new recorded does not read the day
again, a write makes the next refresh rebuild, two viewers asking at once wait
on one build, the recorder rebuilds only what somebody is watching, and the
answer goes out gzipped to a browser that accepts it.
"""

import gzip
import json
import threading
import time
from datetime import date, datetime, timedelta

import pytest
import pytz
from flask import Flask

import database.oi_flow_db as db
import services.writer_flow_service as svc
from blueprints.writer_flow import writer_flow_bp
from utils.thread_safe_cache import LockedTTLCache

T0 = datetime(2026, 10, 16, 9, 15)


def minute_rows(m, underlying="NIFTY"):
    return [
        {
            "trade_date": date(2026, 10, 16),
            "sample_time": T0 + timedelta(minutes=m),
            "underlying": underlying,
            "exchange": "NFO",
            "expiry": "19OCT26",
            "strike": 23000.0,
            "side": side,
            "symbol": f"{underlying}19OCT2623000{side}",
            "oi": 1_000_000 + m * 50_000 * (1 if side == "PE" else -1),
            "ltp": 50.0,
            "volume": 1,
            "spot": 23000.0,
            "lot_size": 65,
            "source": "live",
        }
        for side in ("CE", "PE")
    ]


@pytest.fixture
def reads(tmp_path, monkeypatch):
    """Counts how often the day is read from the record."""
    monkeypatch.setenv("OI_FLOW_DATABASE_PATH", str(tmp_path / "oi_flow.duckdb"))
    monkeypatch.setenv("OI_FLOW_RECORDER_ENABLED", "true")
    svc.forget_views()
    db.init_oi_flow_database()
    for m in range(5):
        db.insert_samples(minute_rows(m))
    calls = []
    real = svc.load_day

    def counted(*args):
        calls.append(args)
        return real(*args)

    monkeypatch.setattr(svc, "load_day", counted)
    return calls


@pytest.fixture
def client(reads):
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="writer-flow-cache-tests")
    app.register_blueprint(writer_flow_bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["logged_in"] = True
        s["user"] = "trader"
        s["login_time"] = datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()
    return c


NIFTY = svc.FlowView(underlying="NIFTY", step="60")


def test_a_refresh_with_nothing_new_does_not_read_the_day_again(reads):
    first = svc.flow_answer(NIFTY)
    assert svc.flow_answer(NIFTY) is first
    assert len(reads) == 1


def test_a_write_makes_the_next_refresh_rebuild_and_show_it(reads):
    before = json.loads(svc.flow_answer(NIFTY).body)
    db.insert_samples(minute_rows(5))
    after = json.loads(svc.flow_answer(NIFTY).body)
    assert len(reads) == 2
    assert before["times"][-1] == "09:19" and after["times"][-1] == "09:20"


def test_a_write_to_another_underlying_leaves_this_answer_alone(reads):
    svc.flow_answer(NIFTY)
    db.insert_samples(minute_rows(0, underlying="BANKNIFTY"))
    svc.flow_answer(NIFTY)
    assert len(reads) == 1


def test_two_viewers_asking_at_once_wait_on_one_build(reads, monkeypatch):
    slow_real = svc.analyse

    def slow(*a, **k):
        time.sleep(0.3)
        return slow_real(*a, **k)

    monkeypatch.setattr(svc, "analyse", slow)
    start = threading.Barrier(2)
    answers = []

    def ask():
        start.wait()
        answers.append(svc.flow_answer(NIFTY))

    threads = [threading.Thread(target=ask) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(answers) == 2 and answers[0] is answers[1]
    assert len(reads) == 1


def test_the_recorder_rebuilds_what_is_watched_and_only_that(reads):
    svc.flow_answer(NIFTY)
    db.insert_samples(minute_rows(5))
    assert svc.rebuild_watched({"NIFTY"}) == 1
    assert len(reads) == 2
    # The viewer's next refresh is served from memory.
    svc.flow_answer(NIFTY)
    assert len(reads) == 2
    # Nobody watches BANKNIFTY: nothing is built for it.
    assert svc.rebuild_watched({"BANKNIFTY"}) == 0


def test_a_view_nobody_asks_for_stops_being_rebuilt(reads, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(svc, "_asked", LockedTTLCache(maxsize=64, ttl=600, timer=lambda: now[0]))
    svc.flow_answer(NIFTY)
    # Rebuilding after writes must not count as somebody watching.
    for m in range(5, 8):
        now[0] += 300
        db.insert_samples(minute_rows(m))
        svc.rebuild_watched({"NIFTY"})
    assert len(reads) == 2  # rebuilt once, at 300 s; dropped by 900 s
    assert svc.rebuild_watched({"NIFTY"}) == 0


def test_the_answer_goes_out_gzipped_when_the_browser_accepts_it(client):
    url = "/writerflow/api/flow?underlying=NIFTY&step=60"
    plain = client.get(url)
    zipped = client.get(url, headers={"Accept-Encoding": "gzip, deflate"})
    assert "Content-Encoding" not in plain.headers
    assert zipped.headers["Content-Encoding"] == "gzip"
    assert "Accept-Encoding" in zipped.headers["Vary"]
    assert json.loads(gzip.decompress(zipped.get_data())) == plain.get_json()
    assert plain.get_json()["status"] == "success"


def test_the_chart_can_leave_out_what_it_does_not_draw(client):
    full = client.get("/writerflow/api/flow?underlying=NIFTY&step=60").get_json()
    lite = client.get("/writerflow/api/flow?underlying=NIFTY&step=60&lite=1").get_json()
    assert "heat_cls" in full and "heat_cls" not in lite
    assert {k: v for k, v in full.items() if k != "heat_cls"} == lite
