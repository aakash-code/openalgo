"""The option levels service and route, over the recorded morning of 21-Oct-2026
stored into a fresh Writer Flow record exactly as the recorder stores it."""

from __future__ import annotations

import gzip
import json
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import pytz
from flask import Flask

import database.oi_flow_db as db
import services.oi_levels_service as svc

FIXTURE = Path(__file__).parent / "data" / "market_read_21oct2026.json.gz"
DAY = date(2026, 10, 21)
EXPIRY = "27OCT26"


def _rows(day: date) -> list[dict]:
    doc = json.loads(gzip.open(FIXTURE, "rt").read())
    shift = day - DAY
    return [
        {
            "trade_date": day,
            "sample_time": datetime.fromisoformat(f"2026-10-21T{t}:00") + shift,
            "underlying": "NIFTY",
            "exchange": "NFO",
            "expiry": EXPIRY,
            "strike": k,
            "side": side,
            "symbol": f"NIFTY{EXPIRY}{int(k)}{side}",
            "oi": oi,
            "ltp": ltp,
            "volume": vol,
            "spot": spot,
            "lot_size": 65,
            "source": "live",
        }
        for t, k, side, oi, ltp, vol, spot in doc["samples"]["NIFTY"]
    ]


def _store(rows):
    import pandas as pd

    frame = pd.DataFrame(rows, columns=list(db.SAMPLE_COLUMNS))  # noqa: F841 - read by DuckDB
    with db.get_connection() as conn:
        conn.execute("INSERT INTO oi_flow_samples SELECT * FROM frame")


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    """The morning stored once, plus the same morning a day earlier as a past day."""
    path = tmp_path_factory.mktemp("record") / "oi_flow.duckdb"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("OI_FLOW_DATABASE_PATH", str(path))
        db.init_oi_flow_database()
        _store(_rows(DAY))
        _store(_rows(DAY - timedelta(days=1)))
    return path


@pytest.fixture
def recorded(tmp_path, monkeypatch, template):
    import shutil

    shutil.copy(template, tmp_path / "oi_flow.duckdb")
    monkeypatch.setenv("OI_FLOW_DATABASE_PATH", str(tmp_path / "oi_flow.duckdb"))
    monkeypatch.setenv("OI_FLOW_RECORDER_ENABLED", "true")
    # The record keeps the days within its window: pretend today is the 21st.
    real = db.available_sessions

    def sessions(limit_days=10):
        return [s for s in real(limit_days=3650) if s["trade_date"] <= DAY]

    monkeypatch.setattr(svc, "available_sessions", sessions)
    svc.forget_views()
    yield
    svc.forget_views()


@pytest.fixture
def client(recorded):
    from blueprints.writer_flow import writer_flow_bp

    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="oi-levels-tests")
    app.register_blueprint(writer_flow_bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["logged_in"] = True
        s["user"] = "trader"
        s["login_time"] = datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()
    return c


def test_the_route_returns_the_zones_for_a_recorded_day(client):
    body = client.get("/writerflow/api/levels?underlying=NIFTY&date=2026-10-21").get_json()
    assert body["status"] == "success" and body["expiry"] == EXPIRY and body["minute"] == "10:23"
    roles = {z["role"]: z for z in body["zones"]}
    assert roles["support"]["anchor"] == 22500 and roles["resistance"]["anchor"] == 23000
    assert len(body["history"]["epochs"]) == len(body["history"]["support"]) == 69
    assert all(isinstance(e, int) for e in body["history"]["epochs"])
    assert [c["kind"] for c in body["changes"]][-1] == "turned"
    assert body["gamma"]["flip"] is not None and len(body["gamma"]["epochs"]) >= 10


def test_walls_carry_the_rolling_month_strength(client):
    body = client.get("/writerflow/api/levels?underlying=NIFTY&date=2026-10-21").get_json()
    walls = {w["strike"]: w for w in body["walls"]}
    assert walls, "the day should carry its strikes"
    # Every strike carries its month persistence for put and call writers, each
    # a share (0..1) of the strongest wall over the recorded cycle.
    for w in walls.values():
        assert 0.0 <= w["persist_put"] <= 1.0 and 0.0 <= w["persist_call"] <= 1.0
    # The heaviest wall of the cycle reaches full strength on its side.
    assert max(w["persist_put"] for w in walls.values()) == pytest.approx(1.0) or max(
        w["persist_call"] for w in walls.values()
    ) == pytest.approx(1.0)
    # The 23,000 put wall and the 22,500 support both register as persistent.
    assert walls[23000]["persist_put"] > 0 and walls[22500]["persist_put"] > 0


def test_earlier_days_count_in_the_hold_rates(client):
    body = client.get("/writerflow/api/levels?underlying=NIFTY&date=2026-10-21").get_json()
    today = [x for x in body["touches"] if x["outcome"] in ("held", "broke")]
    judged = sum(row["n"] for row in body["stats"].values() if isinstance(row, dict))
    assert body["stats"]["days"] == 2
    assert judged == 2 * len(today)  # the same morning recorded twice


def test_a_day_with_nothing_recorded_says_so(client):
    body = client.get("/writerflow/api/levels?underlying=NIFTY&date=2026-10-19").get_json()
    assert body["status"] == "empty" and "Nothing recorded" in body["message"]


def test_bad_input_is_refused_in_words(client):
    r = client.get("/writerflow/api/levels?underlying=nifty 50")
    assert r.status_code == 400 and "underlying" in r.get_json()["message"]
    r = client.get("/writerflow/api/levels?underlying=NIFTY&date=21-10-2026")
    assert r.status_code == 400 and "2026-10-21" in r.get_json()["message"]


def test_one_build_per_recorded_minute_and_on_the_builder_thread(recorded, monkeypatch):
    builds = []
    real = svc.levels_payload

    def counted(view):
        builds.append(threading.current_thread().name)
        return real(view)

    monkeypatch.setattr(svc, "levels_payload", counted)
    view = svc.LevelsView("NIFTY", DAY)
    svc.levels_answer(view)
    svc.levels_answer(view)
    assert builds == ["writer-flow-builder"]
    monkeypatch.setattr(svc, "generation", lambda u: 99)  # the recorder wrote a minute
    svc.levels_answer(view)
    assert len(builds) == 2


def test_past_days_are_worked_out_once(recorded, monkeypatch):
    loads = []
    real = svc.load_day

    def counted(underlying, expiry, day, pause=None):
        loads.append(day)
        return real(underlying, expiry, day, pause=pause)

    monkeypatch.setattr(svc, "load_day", counted)
    view = svc.LevelsView("NIFTY", DAY)
    svc.levels_answer(view)
    past_after_first = loads.count(DAY - timedelta(days=1))
    monkeypatch.setattr(svc, "generation", lambda u: 99)  # a new minute arrives
    svc.levels_answer(view)
    # A finished day is cached (hold rates and the month strength each read it,
    # but once): the rebuild reads only today again, never the earlier day.
    assert loads.count(DAY - timedelta(days=1)) == past_after_first
    assert loads.count(DAY) == 2


def test_the_recorder_rebuilds_only_what_a_chart_asked_for(recorded, monkeypatch):
    assert svc.rebuild_watched({"NIFTY"}) == 0
    svc.levels_answer(svc.LevelsView("NIFTY", DAY))
    assert svc.rebuild_watched({"NIFTY"}) == 1 and svc.rebuild_watched({"SENSEX"}) == 0
