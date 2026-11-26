"""Three fixes found reviewing levels and Writer Flow on recorded days (25-Nov-2026)."""

from datetime import datetime, timedelta

from database.oi_flow_db import _expiry_order
from services.oi_flow_analysis import analyse
from services.oi_levels.core import LevelInputs, build_levels

START = datetime(2026, 11, 20, 9, 15)


def _row(minute, strike, side, oi, spot, ltp=10.0):
    return {
        "sample_time": START + timedelta(minutes=minute),
        "strike": strike,
        "side": side,
        "symbol": f"NIFTY01DEC26{int(strike)}{side}",
        "oi": oi,
        "ltp": ltp,
        "spot": spot,
        "lot_size": 75,
        "expiry": "01DEC26",
        "exchange": "NFO",
    }


def test_a_level_just_beyond_the_price_is_not_broken_into_itself():
    # NIFTY: a break needs 20 points, the call side reaches 25 points below
    # the price, so 25000 stays the biggest call while price sits at 25022.
    rows = []
    for m in range(20):
        spot = 24950.0 if m < 3 else 25022.0
        rows += [
            _row(m, 25000, "CE", 900_000, spot),
            _row(m, 25050, "CE", 300_000, spot),
            _row(m, 25100, "CE", 200_000, spot),
            _row(m, 24900, "PE", 500_000, spot),
            _row(m, 24950, "PE", 100_000, spot),
        ]
    read = build_levels(LevelInputs("NIFTY", rows))
    assert [c for c in read["changes"] if c["from"] == c["to"]] == []
    resistance = next(z for z in read["zones"] if z["role"] == "resistance")
    assert resistance["anchor"] == 25000
    assert resistance["since"] == START  # its start time is not reset either


def test_a_wall_is_not_read_from_a_strike_the_recorder_stopped_reading():
    rows = []
    for m in range(40):
        rows += [
            _row(m, 25100, "CE", 400_000 + m * 1_000, 25000.0),
            _row(m, 24900, "PE", 400_000 + m * 1_000, 25000.0),
        ]
        if m < 2:  # a far strike read only at the open, then out of the window
            rows.append(_row(m, 26000, "CE", 5_000_000, 25000.0))
    trend = analyse(rows)["trend"]
    assert trend["resistance_now"] == 25100
    assert trend["support_now"] == 24900


def test_the_nearest_expiry_sorts_first():
    expiries = ["02DEC26", "25NOV26", "30DEC26"]
    assert sorted(expiries, key=_expiry_order) == ["25NOV26", "02DEC26", "30DEC26"]
    assert sorted(["x", "25NOV26"], key=_expiry_order) == ["25NOV26", "x"]


def test_a_round_strike_touched_from_above_counts_even_landing_just_under_it():
    from services.oi_levels import core

    rows = []
    path = [25080, 25060, 25040, 25020, 24995] + [25060] * 10  # touches 25000, lands under
    for m, spot in enumerate(path):
        rows += [
            _row(m, 25100, "CE", 900_000, spot),
            _row(m, 24900, "PE", 900_000, spot),
            _row(m, 25000, "CE", 100_000, spot),
            _row(m, 25000, "PE", 100_000, spot),
        ]
    book = core._book(rows)
    history, _ = core._levels(book, False)
    touches = [x for x in core._touches(book, history) if x["kind"] == "round"]
    assert [(x["strike"], x["role"], x["outcome"]) for x in touches] == [
        (25000.0, "support", "held")
    ]


def test_the_gamma_flip_reads_every_recorded_strike_and_the_chain_beyond():
    from services.oi_levels import core

    seen = {}

    def flip(legs, spot, low, high, points):
        seen.update(strikes={k for k, *_ in legs}, low=low, high=high, spot=spot)
        return spot

    def greeks(strikes, ce, pe, forward, years):
        leg = {"iv": 15.0, "gamma": 0.0005}
        return [leg] * len(strikes), [leg] * len(strikes)

    rows = []
    for m in range(6):
        for k in range(24250, 25751, 50):  # 31 strikes, as the recorder keeps
            rows += [
                _row(m, k, "CE", 100_000, 25000.0, 40.0),
                _row(m, k, "PE", 100_000, 25000.0, 40.0),
            ]
    read = build_levels(
        LevelInputs(
            "NIFTY",
            rows,
            expiry_at=datetime(2026, 11, 24, 15, 30),
            greeks=greeks,
            flip=flip,
            beyond=[(23000.0, "PE", 900_000, 0.2), (25000.0, "CE", 5, 0.2)],
        )
    )
    assert read["gamma"]["flip"] == 25000.0
    assert 24250 in seen["strikes"] and 25750 in seen["strikes"]  # beyond the ten either side
    assert 23000 in seen["strikes"]  # from the closing file
    assert seen["low"] == 25000 * 0.95 and seen["high"] == 25000 * 1.05


