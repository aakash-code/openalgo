"""The nearest expiry's strikes outside OI Profile's window, from NSE's file,
for a whole-chain max pain."""

from services.oi_profile_service import beyond_window

BOOK = {
    "NIFTY23NOV2625000CE": 100.0,
    "NIFTY23NOV2625000PE": 200.0,
    "NIFTY23NOV2624000PE": 5000.0,
    "NIFTY23NOV2626000CE": 7000.0,
    "NIFTY23NOV2626000PE": 0.0,
    "NIFTY23NOV2627000CE": 0.0,
    "NIFTY30NOV2624000PE": 999.0,
    "BANKNIFTY23NOV2660000CE": 1.0,
    "NIFTY23NOV26FUT": 1.0,
}


def test_only_that_expiry_and_only_outside_the_window():
    rows = beyond_window(BOOK, "nifty23nov26", {25000.0})
    assert rows == [
        {"strike": 24000.0, "ce_oi": 0.0, "pe_oi": 5000.0},
        {"strike": 26000.0, "ce_oi": 7000.0, "pe_oi": 0.0},
    ]


def test_strikes_holding_nothing_are_left_out():
    assert all(r["strike"] != 27000.0 for r in beyond_window(BOOK, "NIFTY23NOV26", set()))


def test_the_window_covering_everything_leaves_nothing():
    assert beyond_window(BOOK, "NIFTY23NOV26", {24000.0, 25000.0, 26000.0, 27000.0}) == []
