"""GEX in the same units on every exchange: open interest as a quantity."""

import services.gex_service as gex
from services.strike_profile_service import _leg_exposure


def _chain(exchange_oi):
    return (
        True,
        {
            "chain": [
                {
                    "strike": 100.0,
                    "ce": {"symbol": "X100CE", "oi": exchange_oi, "ltp": 5.0, "lotsize": 65},
                    "pe": None,
                }
            ],
            "atm_strike": 100.0,
            "underlying_ltp": 100.0,
        },
        200,
    )


def _run(monkeypatch, exchange, oi):
    monkeypatch.setattr(gex, "get_option_chain", lambda **k: _chain(oi))
    monkeypatch.setattr(gex, "_get_nearest_futures_price", lambda **k: None)
    monkeypatch.setattr(
        gex,
        "calculate_greeks",
        lambda **k: (True, {"status": "success", "greeks": {"gamma": 0.01}}, 200),
    )
    ok, body, _ = gex.get_gex_data("X", exchange, "01DEC26", "key")
    assert ok
    return body["chain"][0]["ce_gex"]


def test_nfo_open_interest_is_already_a_quantity(monkeypatch):
    # 6,500 is 100 lots of 65: GEX is gamma times 6,500, not times 422,500.
    assert _run(monkeypatch, "NFO", 6500) == 65.0


def test_mcx_open_interest_is_in_lots(monkeypatch):
    assert _run(monkeypatch, "MCX", 100) == 65.0


def test_strike_profile_counts_mcx_lots_as_quantity():
    leg = {"oi": 100, "lotsize": 65, "gamma": 0.01, "delta": 0.5}
    nfo = _leg_exposure({**leg, "oi": 6500}, 100.0, 1.0, "NFO")
    mcx = _leg_exposure(leg, 100.0, 1.0, "MCX")
    assert mcx["gex"] == nfo["gex"] and mcx["dex"] == nfo["dex"]
    assert mcx["oi"] == 100  # the open interest column still shows the broker's figure
