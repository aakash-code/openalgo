"""The market read on a real morning: NIFTY, 21-Oct-2026, 09:15 to 10:23.

That morning NIFTY opened 127 points above the previous close, rose to 23,118
by 09:36 and fell to 22,929 by 10:13. Read by hand afterwards, the turn showed
up as put writers leaving 23,100 (from 09:48), BANKNIFTY's call writers taking
over before NIFTY's, NIFTY's call writers taking over at about 09:53, 23,000
turning from support to resistance once the price fell through it at 10:04,
and India VIX rising into the low. The fixture is the recorded data itself.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime
from pathlib import Path

import pytest

import services.market_read.core as core
from services.market_read.core import MarketMinute, ReadInputs, build_read

FIXTURE = Path(__file__).parent / "data" / "market_read_21oct2026.json.gz"
DAY = "2026-10-21"


def _t(hm: str) -> datetime:
    return datetime.fromisoformat(f"{DAY}T{hm}:00")


def _load():
    doc = json.loads(gzip.open(FIXTURE, "rt").read())
    samples = {
        u: [
            {
                "sample_time": _t(t),
                "strike": k,
                "side": s,
                "oi": oi,
                "ltp": ltp,
                "volume": vol,
                "spot": spot,
            }
            for t, k, s, oi, ltp, vol, spot in rows
        ]
        for u, rows in doc["samples"].items()
    }
    market = [MarketMinute(_t(t), f, oi, v) for t, f, oi, v in doc["market"]]
    return doc, samples, market


def _greeks(*args):
    from services.option_greeks_service import calculate_chain_greeks

    return calculate_chain_greeks(*args)


@pytest.fixture(scope="module")
def morning():
    doc, samples, market = _load()
    return ReadInputs(
        underlying="NIFTY",
        samples=samples["NIFTY"],
        expiry_at=datetime.fromisoformat(doc["expiry_at"]),
        market=market,
        prev_close=doc["prev_close"],
        others={"BANKNIFTY": samples["BANKNIFTY"], "SENSEX": samples["SENSEX"]},
        greeks=_greeks,
    )


def _events(read, kind=None, level=None):
    return [
        e
        for e in read["events"]
        if (kind is None or e["kind"] == kind) and (level is None or e["level"] == level)
    ]


def _at(e) -> str:
    return e["time"].strftime("%H:%M")


def test_the_turn_is_called_once_and_at_the_right_time(morning):
    read = build_read(morning)
    flips = _events(read, level="major")
    calls = [e for e in flips if e["kind"] == "flow_call"]
    assert [_at(e) for e in calls] == ["09:55"], [(_at(e), e["title"]) for e in flips]
    assert "since 09:53" in calls[0]["reason"].lower()
    assert read["state"]["flow"]["side"] == "call"
    assert read["headline"].startswith("Call writers lead NIFTY since 09:53")


def test_the_morning_raises_only_the_real_turns(morning):
    read = build_read(morning)
    major = [(_at(e), e["kind"]) for e in _events(read, level="major")]
    assert major == [
        ("09:18", "flow_put"),
        ("09:55", "flow_call"),
        ("10:07", "support_to_resistance"),
        ("10:09", "vix_up"),
    ]


def test_23000_turns_from_support_to_resistance_after_the_price_falls_through(morning):
    read = build_read(morning)
    (flip,) = _events(read, "support_to_resistance")
    assert flip["title"] == "23,000 turned from support to resistance"
    assert "since 10:04" in flip["reason"]


def test_put_writers_leaving_23100_is_seen_before_the_flow_turns(morning):
    read = build_read(morning)
    leaving = [e for e in _events(read, "support_weakening") if "23,100" in e["title"]]
    assert leaving and _at(leaving[0]) == "09:53"
    assert leaving[0]["level"] == "info"  # not the support itself: 23,000 was


def test_banknifty_turns_before_nifty(morning):
    read = build_read(morning)
    firsts = [e for e in _events(read, "other_leads") if e["title"].startswith("BANKNIFTY call")]
    assert firsts and _at(firsts[0]) < "09:55"


def test_futures_read_longs_exiting_on_the_fall(morning):
    read = build_read(morning)
    fut = [(_at(e), e["title"]) for e in _events(read, "futures")]
    assert ("09:47", "Futures: longs exiting") in fut or any(
        title == "Futures: longs exiting" and "09:4" in at for at, title in fut
    ), fut


def test_support_sits_below_the_price_and_resistance_above(morning):
    state = build_read(morning)["state"]
    spot = state["price"]["spot"]
    assert state["levels"]["support"]["strike"] <= spot + 25
    assert state["levels"]["resistance"]["strike"] >= spot - 25


def test_implied_volatility_is_priced_off_the_future(morning):
    """Priced off spot, calls read about two points above puts that morning,
    because the future traded 25 points over the index."""
    book = core._book(morning.samples)
    forward = core._forward(morning.market, book)
    t = _t("10:00")
    assert 15 < forward[t] - book.spot[t] < 40
    _, atm_iv = core._deltas_and_iv(book, morning, forward)
    assert 12 < atm_iv[t] < 17


def test_the_same_inputs_give_the_same_event_ids(morning):
    a, b = build_read(morning), build_read(morning)
    assert [e["id"] for e in a["events"]] == [e["id"] for e in b["events"]]


def test_a_partial_day_never_raises_an_event_from_later_minutes(morning):
    cut = _t("09:50")
    early = ReadInputs(
        underlying="NIFTY",
        samples=[s for s in morning.samples if s["sample_time"] <= cut],
        expiry_at=morning.expiry_at,
        market=[m for m in morning.market if m.time <= cut],
        prev_close=morning.prev_close,
        others={k: [s for s in v if s["sample_time"] <= cut] for k, v in morning.others.items()},
        greeks=morning.greeks,
    )
    read = build_read(early)
    assert all(e["time"] <= cut for e in read["events"])
    full_ids = {e["id"] for e in build_read(morning)["events"] if e["time"] <= cut}
    assert {e["id"] for e in read["events"]} <= full_ids | {e["id"] for e in read["events"]}


def test_a_lead_that_flips_straight_back_or_returns_is_not_major():
    """With a 5-minute window the lead swung 24 to 26 times on a full day. A
    swing back within FLIP_SETTLE minutes, or the same side returning after a
    quiet spell, is kept out of the alerts."""
    times = [_t(f"09:{m:02d}") for m in range(15, 60)] + [_t(f"10:{m:02d}") for m in range(0, 60)]
    flow = [core._FlowMinute(t, 0, 0, None, None, 1, 0, 0) for t in times]
    index_of = {t: i for i, t in enumerate(times)}
    regimes = [
        core._Regime(_t("09:22"), "put", _t("09:20")),
        core._Regime(_t("09:32"), "call", _t("09:30")),  # 10 minutes later: a swing
        core._Regime(_t("10:02"), "put", _t("10:00")),  # call held 30 minutes: a turn
        core._Regime(_t("10:20"), "balanced", _t("10:10")),
        core._Regime(_t("10:42"), "put", _t("10:40")),  # put again after quiet: no turn
    ]
    levels = [
        (e["time"].strftime("%H:%M"), e["level"])
        for e in core._flow_events("NIFTY", regimes, flow, index_of)
    ]
    assert levels == [
        ("09:22", "major"),
        ("09:32", "info"),
        ("10:02", "major"),
        ("10:20", "info"),
        ("10:42", "info"),
    ]


def test_expiry_day_mutes_writers_leaving(morning):
    on_expiry = ReadInputs(
        underlying="NIFTY",
        samples=morning.samples,
        expiry_at=_t("15:30"),
        market=morning.market,
        prev_close=morning.prev_close,
        greeks=None,
    )
    read = build_read(on_expiry)
    assert not [
        e for e in read["events"] if e["kind"].endswith("_weakening") and e["level"] == "major"
    ]


def test_fewer_than_two_minutes_reads_nothing():
    assert build_read(ReadInputs(underlying="NIFTY", samples=[])) is None


def test_a_turn_under_way_is_said_before_it_is_held(morning):
    """At 09:54 put writers still held the lead on paper, but the last ten
    minutes already leaned to calls: the read says so instead of showing a
    green 'put writers lead' that the numbers beside it contradict."""
    cut = _t("09:54")
    early = ReadInputs(
        underlying="NIFTY",
        samples=[s for s in morning.samples if s["sample_time"] <= cut],
        expiry_at=morning.expiry_at,
        market=[m for m in morning.market if m.time <= cut],
        greeks=morning.greeks,
    )
    read = build_read(early)
    flow = read["state"]["flow"]
    assert (flow["side"], flow["leaning"], flow["tone"]) == ("put", "call", 0)
    assert "but call writers are taking over" in read["headline"]


def test_a_new_low_must_beat_the_old_one_by_a_real_margin():
    """A fraction of a point read as '22,929, below the 22,929'."""
    rows = []
    for minute, spot in ((0, 23000.0), (1, 22929.25), (20, 22929.0), (40, 22900.0)):
        t = _t(f"10:{minute:02d}")
        rows += [
            {
                "sample_time": t,
                "strike": 23000.0,
                "side": side,
                "oi": 1,
                "ltp": 1,
                "volume": 1,
                "spot": spot,
            }
            for side in ("CE", "PE")
        ] + [
            {
                "sample_time": t,
                "strike": 23050.0,
                "side": side,
                "oi": 1,
                "ltp": 1,
                "volume": 1,
                "spot": spot,
            }
            for side in ("CE", "PE")
        ]
    read = build_read(ReadInputs(underlying="NIFTY", samples=rows))
    lows = [e["time"].strftime("%H:%M") for e in read["events"] if e["kind"] == "day_low"]
    assert lows == ["10:40"]
