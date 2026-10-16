"""Writer Flow analysis, on small hand-made days.

Each case is a scenario a trader would recognise: writers covering a call
strike through a rally, writers adding puts underneath, a strike that has not
moved for ten minutes. The analysis is pure, so these need nothing running.
"""

from datetime import datetime, timedelta

import pytest

from services.oi_flow_analysis import (
    ADDING,
    BUYING,
    COVERING,
    STEADY,
    UNWINDING,
    WRITING,
    FlowSettings,
    analyse,
    choose_step_seconds,
    classify,
)

T0 = datetime(2026, 10, 16, 9, 15)
LOT = 65


def series(strike, side, points, step=60, spot=23100.0, start=T0):
    """Samples for one strike side: points are (oi, ltp), one per step."""
    sym = f"NIFTY19OCT26{int(strike)}{side}"
    return [
        {
            "sample_time": start + timedelta(seconds=i * step),
            "strike": strike,
            "side": side,
            "symbol": sym,
            "oi": oi,
            "ltp": ltp,
            "spot": spot,
            "lot_size": LOT,
        }
        for i, (oi, ltp) in enumerate(points)
    ]


ONE_MIN = FlowSettings(step_seconds=60, noise_pct=1.0, noise_lots=1)


@pytest.mark.parametrize(
    "d_oi, d_ltp, expected",
    [
        (+50_000, -2.0, WRITING),
        (-50_000, +2.0, COVERING),
        (+50_000, +2.0, BUYING),
        (-50_000, -2.0, UNWINDING),
        (+50_000, 0.0, ADDING),
        (+100, -2.0, STEADY),  # below the noise floor
    ],
)
def test_one_step_is_read_from_oi_and_premium_together(d_oi, d_ltp, expected):
    assert classify(d_oi, d_ltp, 1_000_000, LOT, ONE_MIN) == expected


def test_call_writers_covering_through_a_rally_is_one_streak_with_events():
    # 23200 CE: OI falls every minute for 16 minutes while the premium rises.
    pts = [(2_000_000 - i * 40_000, 50 + i * 3) for i in range(17)]
    out = analyse(series(23200.0, "CE", pts), ONE_MIN)
    ce = out["strikes"][0]["ce"]
    assert ce["cls"] == COVERING
    assert ce["streak_minutes"] == 16
    assert ce["streak_oi_change"] == -16 * 40_000
    kinds = [(e["kind"], e["minutes"]) for e in out["events"]]
    assert kinds == [(COVERING, 10), (COVERING, 15)]


def test_quiet_minutes_inside_a_run_do_not_break_it_but_five_do():
    adding = [(1_000_000 + i * 30_000, 100 - i) for i in range(6)]
    four_quiet = adding + [adding[-1]] * 4 + [(1_180_000, 93), (1_210_000, 92)]
    out = analyse(series(23000.0, "PE", four_quiet), ONE_MIN)
    pe = out["strikes"][0]["pe"]
    assert pe["cls"] == WRITING
    assert pe["streak_minutes"] == 11

    five_quiet = adding + [adding[-1]] * 5 + [(1_180_000, 93)]
    out = analyse(series(23000.0, "PE", five_quiet), ONE_MIN)
    pe = out["strikes"][0]["pe"]
    assert pe["cls"] == WRITING
    assert pe["streak_minutes"] == 1, "a new run starts after five quiet minutes"


def test_a_move_against_the_run_ends_it_at_once():
    falling = [(2_000_000 - i * 40_000, 50 + i) for i in range(8)]
    back = falling + [(1_800_000, 59)]  # buyers return: OI up 80k, premium up
    out = analyse(series(23200.0, "CE", back), ONE_MIN)
    ce = out["strikes"][0]["ce"]
    assert ce["cls"] == BUYING
    assert ce["streak_minutes"] == 1


def test_a_strike_with_no_change_reports_how_long_it_has_been_steady():
    moving = [(1_000_000 + i * 30_000, 100 - i) for i in range(4)]
    still = [moving[-1]] * 12
    out = analyse(series(23100.0, "CE", moving + still), ONE_MIN)
    ce = out["strikes"][0]["ce"]
    assert ce["cls"] == STEADY
    assert ce["steady_minutes"] == 12


def test_window_changes_and_totals_add_up():
    calls = series(23100.0, "CE", [(1_000_000 + i * 10_000, 100 - i) for i in range(21)])
    puts = series(23100.0, "PE", [(800_000 - i * 5_000, 80 + i) for i in range(21)])
    out = analyse(calls + puts, ONE_MIN)
    row = out["strikes"][0]
    assert row["ce"]["window_change"][10] == 100_000
    assert row["pe"]["window_change"][20] == -100_000
    assert all(t["ce_change"] == 10_000 and t["pe_change"] == -5_000 for t in out["totals"][1:])
    assert row["atm"] is True


