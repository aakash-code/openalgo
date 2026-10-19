"""The OI Flow recorder and its database, with the broker replaced by fakes.

What has to hold: a minute is recorded only inside the session, one failed
call never stops the beat, a minute stored twice stays one row, and a backfill
fills gaps without overwriting what the live recorder caught.
"""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

import database.oi_flow_db as db
import services.oi_flow_recorder_service as rec

IST = ZoneInfo("Asia/Kolkata")
FRIDAY_1031 = datetime(2026, 10, 16, 10, 31, 5, tzinfo=IST)


def _default_calendar(day, exchange):
    """The platform's default hours on weekdays, shut at weekends: no database needed."""
    from datetime import time

    from database.market_calendar_db import DEFAULT_MARKET_TIMINGS

    if day.weekday() >= 5:
        return None
    t = DEFAULT_MARKET_TIMINGS[exchange]
    as_time = lambda ms: time(ms // 3_600_000, ms // 60_000 % 60)  # noqa: E731
    return as_time(t["start_offset"]), as_time(t["end_offset"])


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("OI_FLOW_DATABASE_PATH", str(tmp_path / "oi_flow.duckdb"))
    monkeypatch.setenv("OI_FLOW_UNDERLYINGS", "NIFTY:NSE_INDEX")
    monkeypatch.setenv("OI_FLOW_STRIKES_AROUND_ATM", "1")
    monkeypatch.setattr(rec, "_api_key", lambda: "k")
    rec._expiry_cache.clear()
    rec._gap_tries.clear()
    rec._sessions.clear()
    monkeypatch.setattr(rec, "_calendar_window", _default_calendar)
    monkeypatch.setattr(rec, "_gap_day", None)
    monkeypatch.setattr(rec, "FILL_REST_SECONDS", 0)
    db.init_oi_flow_database()
    yield


def chain(oi_ce=1_000_000, oi_pe=800_000, ltp=100.0):
    return {
        "underlying_ltp": 23110.0,
        "chain": [
            {
                "strike": k,
                "ce": {
                    "symbol": f"NIFTY19OCT26{k}CE",
                    "oi": oi_ce,
                    "ltp": ltp,
                    "volume": 10,
                    "lotsize": 65,
                },
                "pe": {
                    "symbol": f"NIFTY19OCT26{k}PE",
                    "oi": oi_pe,
                    "ltp": ltp,
                    "volume": 10,
                    "lotsize": 65,
                },
            }
            for k in (23050, 23100, 23150)
        ],
    }


@pytest.fixture
def fake_broker(monkeypatch):
    calls = {"expiry": 0, "chain": 0, "history": []}

    def get_expiry_dates(symbol, exchange, instrumenttype, api_key=None):
        calls["expiry"] += 1
        assert (symbol, exchange, instrumenttype) == ("NIFTY", "NFO", "options")
        return True, {"status": "success", "data": ["19-OCT-26", "27-OCT-26"]}, 200

    def get_option_chain(
        underlying, exchange, expiry, strike_count, api_key, with_quotes=True, **_
    ):
        calls["chain"] += 1
        assert (underlying, exchange, expiry, strike_count) == ("NIFTY", "NSE_INDEX", "19OCT26", 1)
        return True, chain(), 200

    import services.expiry_service as es
    import services.history_service as hs
    import services.option_chain_service as ocs

    monkeypatch.setattr(es, "get_expiry_dates", get_expiry_dates)
    monkeypatch.setattr(ocs, "get_option_chain", get_option_chain)

    def get_history(symbol, exchange, interval, start, end, api_key=None, background=False, **_):
        calls["history"].append((symbol, background))
        base = int(datetime(2026, 10, 16, 9, 15, tzinfo=IST).timestamp())
        return (
            True,
            {
                "data": [
                    {
                        "timestamp": base + 60 * i,
                        "close": 90.0 + i,
                        "oi": 500_000 + 1000 * i,
                        "volume": 5,
                    }
                    for i in range(3)
                ]
            },
            200,
        )

    monkeypatch.setattr(hs, "get_history", get_history)
    return calls


def test_a_minute_records_every_contract_in_the_window(fake_broker):
    assert rec.record_once(FRIDAY_1031) == 6
    rows = db.load_day("NIFTY", "19OCT26", date(2026, 10, 16))
    assert len(rows) == 6
    assert {r["sample_time"] for r in rows} == {datetime(2026, 10, 16, 10, 31)}
    ce = next(r for r in rows if r["symbol"] == "NIFTY19OCT2623100CE")
    assert (
        ce["side"],
        ce["strike"],
        ce["oi"],
        ce["spot"],
        ce["lot_size"],
        ce["source"],
        ce["exchange"],
    ) == ("CE", 23100.0, 1_000_000, 23110.0, 65, "live", "NFO")


@pytest.mark.parametrize(
    "when",
    [
        datetime(2026, 10, 16, 9, 14, 5, tzinfo=IST),
        datetime(2026, 10, 16, 15, 41, 5, tzinfo=IST),  # F&O closes 15:40
        datetime(2026, 10, 17, 10, 31, 5, tzinfo=IST),  # Saturday
    ],
)
def test_nothing_is_recorded_outside_the_session(fake_broker, when):
    assert rec.record_once(when) == 0
    assert fake_broker["chain"] == 0


def test_the_same_minute_twice_is_one_row_and_the_expiry_is_asked_once(fake_broker):
    rec.record_once(FRIDAY_1031)
    rec.record_once(FRIDAY_1031)
    assert len(db.load_day("NIFTY", "19OCT26", date(2026, 10, 16))) == 6
    assert fake_broker["expiry"] == 1


def test_a_failed_minute_never_raises(monkeypatch, fake_broker):
    import services.option_chain_service as ocs

    def boom(*a, **k):
        raise RuntimeError("broker down")

    monkeypatch.setattr(ocs, "get_option_chain", boom)
    assert rec.record_once(FRIDAY_1031) == 0


def test_backfill_fills_gaps_from_history_without_overwriting_live_minutes(fake_broker):
    # The live recorder caught 09:16 with its own numbers.
    live = rec.chain_samples(
        chain(oi_ce=777), "NIFTY", "NSE_INDEX", "19OCT26", datetime(2026, 10, 16, 9, 16), "live"
    )
    db.insert_samples(live)
    rec.backfill_today(FRIDAY_1031)
    rows = db.load_day("NIFTY", "19OCT26", date(2026, 10, 16))
    at_0916 = [
        r
        for r in rows
        if r["sample_time"] == datetime(2026, 10, 16, 9, 16) and r["symbol"].endswith("23100CE")
    ]
    assert [(r["oi"], r["source"]) for r in at_0916] == [(777, "live")]
    # A candle is the state at the end of its minute, which the live beat reads
    # a minute later: the 09:16 candle is stored at 09:17.
    at_0917 = [
        r
        for r in rows
        if r["sample_time"] == datetime(2026, 10, 16, 9, 17) and r["symbol"].endswith("23100CE")
    ]
    assert [(r["oi"], r["ltp"], r["source"]) for r in at_0917] == [(501_000, 91.0, "backfill")]
    # Nothing in history is the state at 09:15 (its candle ends at 09:16).
    assert not any(r["sample_time"] == datetime(2026, 10, 16, 9, 15) for r in rows)
    # Every history call goes through the shared, paced background gate.
    assert fake_broker["history"] and all(bg for _, bg in fake_broker["history"])


@pytest.mark.parametrize(
    "underlying, expiry",
    [("NIFTY' OR '1'='1", "19OCT26"), ("NIFTY", "19OCT26' OR '1'='1"), ("", "19OCT26")],
)
def test_a_day_is_only_read_for_a_well_formed_name(underlying, expiry):
    # The day filter is written into the SQL as literals, for speed; this is
    # the guard that keeps anything else out of it.
    with pytest.raises(ValueError):
        db.load_day(underlying, expiry, date(2026, 10, 16))
    with pytest.raises(ValueError):
        db.recorded_minutes(underlying, expiry, date(2026, 10, 16))


def test_a_minute_is_stored_whole_or_not_at_all():
    # The second row breaks a NOT NULL column. Committed row by row, the first
    # would stay behind and a reader would see half a minute.
    good = dict.fromkeys(db.SAMPLE_COLUMNS) | {
        "trade_date": date(2026, 10, 16),
        "sample_time": datetime(2026, 10, 16, 10, 31),
        "underlying": "NIFTY",
        "exchange": "NFO",
        "expiry": "19OCT26",
        "strike": 23000.0,
        "side": "CE",
        "symbol": "NIFTY19OCT2623000CE",
        "oi": 1,
        "source": "live",
    }
    bad = good | {"symbol": "NIFTY19OCT2623000PE", "side": None}
    import duckdb

    with pytest.raises(duckdb.ConstraintException):
        db.insert_samples([good, bad])
    assert db.load_day("NIFTY", "19OCT26", date(2026, 10, 16)) == []


def _minute(hh, mm):
    return datetime(2026, 10, 16, hh, mm)


def test_missed_minutes_stop_short_of_the_live_beat_and_at_the_close():
    held = {_minute(9, 15), _minute(9, 17)}
    nfo = (time(9, 15), time(15, 30))
    assert rec.missing_minutes(held, date(2026, 10, 16), _minute(9, 20), nfo) == [
        _minute(9, 16),
        _minute(9, 18),
    ]
    late = rec.missing_minutes(set(), date(2026, 10, 16), datetime(2026, 10, 16, 15, 45, 30), nfo)
    assert late[0] == _minute(9, 16) and late[-1] == _minute(15, 30) and len(late) == 375
    mcx = rec.missing_minutes(
        set(), date(2026, 10, 16), datetime(2026, 10, 16, 23, 58, 35), (time(9, 0), time(23, 55))
    )
    assert mcx[0] == _minute(9, 1) and mcx[-1] == _minute(23, 55)


def test_the_opening_minute_is_never_asked_of_history(fake_broker):
    """History cannot hold it, and asking cost a call per contract, twice a day."""
    nfo = (time(9, 15), time(15, 30))
    assert rec.missing_minutes(set(), date(2026, 10, 16), _minute(9, 18), nfo) == [_minute(9, 16)]
    # Everything held except the opening minute: nothing to ask the broker.
    held = {_minute(9, m) for m in range(16, 60)}
    assert _minute(9, 15) not in rec.missing_minutes(held, date(2026, 10, 16), _minute(9, 50), nfo)


def test_a_gap_mid_session_is_filled_for_the_contracts_recorded_around_it(fake_broker):
    # The beat caught 09:15 and 09:18, then missed 09:16 and 09:17.
    for m in (15, 18):
        db.insert_samples(
            rec.chain_samples(chain(), "NIFTY", "NSE_INDEX", "19OCT26", _minute(9, m), "live")
        )
    rec.nearest_expiry("NIFTY", "NSE_INDEX", "k", date(2026, 10, 16))
    rec.fill_gaps(datetime(2026, 10, 16, 9, 20, 35, tzinfo=IST))

    held = db.recorded_minutes("NIFTY", "19OCT26", date(2026, 10, 16))
    assert {_minute(9, 16), _minute(9, 17)} <= held
    rows = db.load_day("NIFTY", "19OCT26", date(2026, 10, 16))
    filled = [r for r in rows if r["sample_time"] == _minute(9, 16)]
    assert len(filled) == 6 and {r["source"] for r in filled} == {"backfill"}
    # 09:15 is the beat's own, and stays so.
    assert {r["source"] for r in rows if r["sample_time"] == _minute(9, 15)} == {"live"}
    # The contracts came from the record; nobody asked for today's chain.
    assert fake_broker["chain"] == 0
    assert all(bg for _, bg in fake_broker["history"])


def test_with_nothing_missing_the_broker_is_not_asked(fake_broker):
    for m in range(15, 19):
        db.insert_samples(
            rec.chain_samples(chain(), "NIFTY", "NSE_INDEX", "19OCT26", _minute(9, m), "live")
        )
    rec.nearest_expiry("NIFTY", "NSE_INDEX", "k", date(2026, 10, 16))
    assert rec.fill_gaps(datetime(2026, 10, 16, 9, 20, 35, tzinfo=IST)) == 0
    assert fake_broker["history"] == [] and fake_broker["chain"] == 0


def test_a_minute_history_cannot_supply_is_asked_for_twice_then_left(fake_broker):
    # History in the fake only reaches 09:17, so 09:18 to 09:28 stay missing.
    db.insert_samples(
        rec.chain_samples(chain(), "NIFTY", "NSE_INDEX", "19OCT26", _minute(9, 15), "live")
    )
    when = datetime(2026, 10, 16, 9, 30, 35, tzinfo=IST)
    rec.fill_gaps(when)
    first = len(fake_broker["history"])
    rec.fill_gaps(when)
    second = len(fake_broker["history"])
    rec.fill_gaps(when)
    assert first > 0 and second == 2 * first
    assert len(fake_broker["history"]) == second


def test_a_candle_without_open_interest_is_not_stored_as_zero(monkeypatch, fake_broker):
    import services.history_service as hs

    base = int(datetime(2026, 10, 16, 9, 16, tzinfo=IST).timestamp())

    def get_history(symbol, exchange, interval, start, end, api_key=None, background=False, **_):
        return True, {"data": [{"timestamp": base, "close": 91.0, "oi": None, "volume": 5}]}, 200

    monkeypatch.setattr(hs, "get_history", get_history)
    db.insert_samples(
        rec.chain_samples(chain(), "NIFTY", "NSE_INDEX", "19OCT26", _minute(9, 15), "live")
    )
    rec.fill_gaps(datetime(2026, 10, 16, 9, 20, 35, tzinfo=IST))
    # The 09:16 candle would be stored at 09:17.
    assert _minute(9, 17) not in db.recorded_minutes("NIFTY", "19OCT26", date(2026, 10, 16))


def test_a_contract_whose_quote_did_not_come_back_is_not_stored_as_zero():
    # The option chain answers 0 for both when a quote is missing.
    answer = chain()
    answer["chain"][0]["ce"].update(oi=0, ltp=0)
    answer["chain"][1]["pe"].update(oi=float("nan"))
    rows = rec.chain_samples(answer, "NIFTY", "NSE_INDEX", "19OCT26", _minute(10, 31), "live")
    symbols = {r["symbol"] for r in rows}
    assert "NIFTY19OCT2623050CE" not in symbols and "NIFTY19OCT2623100PE" not in symbols
    assert len(rows) == 4


def test_a_contract_not_traded_yet_with_a_price_is_still_stored():
    # Zero open interest with a quoted premium is a real reading: nobody holds it yet.
    answer = chain()
    answer["chain"][0]["ce"].update(oi=0, ltp=12.5)
    rows = rec.chain_samples(answer, "NIFTY", "NSE_INDEX", "19OCT26", _minute(10, 31), "live")
    row = next(r for r in rows if r["symbol"] == "NIFTY19OCT2623050CE")
    assert (row["oi"], row["ltp"]) == (0, 12.5)


def test_only_one_process_records(tmp_path):
    # A second OpenAlgo process on the same record must not start writing.
    import subprocess
    import sys

    path = str(tmp_path / "oi_flow.duckdb")
    holder = (
        "import os, sys, time; os.environ['OI_FLOW_DATABASE_PATH']=sys.argv[1];"
        "import database.oi_flow_db as d; print(d.claim_writer(), flush=True); time.sleep(30)"
    )
    proc = subprocess.Popen([sys.executable, "-c", holder, path], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "True"
        probe = subprocess.run(
            [sys.executable, "-c", holder.replace("time.sleep(30)", "pass"), path],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert probe.stdout.strip() == "False"
    finally:
        proc.kill()
        proc.wait()
    # Once the holder is gone, the claim is free again.
    probe = subprocess.run(
        [sys.executable, "-c", holder.replace("time.sleep(30)", "pass"), path],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert probe.stdout.strip() == "True"


def test_each_underlying_records_while_its_own_market_is_open(monkeypatch, fake_broker):
    # 17:00: F&O is shut, MCX is trading. Only CRUDEOIL is asked for.
    monkeypatch.setenv("OI_FLOW_UNDERLYINGS", "NIFTY:NSE_INDEX,CRUDEOIL:MCX")
    asked = []
    import services.option_chain_service as ocs

    def chain_for(underlying, exchange, expiry, strike_count, api_key, **_):
        asked.append((underlying, exchange))
        return True, chain(), 200

    monkeypatch.setattr(ocs, "get_option_chain", chain_for)
    monkeypatch.setattr(rec, "nearest_expiry", lambda *a: "19OCT26")
    rec.record_once(datetime(2026, 10, 16, 17, 0, 5, tzinfo=IST))
    assert asked == [("CRUDEOIL", "MCX")]
    rows = db.load_day("CRUDEOIL", "19OCT26", date(2026, 10, 16))
    assert rows and {r["exchange"] for r in rows} == {"MCX"}
    asked.clear()
    # 23:56: MCX has closed too.
    rec.record_once(datetime(2026, 10, 16, 23, 56, 5, tzinfo=IST))
    assert asked == []
    # 15:35: F&O still trading (closing auction to 15:40), MCX too.
    rec.record_once(datetime(2026, 10, 16, 15, 35, 5, tzinfo=IST))
    assert asked == [("NIFTY", "NSE_INDEX"), ("CRUDEOIL", "MCX")]


def test_a_closed_day_records_nothing_and_a_special_session_records(monkeypatch, fake_broker):
    # The calendar decides: a holiday shuts F&O; an MCX evening-only day opens at 17:00.
    windows = {"NFO": None, "MCX": (time(17, 0), time(23, 55))}
    monkeypatch.setattr(rec, "_calendar_window", lambda day, exch: windows[exch])
    monkeypatch.setenv("OI_FLOW_UNDERLYINGS", "NIFTY:NSE_INDEX,CRUDEOIL:MCX")
    assert rec.within_session(datetime(2026, 10, 16, 10, 0, tzinfo=IST), "NSE_INDEX") is False
    assert rec.within_session(datetime(2026, 10, 16, 10, 0, tzinfo=IST), "MCX") is False
    assert rec.within_session(datetime(2026, 10, 16, 17, 30, tzinfo=IST), "MCX") is True


def test_two_threads_asking_for_the_hours_read_the_calendar_once(monkeypatch):
    # The live beat and the gap filler can ask at the same moment (real
    # threads under gthread); the calendar is read once, not once each.
    import threading

    reads = []
    start = threading.Barrier(2)

    def slow_calendar(day, exchange):
        reads.append(exchange)
        import time as _t

        _t.sleep(0.2)
        return time(9, 15), time(15, 40)

    monkeypatch.setattr(rec, "_calendar_window", slow_calendar)
    answers = []

    def ask():
        start.wait()
        answers.append(rec.session_for("NSE_INDEX", date(2026, 10, 16)))

    threads = [threading.Thread(target=ask) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert answers == [(time(9, 15), time(15, 40))] * 2
    assert reads == ["NFO"]


def test_open_charts_are_told_of_a_new_minute_after_it_is_rebuilt(fake_broker, monkeypatch):
    """A chart used to find a new minute only at its own 60-second poll."""
    import extensions
    import services.writer_flow_service as svc

    order = []
    monkeypatch.setattr(svc, "rebuild_watched", lambda u: order.append(("rebuild", sorted(u))))
    monkeypatch.setattr(
        extensions, "emit_from_any_thread", lambda event, data, **_: order.append((event, data))
    )
    rec.record_once(FRIDAY_1031)
    assert order == [
        ("rebuild", ["NIFTY"]),
        (rec.UPDATE_EVENT, {"underlying": "NIFTY"}),
    ]


def test_a_failed_announcement_never_stops_the_minute_beat(fake_broker, monkeypatch):
    import extensions

    def broken(*_a, **_k):
        raise RuntimeError("no socket server")

    monkeypatch.setattr(extensions, "emit_from_any_thread", broken)
    assert rec.record_once(FRIDAY_1031) > 0


def test_the_announcement_reaches_a_connected_socketio_client(monkeypatch):
    """End to end through Flask-SocketIO: what the recorder emits is what a
    browser connected to the app receives, under the name the chart listens for."""
    from flask import Flask
    from flask_socketio import SocketIO

    import extensions

    app = Flask(__name__)
    app.config["SECRET_KEY"] = "writer-flow-announce-test"
    server = SocketIO(app, async_mode="threading")
    monkeypatch.setattr(extensions, "socketio", server)
    client = server.test_client(app)
    assert client.is_connected()

    rec._announce({"CRUDEOIL", "NIFTY"})

    received = [(m["name"], m["args"][0]) for m in client.get_received()]
    assert received == [
        ("writer_flow_update", {"underlying": "CRUDEOIL"}),
        ("writer_flow_update", {"underlying": "NIFTY"}),
    ]
    client.disconnect()


def test_a_minute_holding_only_some_strikes_is_filled_again(fake_broker):
    # 19-Oct-2026: a fill failed partway when another process held the record,
    # leaving 09:19-09:24 and 09:29-09:38 with 12 of 31 NIFTY strikes. Because
    # any row made a minute count as held, the other 19 were never asked for,
    # and most strips on the chart stayed blank through those minutes.
    for m in (15, 18):
        db.insert_samples(
            rec.chain_samples(chain(), "NIFTY", "NSE_INDEX", "19OCT26", _minute(9, m), "live")
        )
    partial = [
        r
        for r in rec.chain_samples(
            chain(), "NIFTY", "NSE_INDEX", "19OCT26", _minute(9, 16), "backfill"
        )
        if r["strike"] == 23050
    ]
    db.insert_samples(partial)  # 2 of the 6 contracts
    assert _minute(9, 16) not in db.recorded_minutes("NIFTY", "19OCT26", date(2026, 10, 16))
    rec.nearest_expiry("NIFTY", "NSE_INDEX", "k", date(2026, 10, 16))
    rec.fill_gaps(datetime(2026, 10, 16, 9, 20, 35, tzinfo=IST))
    rows = db.load_day("NIFTY", "19OCT26", date(2026, 10, 16))
    assert len([r for r in rows if r["sample_time"] == _minute(9, 16)]) == 6


def test_a_chain_that_arrives_after_its_minute_is_not_stored_under_it(fake_broker, monkeypatch):
    # 19-Oct-2026: the 09:59 and 10:00 beats waited minutes for Upstox while its
    # allowance was used up. Each was stamped at its start and filled with the
    # open interest of minutes later: NIFTY 23100 CE read 19.55M at both, then
    # 18.53M from history at 10:01, a jump that never happened.
    clock = [1000.0]
    monkeypatch.setattr(rec.time, "monotonic", lambda: clock[0])
    import services.option_chain_service as ocs

    real = ocs.get_option_chain

    def slow(*a, **k):
        clock[0] += 120.0  # the chain comes back two minutes later
        return real(*a, **k)

    monkeypatch.setattr(ocs, "get_option_chain", slow)
    assert rec.record_once(FRIDAY_1031) == 0
    assert db.load_day("NIFTY", "19OCT26", date(2026, 10, 16)) == []
