"""Writer Flow reading who acted from the trades, not only the premium.

Two halves: the pure reading in services.oi_flow_analysis (trade_class and
analyse with trades), and services.tick_trade_flow building per-minute buy and
sell volume from real Parquet written the way the tick store writes it.
"""

from datetime import date, datetime, timedelta

import pytest
import pytz

from database import ticks_store
from services import tick_trade_flow
from services.oi_flow_analysis import (
    BUYING,
    COVERING,
    UNWINDING,
    WRITING,
    FlowSettings,
    analyse,
    trade_class,
    trades_between,
)

IST = pytz.timezone("Asia/Kolkata")
T0 = datetime(2026, 11, 20, 9, 20)
LOT = 65
SYM = "NIFTY24NOV2625600CE"
S = FlowSettings(step_seconds=60, noise_pct=1.0, noise_lots=1, read_trades=True)


@pytest.mark.parametrize(
    "up, buy, sell, mid, expected",
    [
        (True, 100, 900, 0, WRITING),  # OI up, sellers pressing
        (True, 900, 100, 0, BUYING),  # OI up, buyers pressing
        (False, 900, 100, 0, COVERING),  # OI down, buyers pressing
        (False, 100, 900, 0, UNWINDING),  # OI down, sellers pressing
        (True, 520, 480, 0, None),  # lean 0.04, too even to decide
        (True, 100, 300, 600, None),  # only 40% placed at bid or offer
        (True, 0, 0, 0, None),  # no trades
    ],
)
def test_trade_class(up, buy, sell, mid, expected):
    assert trade_class(up, buy, sell, mid, S) == expected


def test_trades_between_takes_whole_minutes_end_excluded():
    minutes = {
        T0: (1, 2, 3),
        T0 + timedelta(minutes=1): (10, 20, 30),
        T0 + timedelta(minutes=2): (9, 9, 9),
    }
    got = trades_between(minutes, T0 + timedelta(seconds=40), T0 + timedelta(minutes=2, seconds=5))
    assert got == (11, 22, 33)
    assert trades_between(minutes, T0 + timedelta(seconds=10), T0 + timedelta(seconds=50)) is None


def _samples(points):
    return [
        {
            "sample_time": T0 + timedelta(minutes=i, seconds=30),
            "strike": 25600.0,
            "side": "CE",
            "symbol": SYM,
            "expiry": "24NOV26",
            "exchange": "NFO",
            "oi": oi,
            "ltp": ltp,
            "spot": 25550.0,
            "lot_size": LOT,
        }
        for i, (oi, ltp) in enumerate(points)
    ]


# OI rises 1,00,000 while the premium rises: the premium alone reads buying.
RISING = _samples([(1_000_000, 100.0), (1_100_000, 101.0)])


def test_trades_overrule_the_premium_where_they_decide():
    trades = {SYM: {T0: (1000, 9000, 500), T0 + timedelta(minutes=1): (500, 0, 0)}}
    out = analyse(RISING, S, trades=trades)
    (cell,) = out["cells"]
    assert cell["premium_cls"] == BUYING
    assert cell["cls"] == WRITING
    assert cell["trade_lean"] == -0.8
    assert out["trades"] == {
        "steps": 1,
        "decided": 1,
        "compared": 1,
        "agree": 0,
        "agree_share": 0.0,
    }
    assert out["strikes"][0]["ce"]["cls"] == WRITING


def test_premium_reading_stands_when_trades_do_not_decide():
    trades = {SYM: {T0: (5000, 4900, 0)}}
    out = analyse(RISING, S, trades=trades)
    (cell,) = out["cells"]
    assert cell["cls"] == BUYING
    assert "premium_cls" not in cell
    assert out["trades"]["decided"] == 0


def test_off_by_default_and_unchanged_without_trades():
    trades = {SYM: {T0: (0, 9000, 0)}}
    plain = FlowSettings(step_seconds=60, noise_pct=1.0, noise_lots=1)
    out = analyse(RISING, plain, trades=trades)
    assert out["cells"][0]["cls"] == BUYING
    assert out["trades"] is None
    assert analyse(RISING, plain)["cells"] == analyse(RISING, S)["cells"]


# --- tick_trade_flow on real Parquet ---------------------------------------


def _ms(t: datetime) -> int:
    return int(IST.localize(t).timestamp() * 1000)


def _tick(t, ltp, volume, bid=None, ask=None):
    data = {"ltp": ltp, "volume": volume, "oi": 1_000_000, "timestamp": _ms(t), "ltt": _ms(t)}
    if bid is not None:
        data["depth"] = {
            "buy": [{"price": bid, "quantity": 100, "orders": 1}],
            "sell": [{"price": ask, "quantity": 100, "orders": 1}],
        }
    return ticks_store.row_from_tick(SYM, "NFO", "NIFTY", data, T0.date())


@pytest.fixture
def lake(tmp_path, monkeypatch):
    monkeypatch.setenv("TICKS_DATA_DIR", str(tmp_path))
    tick_trade_flow.forget()
    yield tmp_path
    tick_trade_flow.forget()


def _write(rows):
    assert ticks_store.write_batch(rows) == len(rows)


def test_minute_trades_reads_the_side_from_the_quote_before(lake):
    m0, m1 = T0, T0 + timedelta(minutes=1)
    _write(
        [
            _tick(
                m0 + timedelta(seconds=1), 100.0, 1000, bid=99.5, ask=100.0
            ),  # first: no previous volume
            _tick(m0 + timedelta(seconds=2), 100.0, 1300),  # at the offer: 300 bought
            _tick(
                m0 + timedelta(seconds=3), 99.5, 1500, bid=99.5, ask=100.0
            ),  # at the bid: 200 sold
            _tick(m0 + timedelta(seconds=4), 99.75, 1550),  # inside: 50 unplaced
            _tick(
                m1 + timedelta(seconds=5), 99.0, 1650, bid=99.0, ask=99.5
            ),  # quote was 99.5/100: sold 100
        ]
    )
    got = tick_trade_flow.minute_trades("NIFTY", T0.date())
    assert got[SYM][m0] == (300, 200, 50)
    assert got[SYM][m1] == (0, 100, 0)


def test_incremental_read_matches_a_full_read(lake):
    rows = []
    vol = 0
    for i in range(10 * 6):  # ten minutes, a tick every 10 s
        t = T0 + timedelta(seconds=10 * i)
        vol += 50
        rows.append(_tick(t, 100.0 if i % 2 else 99.5, vol, bid=99.5, ask=100.0))
    _write(rows[:35])
    tick_trade_flow.minute_trades("NIFTY", T0.date())
    _write(rows[35:])
    incremental = tick_trade_flow.minute_trades("NIFTY", T0.date())
    tick_trade_flow.forget()
    full = tick_trade_flow.minute_trades("NIFTY", T0.date())
    assert incremental == full
    assert len(full[SYM]) == 10


def test_no_ticks_for_the_day_is_none(lake):
    assert tick_trade_flow.minute_trades("NIFTY", date(2026, 1, 1)) is None
