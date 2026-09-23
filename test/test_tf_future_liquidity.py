"""Future-liquidity enrichment for the Intraday Boost list.

Guards what makes this affordable and honest: the whole list costs ONE broker
call however many symbols it carries, a single noisy reading cannot swing the
chip, and an unquoted contract reads as "no data" rather than as a perfect book.
"""

import services.tf_future_liquidity_service as svc
from services.tf_future_liquidity_service import (
    attach_future_liquidity,
    compute_liquidity,
    spread_tier,
)


def _reset() -> None:
    svc._cache.clear()
    svc._pending.clear()
    svc._samples.clear()


def _quoting(spreads, monkeypatch):
    """Feed a sequence of spreads for one symbol, one per fill."""
    _reset()
    monkeypatch.setattr(svc, "future_for", lambda s: "X29SEP26FUT")
    seen = []

    def fake(auth_token, feed_token, broker, symbols):
        bid = 100.0
        ask = bid + spreads[len(seen)]  # spread in rupees on a 100.0 price
        seen.append(1)
        return (
            True,
            {
                "results": [
                    {
                        "symbol": "X29SEP26FUT",
                        "data": {"bid": bid, "ask": ask, "ltp": 100.0, "volume": 1},
                    }
                ]
            },
            200,
        )

    monkeypatch.setattr(svc, "get_multiquotes_with_auth", fake)
    out = []
    for _ in spreads:
        svc._background_fill(["X"], "t", "upstox")
        items = [{"symbol": "X"}]
        attach_future_liquidity(items)
        out.append(items[0]["fut_spread_pct"])
    return out


def test_one_wild_reading_does_not_move_the_reported_spread(monkeypatch):
    """THE DEFECT THIS GUARDS. Measured 22-Sep-2026, sampling 12 contracts every
    12s: the raw reading crossed a tier boundary 57 times in three minutes,
    a median of 5 only 15. CONCOR alone read 0.096, 0.043, 0.075 in consecutive
    minutes. Reporting the latest tick makes the chip flicker between WIDE and
    tight, which both teaches the reader to ignore it and can show a healthy
    book at the exact moment they look."""
    # Four tight readings then one wild spike (0.30 rupees on 100.0 = 0.30%).
    reported = _quoting([0.02, 0.02, 0.02, 0.02, 0.30], monkeypatch)
    assert reported[-1] == 0.02, f"one spike moved the reported spread to {reported[-1]}"
    assert svc.spread_tier(reported[-1]) == "tight"


def test_a_sustained_widening_does_come_through(monkeypatch):
    """Smoothing must not become deafness -- a book that genuinely widens and
    stays wide has to reach the chip."""
    reported = _quoting([0.02, 0.02, 0.12, 0.12, 0.12, 0.12, 0.12], monkeypatch)
    assert svc.spread_tier(reported[-1]) == "wide", reported


def test_whole_list_costs_one_broker_call(monkeypatch):
    """THE PERF PROPERTY. The siblings (CPR, first-candle, directional score)
    each cost one history call per symbol, which is why they are rate-limited
    and filled once a day. This one refreshes every REFRESH_INTERVAL_SEC, which
    is only affordable because the multiquote path batches. One call per symbol
    here would be ~400 calls a minute against the broker."""
    _reset()
    calls = []

    def fake_multiquotes(auth_token, feed_token, broker, symbols):
        calls.append(symbols)
        return (
            True,
            {
                "results": [
                    {
                        "symbol": s["symbol"],
                        "exchange": "NFO",
                        "data": {"bid": 100.0, "ask": 100.05, "ltp": 100.0, "volume": 5_000},
                    }
                    for s in symbols
                ]
            },
            200,
        )

    monkeypatch.setattr(svc, "get_multiquotes_with_auth", fake_multiquotes)
    monkeypatch.setattr(svc, "future_for", lambda s: f"{s}29SEP26FUT")

    symbols = [f"SYM{i}" for i in range(200)]
    svc._background_fill(symbols, "token", "upstox")

    assert len(calls) == 1, f"expected 1 batched call, made {len(calls)}"
    assert len(calls[0]) == 200

    items = [{"symbol": s} for s in symbols]
    attach_future_liquidity(items)
    assert all(i["fut_spread_pct"] == 0.05 for i in items)
    assert all(i["fut_tier"] == "ok" for i in items)


def test_a_stock_with_no_future_is_marked_not_dropped(monkeypatch):
    """A stock with no listed future has no options either -- that is worth
    saying on the row, and it must not be confused with "not fetched yet"."""
    _reset()
    monkeypatch.setattr(
        svc, "get_multiquotes_with_auth", lambda *a, **k: (True, {"results": []}, 200)
    )
    monkeypatch.setattr(svc, "future_for", lambda s: None)

    svc._background_fill(["DALBHARAT"], "token", "upstox")
    items = [{"symbol": "DALBHARAT"}]
    attach_future_liquidity(items)
    assert items[0]["fut_symbol"] is None
    assert items[0]["fut_spread_pct"] is None
    assert items[0]["fut_tier"] is None


def test_broker_failure_leaves_the_row_intact(monkeypatch):
    """A broker hiccup costs the chip, never the list."""
    _reset()
    monkeypatch.setattr(
        svc,
        "get_multiquotes_with_auth",
        lambda *a, **k: (False, {"message": "rate limited"}, 429),
    )
    monkeypatch.setattr(svc, "future_for", lambda s: f"{s}29SEP26FUT")

    svc._background_fill(["RELIANCE"], "token", "upstox")
    items = [{"symbol": "RELIANCE", "ltp": 1248.0}]
    attach_future_liquidity(items)
    assert items[0]["ltp"] == 1248.0
    assert items[0]["fut_spread_pct"] is None
    assert items[0]["fut_tier"] is None
    assert not svc._pending, "a failed fill must release its pending symbols"


def test_unquoted_contract_is_not_a_perfect_book():
    """THE DEFECT THIS GUARDS. Outside market hours bid/ask come back as 0, and
    a naive (ask-bid)/ltp reports 0.000% -- which renders as the tightest book
    on the list, on every row, exactly when none of them are quoting."""
    assert compute_liquidity(0, 0, 0, 0) == (None, None, None)
    assert compute_liquidity(None, None, 100.0, 10)[0] is None
    # A crossed book is a stale quote, not free money.
    assert compute_liquidity(101.0, 100.0, 100.0, 10)[0] is None


def test_spread_tiers_match_the_measured_range():
    """Anchored on real quotes taken 22-Sep-2026: TITAN was the tightest future
    on the list at 0.010%, SOLARINDS was 0.099% while ranked #1."""
    assert spread_tier(0.010) == "tight"
    assert spread_tier(0.040) == "ok"
    assert spread_tier(0.099) == "wide"
    assert spread_tier(None) is None
    # Boundaries land in 'ok' from both sides -- neither edge falls through.
    assert spread_tier(svc.SPREAD_TIGHT_PCT) == "ok"
    assert spread_tier(svc.SPREAD_WIDE_PCT) == "ok"


def test_turnover_is_rupees_not_raw_volume():
    """Raw volume is not comparable across contracts with different lot sizes
    and prices; rupees traded is."""
    _, _, cr = compute_liquidity(100.0, 100.1, 100.0, 1_000_000)
    assert cr == 10.0  # 1e6 * 100 / 1e7
