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
