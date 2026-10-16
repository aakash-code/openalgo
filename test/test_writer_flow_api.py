"""The Writer Flow API over a small recorded day.

The day: 23000 CE covered for twenty minutes through a rally while 23000 PE
was written, the two halves of a bull run. The page colours by what a move
means for the underlying, so both must come back bullish.
"""

from datetime import date, datetime, timedelta

import pytest
import pytz
from flask import Flask

import database.oi_flow_db as db
import services.writer_flow_service as svc
from blueprints.writer_flow import writer_flow_bp

DAY = date(2026, 10, 16)
T0 = datetime(2026, 10, 16, 9, 15)


def sample(minute, strike, side, oi, ltp, spot):
    return {
        "trade_date": DAY,
        "sample_time": T0 + timedelta(minutes=minute),
        "underlying": "NIFTY",
        "exchange": "NFO",
        "expiry": "19OCT26",
        "strike": strike,
        "side": side,
        "symbol": f"NIFTY19OCT26{int(strike)}{side}",
        "oi": oi,
        "ltp": ltp,
        "volume": 1,
        "spot": spot,
        "lot_size": 65,
        "source": "live",
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OI_FLOW_DATABASE_PATH", str(tmp_path / "oi_flow.duckdb"))
    monkeypatch.setenv("OI_FLOW_RECORDER_ENABLED", "true")
    svc.forget_views()
    db.init_oi_flow_database()
    rows = []
    for m in range(21):
        spot = 22950 + m * 5
        rows.append(sample(m, 23000.0, "CE", 3_000_000 - m * 50_000, 40 + m * 2, spot))
        rows.append(sample(m, 23000.0, "PE", 1_000_000 + m * 40_000, 90 - m * 2, spot))
        rows.append(sample(m, 23050.0, "CE", 2_000_000, 25.0, spot))
        rows.append(sample(m, 23050.0, "PE", 500_000, 120.0, spot))
        if m < 5:
            # Sampled while near the money at the open, then out of the window.
            rows.append(sample(m, 22800.0, "CE", 900_000, 200.0, spot))
    db.insert_samples(rows)
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="writer-flow-tests")
    app.register_blueprint(writer_flow_bp)
    c = app.test_client()
    with c.session_transaction() as s:
        s["logged_in"] = True
        s["user"] = "trader"
        s["login_time"] = datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()
    return c


def test_the_day_reads_as_a_bull_run(client):
    r = client.get("/writerflow/api/flow?underlying=NIFTY&step=60")
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == "success"
    assert (body["expiry"], body["date"], body["step_seconds"]) == ("19OCT26", "2026-10-16", 60)
    assert body["strikes"] == [23000.0, 23050.0]
    assert len(body["times"]) == 21 and body["times"][0] == "09:15"
    # Heatmap rows match the strikes, columns the times; the first minute has
    # nothing to compare with.
    assert len(body["heat"]["ce"]) == 2 and len(body["heat"]["ce"][0]) == 21
    assert body["heat"]["ce"][0][0] is None
    # Call covering and put writing are both bullish: positive.
    assert body["heat"]["ce"][0][5] == 50_000 and body["heat_cls"]["ce"][0][5] == "C"
    assert body["heat"]["pe"][0][5] == 40_000 and body["heat_cls"]["pe"][0][5] == "W"
    # The untouched strike is steady: zero.
    assert body["heat"]["ce"][1][5] == 0 and body["heat_cls"]["ce"][1][5] == "S"
    row = body["ladder"][0]
    assert (row["ce"]["cls"], row["ce"]["bias"], row["ce"]["streak_minutes"]) == ("covering", 1, 20)
    assert (row["pe"]["cls"], row["pe"]["bias"]) == ("writing", 1)
    assert body["ladder"][1]["ce"]["cls"] == "steady"
    kinds = {(e["side"], e["kind"], e["minutes"], e["bias"]) for e in body["events"]}
    assert ("CE", "covering", 20, 1) in kinds and ("PE", "writing", 20, 1) in kinds
    assert body["totals"][5]["ce_change"] == -50_000 and body["totals"][5]["spot"] == 22975


def test_sessions_list_what_is_recorded(client):
    body = client.get("/writerflow/api/sessions").get_json()
    assert body["recorder_enabled"] is True
    assert body["sessions"] == [
        {
            "trade_date": "2026-10-16",
            "underlying": "NIFTY",
            "expiry": "19OCT26",
            "minutes": 21,
            "last_sample": "09:35",
        }
    ]


@pytest.mark.parametrize(
    "recorder, words", [("true", "Nothing recorded"), ("false", "recorder is off")]
)
def test_an_underlying_with_no_record_says_why(client, monkeypatch, recorder, words):
    monkeypatch.setenv("OI_FLOW_RECORDER_ENABLED", recorder)
    body = client.get("/writerflow/api/flow?underlying=BANKNIFTY").get_json()
    assert body["status"] == "empty"
    assert words in body["message"]


@pytest.mark.parametrize(
    "query",
    [
        "underlying=nifty%3B",
        "underlying=NIFTY&expiry=19-10-26",
        "underlying=NIFTY&step=7",
        "underlying=NIFTY&noise_pct=abc",
    ],
)
def test_bad_input_is_refused_in_words(client, query):
    r = client.get(f"/writerflow/api/flow?{query}")
    assert r.status_code == 400
    msg = r.get_json()["message"]
    assert msg and "Traceback" not in msg and "400" not in msg


def test_a_logged_out_visitor_gets_nothing(client):
    with client.session_transaction() as s:
        s.clear()
    r = client.get(
        "/writerflow/api/flow?underlying=NIFTY", headers={"X-Requested-With": "XMLHttpRequest"}
    )
    assert r.status_code in (302, 401)


def test_a_strike_out_of_the_window_is_left_out_unless_asked_for(client):
    body = client.get("/writerflow/api/flow?underlying=NIFTY&step=60").get_json()
    assert 22800.0 not in body["strikes"]
    assert all(r["observed"] for r in body["ladder"])
    every = client.get("/writerflow/api/flow?underlying=NIFTY&step=60&all_strikes=1").get_json()
    assert every["strikes"] == [22800.0, 23000.0, 23050.0]
    row = every["ladder"][0]
    assert row["observed"] is False and row["ce"]["last_seen"] == "09:19"
    # Its history is still there for the chart to paint; its present is not.
    assert every["heat"]["ce"][0][4] == 0 and every["heat"]["ce"][0][5] is None


def test_times_come_back_as_utc_seconds_for_the_chart(client):
    body = client.get("/writerflow/api/flow?underlying=NIFTY&step=60").get_json()
    # 09:15 IST on 16-Oct-2026 is 03:45 UTC.
    assert body["times_epoch"][0] == 1792122300
    assert body["totals"][0]["epoch"] == 1792122300
    assert (
        body["events"][0]["epoch"]
        == body["times_epoch"][body["times"].index(body["events"][0]["time"])]
    )
