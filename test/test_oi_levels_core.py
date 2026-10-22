"""Option levels on a real morning (NIFTY, 21-Oct-2026, 09:15 to 10:23) and on
made-up days small enough to reason about by hand.

That morning NIFTY opened above 23,000, rose to 23,118 by 09:36 and fell
through 23,000 at 10:04, where the market read saw put writers leave and call
writers arrive: 23,000 turned from support to resistance.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import services.oi_levels.core as core
from services.market_read.core import ReadInputs, build_read
from services.oi_levels.core import LevelInputs, build_levels, hold_rates

FIXTURE = Path(__file__).parent / "data" / "market_read_21oct2026.json.gz"
DAY = "2026-10-21"


def _t(hm: str) -> datetime:
    return datetime.fromisoformat(f"{DAY}T{hm}:00")


def _greeks(*args):
    from services.option_greeks_service import calculate_chain_greeks

    return calculate_chain_greeks(*args)


def _flip(*args):
    from services.strike_profile_service import find_gamma_flip

    return find_gamma_flip(*args)


@pytest.fixture(scope="module")
def doc():
    return json.loads(gzip.open(FIXTURE, "rt").read())


@pytest.fixture(scope="module")
def samples(doc):
    return [
        {
            "sample_time": _t(t),
            "strike": k,
            "side": s,
            "oi": oi,
            "ltp": ltp,
            "volume": v,
            "spot": sp,
        }
        for t, k, s, oi, ltp, v, sp in doc["samples"]["NIFTY"]
    ]


@pytest.fixture(scope="module")
def morning(doc, samples):
    return build_levels(
        LevelInputs(
            "NIFTY", samples, datetime.fromisoformat(doc["expiry_at"]), greeks=_greeks, flip=_flip
        )
    )


def _zone(read, role):
    return next(z for z in read["zones"] if z["role"] == role)


def test_zones_sit_on_both_sides_and_agree_with_the_market_read(doc, samples, morning):
    read = build_read(ReadInputs("NIFTY", samples, datetime.fromisoformat(doc["expiry_at"])))
    support, resistance = _zone(morning, "support"), _zone(morning, "resistance")
    assert support["high"] <= morning["spot"] <= resistance["low"]
    assert support["anchor"] == read["state"]["levels"]["support"]["strike"] == 22500
    assert resistance["anchor"] == read["state"]["levels"]["resistance"]["strike"] == 23000


def test_walls_shade_puts_as_support_and_calls_as_resistance(morning):
    """Each near-the-money strike carries its put and call open interest, so the
    heatmap can colour it: put-dominant strikes are support (green), call-dominant
    resistance (red). The biggest wall on each side is where the zone anchor sits."""
    walls = {w["strike"]: w for w in morning["walls"]}
    assert walls, "the last minute should carry its strikes"
    spot = morning["spot"]
    # Well out of the money, puts hold the downside and calls the upside: that is
    # the green-below, red-above the heatmap paints.
    deep = 3 * morning["step"]
    assert all(w["put"] >= w["call"] for k, w in walls.items() if k <= spot - deep)
    assert all(w["call"] >= w["put"] for k, w in walls.items() if k >= spot + deep)
    # Both sides are present, so the chart is never all one colour, and every
    # strike carries the change the "writers adding" shade reads from.
    assert any(w["put"] >= w["call"] for w in walls.values())
    assert any(w["call"] > w["put"] for w in walls.values())
    assert all({"put_change", "call_change"} <= w.keys() for w in walls.values())


def test_the_levels_move_with_the_data(morning):
    supports = {row["support"][0] for row in morning["history"] if row["support"]}
    resistances = {row["resistance"][0] for row in morning["history"] if row["resistance"]}
    assert len(supports) > 1 and len(resistances) > 1
    assert {c["kind"] for c in morning["changes"]} >= {"moved", "broken", "turned"}


def test_23000_turned_from_support_to_resistance_after_the_fall(morning):
    turned = [c for c in morning["changes"] if c["kind"] == "turned"]
    assert [(c["role"], c["from"]) for c in turned] == [("support", 23000)]
    assert _t("10:04") <= turned[0]["time"] <= _t("10:12")


def test_the_fall_through_23000_is_logged_as_a_broken_touch(morning):
    """At 10:04 the price came down onto the 23,000 support, still holding, and went through."""
    zone = [x for x in morning["touches"] if x["kind"] == "zone"]
    assert [(x["time"], x["role"], x["strike"], x["status"], x["outcome"]) for x in zone] == [
        (_t("10:04"), "support", 23000, "holding", "broke")
    ]


def test_a_minute_never_depends_on_later_minutes(samples, morning):
    cut = _t("09:50")
    early = build_levels(LevelInputs("NIFTY", [s for s in samples if s["sample_time"] <= cut]))
    full = {row["time"]: row for row in morning["history"]}
    for row in early["history"]:
        assert row == full[row["time"]]
    assert early["changes"] == [c for c in morning["changes"] if c["time"] <= cut]


def test_the_lead_rule_stops_neighbours_trading_places():
    """Two call strikes a few percent apart swap the lead every minute, then one
    stays ahead by under 10% for ten minutes: the raw biggest strike changes 10
    times and the zone does not move. When one pulls clearly ahead and stays
    there, the zone moves once."""
    rows = _day([1000.0] * 30)
    for r in rows:
        minute = int((r["sample_time"] - rows[0]["sample_time"]).total_seconds() // 60)
        if r["side"] == "CE" and r["strike"] in (1050, 1060):
            lead = r["strike"] == (1050 if minute % 2 == 0 else 1060)
            r["oi"] = 900_000 if lead else 870_000
            if 10 <= minute < 20 and r["strike"] == 1060:
                r["oi"] = 940_000  # ahead for ten minutes, but by under 10%
            if minute >= 20 and r["strike"] == 1060:
                r["oi"] = 1_200_000
    book = core._book(rows)
    raw, prev = 0, None
    for t in book.times:
        own = core._own(book, t, "CE")
        top = max(own, key=own.get)
        raw += prev is not None and top != prev
        prev = top
    read = build_levels(LevelInputs("X", rows))
    moved = [c for c in read["changes"] if c["role"] == "resistance"]
    assert raw >= 9
    assert [(c["from"], c["to"], c["kind"]) for c in moved] == [(1050, 1060, "moved")]


def test_gamma_is_read_from_the_recorded_prices(morning):
    g = morning["gamma"]
    assert 22500 <= g["top_strike"] <= 23500
    assert g["flip"] is not None and 22400 <= g["flip"] <= 23600
    assert abs(g["forward"] - morning["spot"]) < 0.005 * morning["spot"]
    assert len(g["series"]) >= 10


# ------------------------------------------------------------- made-up days


def _day(prices: list[float], base: float = 1000.0, step: float = 10.0) -> list[dict]:
    """One minute per price. Puts are biggest at base - 50, calls at base + 50."""
    rows = []
    start = datetime(2026, 10, 23, 10, 0)
    for i, p in enumerate(prices):
        t = start + timedelta(minutes=i)
        for j in range(-8, 9):
            k = base + j * step
            pe = 900_000 if k == base - 50 else 100_000
            ce = 900_000 if k == base + 50 else 100_000
            rows.append(
                {
                    "sample_time": t,
                    "strike": k,
                    "side": "PE",
                    "oi": pe,
                    "ltp": 5.0,
                    "volume": 0,
                    "spot": p,
                }
            )
            rows.append(
                {
                    "sample_time": t,
                    "strike": k,
                    "side": "CE",
                    "oi": ce,
                    "ltp": 5.0,
                    "volume": 0,
                    "spot": p,
                }
            )
    return rows


def _zone_touches(read):
    return [x for x in read["touches"] if x["kind"] == "zone"]


def test_a_touch_that_bounces_counts_as_held():
    # Down onto the 950 support (within 0.04% = 0.4), then up 0.12% (1.2) and more.
    read = build_levels(LevelInputs("X", _day([1000, 980, 960, 950.2] + [952.5] * 25)))
    assert [(x["role"], x["strike"], x["outcome"]) for x in _zone_touches(read)] == [
        ("support", 950, "held")
    ]


def test_a_touch_that_goes_through_counts_as_broken():
    read = build_levels(LevelInputs("X", _day([1000, 980, 960, 950.2, 948.5] + [948.0] * 25)))
    assert [x["outcome"] for x in _zone_touches(read)] == ["broke"]


def test_a_touch_too_recent_to_judge_is_pending():
    read = build_levels(LevelInputs("X", _day([1000, 980, 960, 950.2, 950.4, 950.1])))
    assert [x["outcome"] for x in _zone_touches(read)] == ["pending"]


def test_hold_rates_count_only_judged_touches():
    rates = hold_rates(
        [
            {"kind": "zone", "status": "holding", "outcome": "held"},
            {"kind": "zone", "status": "holding", "outcome": "broke"},
            {"kind": "zone", "status": "holding", "outcome": "neither"},
            {"kind": "zone", "status": "weakening", "outcome": "pending"},
            {"kind": "round", "status": "round", "outcome": "held"},
        ]
    )
    assert rates["zone_holding"] == {"n": 2, "held": 1, "rate": 0.5, "enough": False}
    assert rates["round"]["n"] == 1 and "zone_weakening" not in rates


def test_thresholds_are_shares_of_the_price():
    """At 6,000 (an MCX contract) a touch is within 2.4, not NIFTY's 10 points."""
    read = build_levels(LevelInputs("X", _day([6000, 5960, 5952.0] + [5960] * 25, base=6000)))
    assert [x["outcome"] for x in _zone_touches(read)] == ["held"]
    assert read["thresholds"]["chance"] == pytest.approx(0.4)


def test_a_minute_recorded_only_in_part_keeps_the_zones():
    """The last minute arrives with only two far strikes: the zones stay put."""
    rows = _day([1000.0] * 6)
    last = rows[-1]["sample_time"]
    rows = [r for r in rows if r["sample_time"] != last or r["strike"] in (1080.0,)]
    read = build_levels(LevelInputs("X", rows))
    assert {z["role"]: z["anchor"] for z in read["zones"]} == {"support": 950, "resistance": 1050}