def test_the_step_follows_how_often_oi_actually_changes():
    every_minute = series(23100.0, "CE", [(1_000_000 + i * 1000, 100) for i in range(10)])
    assert choose_step_seconds(every_minute) == 60
    # OI moves every third minute, as SENSEX's did: a three-minute step.
    jumps = [(1_000_000 + (i // 3) * 1000, 100) for i in range(12)]
    assert choose_step_seconds(series(74700.0, "CE", jumps)) == 180


def test_three_minute_steps_take_the_last_sample_in_each_step():
    pts = [(1_000_000 + i * 20_000, 100 - i) for i in range(9)]  # one per minute
    out = analyse(
        series(23100.0, "PE", pts), FlowSettings(step_seconds=180, noise_pct=1.0, noise_lots=1)
    )
    assert len(out["steps"]) == 3
    assert [c["oi_change"] for c in out["cells"]] == [60_000, 60_000]


def test_covering_through_a_noisy_premium_is_still_one_streak():
    # Seen live on 16-Oct-2026: 23000 CE lost 95L of OI in an hour of rally,
    # but its premium ticked up and down minute to minute, so a class read
    # per step alternated covering/unwinding and no streak ever reached ten
    # minutes. The streak follows OI; its class follows the premium across it.
    pts, ltp = [], 50.0
    for i in range(21):
        ltp += 3.0 if i % 2 == 0 else -1.0  # net up, never two steps alike
        pts.append((3_000_000 - i * 40_000, ltp))
    out = analyse(series(23000.0, "CE", pts), ONE_MIN)
    ce = out["strikes"][0]["ce"]
    assert ce["cls"] == COVERING
    assert ce["streak_minutes"] == 20
    assert [(e["kind"], e["minutes"]) for e in out["events"]] == [
        (COVERING, 10),
        (COVERING, 15),
        (COVERING, 20),
    ]
    # The heatmap still shows each minute as it was.
    assert {c["cls"] for c in out["cells"]} == {COVERING, UNWINDING}


def test_nothing_recorded_reads_as_nothing():
    assert analyse([], ONE_MIN)["strikes"] == []


def test_a_strike_that_left_the_window_is_not_read_as_steady():
    # 23500 CE was sampled for the first 5 minutes only, then ATM moved away
    # and the recorder stopped asking for it. Its last state is unknown, not
    # quiet: the page must be able to tell the two apart.
    left = series(23500.0, "CE", [(1_000_000, 10.0)] * 5)
    held = series(23100.0, "CE", [(2_000_000, 50.0)] * 20)
    out = analyse(left + held, ONE_MIN)
    by_strike = {r["strike"]: r for r in out["strikes"]}
    assert by_strike[23500.0]["ce"]["observed"] is False
    assert by_strike[23500.0]["ce"]["last_seen"] == T0 + timedelta(minutes=4)
    assert by_strike[23500.0]["observed"] is False
    assert by_strike[23100.0]["ce"]["observed"] is True
    assert by_strike[23100.0]["observed"] is True


def _cells(out, strike, side="CE"):
    return {c["time"]: c for c in out["cells"] if c["strike"] == strike and c["side"] == side}


def test_a_dropped_quote_is_a_gap_not_every_writer_leaving():
    # 23100 CE held 20L; one minute the quote did not come back and read 0.
    points = [(2_000_000, 50.0)] * 5 + [(0, 0.0)] + [(2_050_000, 49.0)] * 3
    out = analyse(series(23100.0, "CE", points), ONE_MIN)
    cells = _cells(out, 23100.0)
    assert T0 + timedelta(minutes=5) not in cells
    # The minute after is compared with the last real reading: +50K, not +20.5L.
    assert cells[T0 + timedelta(minutes=6)]["oi_change"] == 50_000
    assert max(abs(c["oi_change"]) for c in cells.values()) == 50_000
    totals = {t["time"]: t["ce_change"] for t in out["totals"]}
    # Nothing usable at 09:20, so it is no step at all, and 09:21 carries the move.
    assert T0 + timedelta(minutes=5) not in totals
    assert totals[T0 + timedelta(minutes=6)] == 50_000


def test_a_dropout_last_in_a_step_does_not_hide_the_good_reading_before_it():
    three = FlowSettings(step_seconds=180, noise_pct=1.0, noise_lots=1)
    # Steps of three minutes; the last sample of the second step is a dropout.
    points = [(1_000_000, 50.0)] * 3 + [(1_100_000, 48.0), (1_100_000, 48.0), (0, 0.0)]
    out = analyse(series(23100.0, "CE", points), three)
    second = _cells(out, 23100.0)[T0 + timedelta(minutes=3)]
    assert (second["oi_change"], second["cls"]) == (100_000, WRITING)


def test_a_premium_of_zero_is_no_price_so_the_side_stays_unclear():
    # Not traded yet (premium 0), then traded at 50 as OI arrives.
    points = [(0, 0.0), (500_000, 50.0), (500_000, 50.0)]
    out = analyse(series(23500.0, "CE", points), ONE_MIN)
    first_move = _cells(out, 23500.0)[T0 + timedelta(minutes=1)]
    assert first_move["oi_change"] == 500_000
    assert first_move["cls"] == ADDING  # not "buying" on a premium leap from nothing


def test_a_strike_back_after_hours_away_starts_over_rather_than_jumping():
    away = series(23500.0, "CE", [(1_000_000, 40.0)] * 5)
    back = series(
        23500.0, "CE", [(3_000_000, 20.0), (3_040_000, 19.0)], start=T0 + timedelta(minutes=180)
    )
    held = series(23100.0, "CE", [(2_000_000, 50.0)] * 182)
    out = analyse(away + back + held, ONE_MIN)
    cells = _cells(out, 23500.0)
    assert T0 + timedelta(minutes=180) not in cells  # a new baseline, not +20L in one minute
    assert cells[T0 + timedelta(minutes=181)]["oi_change"] == 40_000
    side = next(r for r in out["strikes"] if r["strike"] == 23500.0)["ce"]
    assert side["streak_oi_change"] == 40_000


def test_open_interest_that_starts_from_none_is_a_real_first_move():
    # Zero before anybody has traded it is a reading; the first build-up counts.
    points = [(0, 10.0), (0, 10.0), (300_000, 9.0)]
    out = analyse(series(23600.0, "PE", points), ONE_MIN)
    move = _cells(out, 23600.0, "PE")[T0 + timedelta(minutes=2)]
    assert (move["oi_change"], move["cls"]) == (300_000, WRITING)


def test_open_interest_quoted_in_lots_is_read_in_lots():
    # MCX quotes open interest in lots (a lot of 100 here): a move of 40 lots
    # is real, and must not be held to a floor of 5 x 100.
    rows = series(9000.0, "PE", [(6_094, 458.0), (6_134, 455.0), (6_174, 452.0)])
    for r in rows:
        r["lot_size"] = 100
    out = analyse(rows, FlowSettings(step_seconds=60))
    cells = _cells(out, 9000.0, "PE")
    assert cells[T0 + timedelta(minutes=1)]["cls"] == WRITING


def test_open_interest_quoted_in_contracts_keeps_its_lot_floor():
    # NFO quotes contracts (multiples of the lot): 2 lots moved on a big strike is noise.
    rows = series(23100.0, "CE", [(1_300_000, 50.0), (1_300_130, 49.0)])
    out = analyse(rows, FlowSettings(step_seconds=60))
    assert _cells(out, 23100.0)[T0 + timedelta(minutes=1)]["cls"] == STEADY


def test_the_latest_known_price_carries_to_the_close():
    # The index stops at 15:30; its options trade on with no price of their own.
    rows = series(23100.0, "CE", [(1_000_000, 50.0)] * 3)
    rows[-1]["spot"] = None
    assert analyse(rows, ONE_MIN)["spot"] == 23100.0


def test_the_unit_open_interest_is_quoted_in_is_reported():
    lots = series(9000.0, "PE", [(6_094, 458.0), (6_134, 455.0)])
    for r in lots:
        r["lot_size"] = 100
    assert analyse(lots, FlowSettings(step_seconds=60))["oi_unit"] == "lots"
    contracts = series(23100.0, "CE", [(1_300_000, 50.0), (1_365_000, 49.0)])
    assert analyse(contracts, FlowSettings(step_seconds=60))["oi_unit"] == "contracts"


def test_pausing_steps_aside_without_changing_the_reading():
    """The Writer Flow builder pauses during a day's analysis so a waiting
    request runs at once; the pause must have no say in what is read."""
    import services.oi_flow_analysis as oa

    samples = []
    for k in range(20):
        samples += series(
            23000 + 50 * k,
            "CE",
            [(1_000_000 + 9_000 * i * (k % 3 - 1), 100 - i) for i in range(60)],
        )
        samples += series(23000 + 50 * k, "PE", [(800_000 + 7_000 * i, 50 + i) for i in range(60)])
    calls = []
    paused = analyse(samples, ONE_MIN, pause=lambda: calls.append(1))
    assert analyse(samples, ONE_MIN) == paused
    assert len(calls) >= len(samples) // oa.PAUSE_EVERY + 40  # rows, then each strike side


def test_the_writer_flow_builder_pauses(tmp_path, monkeypatch):
    import database.oi_flow_db as db
    import services.writer_flow_service as svc

    monkeypatch.setenv("OI_FLOW_DATABASE_PATH", str(tmp_path / "oi_flow.duckdb"))
    db.init_oi_flow_database()
    rows = [
        {
            **s,
            "trade_date": T0.date(),
            "underlying": "NIFTY",
            "exchange": "NFO",
            "expiry": "19OCT26",
            "volume": 1,
            "source": "live",
        }
        for s in series(23000, "CE", [(1_000_000 + 1000 * i, 100 - i) for i in range(5)])
    ]
    db.insert_samples(rows)
    calls = []
    monkeypatch.setattr(svc, "_pause", lambda: calls.append(1))
    assert svc.flow_payload(svc.FlowView(underlying="NIFTY", step="60"))["status"] == "success"
    assert calls, "the builder ran a whole day without stepping aside"