def test_beyond_legs_come_from_the_previous_close_and_never_from_a_later_day(monkeypatch):
    from datetime import date

    import services.nse_oi_bhavcopy as bhav
    import services.oi_levels_service as svc

    svc.forget_views()
    book = bhav.Bhavcopy(
        date(2026, 11, 19),
        {
            "NIFTY24NOV2625000CE": 1000.0,
            "NIFTY24NOV2625000PE": 2000.0,
            "NIFTY24NOV2624000PE": 3000.0,
        },
        frozenset({"NIFTY"}),
        {"NIFTY24NOV2625000CE": 120.0, "NIFTY24NOV2625000PE": 110.0, "NIFTY24NOV2624000PE": 8.0},
    )
    monkeypatch.setattr(bhav, "cached_previous_session_oi", lambda exchange: book)
    expiry_at = datetime(2026, 11, 24, 15, 30)
    legs = svc._beyond_legs("NIFTY", "24NOV26", "NFO", date(2026, 11, 20), expiry_at)
    assert {(k, s) for k, s, _, _ in legs} == {(25000.0, "CE"), (25000.0, "PE"), (24000.0, "PE")}
    assert all(0 < iv < 3 for *_, iv in legs)
    svc.forget_views()
    assert svc._beyond_legs("NIFTY", "24NOV26", "NFO", date(2026, 11, 19), expiry_at) == []


def test_the_market_read_opens_at_the_index_quote_when_options_start_late():
    from services.market_read.core import MarketMinute, ReadInputs, build_read

    late = START + timedelta(minutes=29)  # options recorded from 09:44
    rows = []
    for m in range(5):
        spot = 25200.0 + m
        t = m + 29
        rows += [_row(t, 25200, "CE", 100_000, spot), _row(t, 25200, "PE", 100_000, spot)]
        rows += [_row(t, 25250, "CE", 100_000, spot), _row(t, 25150, "PE", 100_000, spot)]
    market = [MarketMinute(time=START + timedelta(minutes=m), index=25000.0 + m) for m in range(34)]
    read = build_read(ReadInputs("NIFTY", rows, market=market, prev_close=24900.0))
    assert read["state"]["price"]["open"] == 25000.0  # 09:15, not 09:44's 25,200
    assert read["state"]["price"]["day_low"] == 25000.0
    assert late == read["minute"] - timedelta(minutes=4)


def _rally_rows(shift_resistance: bool):
    oi = {
        (24900, "PE"): 900_000,
        (24950, "PE"): 300_000,
        (25000, "PE"): 200_000,
        (25050, "PE"): 600_000,
        (25100, "PE"): 100_000,
        (25150, "PE"): 50_000,
        (25200, "PE"): 40_000,
        (24900, "CE"): 50_000,
        (24950, "CE"): 60_000,
        (25000, "CE"): 900_000,
        (25050, "CE"): 300_000,
        (25100, "CE"): 200_000,
        (25150, "CE"): 150_000,
        (25200, "CE"): 800_000,
    }
    rows = []
    for m in range(40):
        spot = 24990 + m * 170 / 39  # a 0.7% rally across the 25000 call wall
        for (k, s), v in oi.items():
            if shift_resistance and (k, s) == (25200, "CE"):
                v += m * 10_000  # call writers really building 25200
            rows.append(_row(m, k, s, v + m % 2, spot))
    return rows


def test_the_price_alone_moves_neither_the_ratio_nor_the_walls_vote():
    trend = analyse(_rally_rows(False))["trend"]
    assert trend["resistance_now"] == 25200  # the wall shown follows the price
    assert trend["votes"]["walls"] == 0
    assert trend["votes"]["pcr"] == 0


def test_a_wall_the_open_interest_moved_still_votes():
    trend = analyse(_rally_rows(True))["trend"]
    assert trend["votes"]["walls"] == 1
