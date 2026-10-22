"""Option levels for the chart: support and resistance zones from the Writer Flow record.

Reads the recorded day and hands it to the pure core in services/oi_levels/.
Built on the Writer Flow builder thread and kept until the recorder writes the
underlying again, the same way the Writer Flow view is: a chart asking every
minute costs one build a minute, not one per chart. Earlier days' touches,
which never change, are worked out once each and kept for the hold rates.

The builder is a real OS thread under eventlet, so everything here stays on
DuckDB reads and pure computation: no scoped sessions, no broker or HTTP call.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from datetime import date, datetime

import pytz

from database.oi_flow_db import available_sessions, generation, load_day
from services.oi_levels.core import LevelInputs, build_levels
from services.writer_flow_service import Built, _on_builder, _pause, recorder_enabled
from utils.keyed_locks import KeyedLocks
from utils.logging import get_logger
from utils.thread_safe_cache import LockedTTLCache

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")
PAST_DAYS = 10  # earlier recorded days counted in the hold rates
CYCLE_DAYS = 30  # recorded days the rolling-month level strength is built from
HALF_LIFE_DAYS = 7.0  # a day this old counts half as much, so positions can roll

_views = LockedTTLCache(maxsize=64, ttl=600)
_asked = LockedTTLCache(maxsize=64, ttl=150)  # views a chart asked for lately
_past = LockedTTLCache(maxsize=256, ttl=7 * 86400)  # (underlying, day, expiry) -> touches
_day_oi = LockedTTLCache(
    maxsize=512, ttl=7 * 86400
)  # (underlying, day, expiry) -> {strike: (put, call)}
_building = KeyedLocks(name="oi_levels")


@dataclass(frozen=True)
class LevelsView:
    underlying: str
    day: date | None = None
    expiry: str | None = None


def _hhmm(t: datetime) -> str:
    return t.strftime("%H:%M")


def _epoch(t: datetime) -> int:
    return int(IST.localize(t).timestamp())


def _expiry_at(expiry: str, exchange: str) -> datetime | None:
    from services.option_greeks_service import get_exchange_expiry_time

    try:
        day = datetime.strptime(expiry, "%d%b%y")
    except ValueError:
        return None
    hour, minute = get_exchange_expiry_time(exchange or "NFO")
    return day.replace(hour=hour, minute=minute)


def _past_touches(underlying: str, today: date, sessions: list[dict]) -> list[dict]:
    """Touches on earlier recorded days, each worked out once."""
    out: list[dict] = []
    for s in sessions:
        if s["underlying"] != underlying or s["trade_date"] >= today:
            continue
        key = (underlying, s["trade_date"], s["expiry"])
        touches = _past.get(key)
        if touches is None:
            rows = load_day(underlying, s["expiry"], s["trade_date"], pause=_pause)
            read = build_levels(LevelInputs(underlying, rows))
            touches = read["touches"] if read else []
            _past[key] = touches
        out.extend(touches)
    return out


def _day_strikes(underlying: str, expiry: str, day: date) -> dict[float, tuple[float, float]]:
    """Each strike's final-minute put and call open interest for one recorded day.
    Cached: a finished day never changes, so it is read from DuckDB once."""
    from services.market_read.core import _book

    key = (underlying, day, expiry)
    held = _day_oi.get(key)
    if held is not None:
        return held
    rows = load_day(underlying, expiry, day, pause=_pause)
    book = _book(rows)
    out: dict[float, tuple[float, float]] = {}
    if book is not None:
        legs = book.legs[book.times[-1]]
        strikes = {k for (_, k) in legs}
        out = {k: (legs.get(("PE", k), (0.0,))[0], legs.get(("CE", k), (0.0,))[0]) for k in strikes}
    _day_oi[key] = out
    return out


def _normalise(day_oi: dict[float, tuple[float, float]]) -> dict[float, tuple[float, float]]:
    """Each day's open interest as a share of that day's biggest put and call wall,
    so a quiet weekly expiry and a heavy monthly one count on the same scale."""
    mp = max((p for p, _ in day_oi.values()), default=0.0) or 1.0
    mc = max((c for _, c in day_oi.values()), default=0.0) or 1.0
    return {k: (p / mp, c / mc) for k, (p, c) in day_oi.items()}


def _cycle_strength(
    underlying: str, today: date, sessions: list[dict], today_walls: list[dict]
) -> dict[float, tuple[float, float]]:
    """How strongly each strike has held put and call writers over the recorded
    month, across weekly and monthly expiries, recent days weighted more. The
    result is 0..1 against the strongest wall, so the overlay ranks the levels
    where the majority of positions have built, not only where they sit today."""
    acc: dict[float, list[float]] = {}
    # Today, from the live walls already built, at full weight.
    today_norm = _normalise({w["strike"]: (w["put"], w["call"]) for w in today_walls})
    for k, (p, c) in today_norm.items():
        acc.setdefault(k, [0.0, 0.0])
        acc[k][0] += p
        acc[k][1] += c
    # Earlier recorded days, across every expiry, fading with age.
    for s in sessions:
        if s["underlying"] != underlying:
            continue
        age = (today - s["trade_date"]).days
        if age <= 0 or age > CYCLE_DAYS:
            continue
        weight = 0.5 ** (age / HALF_LIFE_DAYS)
        for k, (p, c) in _normalise(_day_strikes(underlying, s["expiry"], s["trade_date"])).items():
            acc.setdefault(k, [0.0, 0.0])
            acc[k][0] += weight * p
            acc[k][1] += weight * c
    peak = max((max(v) for v in acc.values()), default=1.0) or 1.0
    return {k: (round(v[0] / peak, 3), round(v[1] / peak, 3)) for k, v in acc.items()}


def levels_payload(view: LevelsView) -> dict:
    """The zones for the chart. Raises on a read failure."""
    from services.option_greeks_service import calculate_chain_greeks
    from services.strike_profile_service import find_gamma_flip

    sessions = available_sessions(limit_days=CYCLE_DAYS + 1)
    held = [s for s in sessions if s["underlying"] == view.underlying]
    if view.day:
        held = [s for s in held if s["trade_date"] == view.day]
    if view.expiry:
        held = [s for s in held if s["expiry"] == view.expiry]
    if not held:
        reason = (
            "Nothing recorded for this yet. Turn Writer Flow on for this chart to start recording it."
            if recorder_enabled()
            else "The Writer Flow recorder is off. Set OI_FLOW_RECORDER_ENABLED=true in .env and restart OpenAlgo."
        )
        return {"status": "empty", "message": reason}
    chosen = held[0]
    rows = load_day(view.underlying, chosen["expiry"], chosen["trade_date"], pause=_pause)
    exchange = rows[0].get("exchange") if rows else "NFO"
    expiry_at = _expiry_at(chosen["expiry"], exchange)
    read = build_levels(
        LevelInputs(
            underlying=view.underlying,
            samples=rows,
            expiry_at=expiry_at,
            expiry_day=bool(expiry_at and expiry_at.date() == chosen["trade_date"]),
            greeks=calculate_chain_greeks,
            flip=find_gamma_flip,
            past_touches=_past_touches(view.underlying, chosen["trade_date"], sessions),
        )
    )
    if read is None:
        return {
            "status": "empty",
            "message": "Too few minutes recorded today to place a level yet.",
        }

    def touch(x):
        return {**x, "time": _hhmm(x["time"]), "epoch": _epoch(x["time"])}

    # Rank each strike by how strongly it has held writers over the recorded
    # month (today live, earlier days fading), so the overlay draws the levels
    # where the majority of positions have built, not only today's snapshot.
    cycle = _cycle_strength(view.underlying, chosen["trade_date"], sessions, read["walls"])
    walls = [
        {
            **w,
            "persist_put": cycle.get(w["strike"], (0.0, 0.0))[0],
            "persist_call": cycle.get(w["strike"], (0.0, 0.0))[1],
        }
        for w in read["walls"]
    ]
    days = len({s["trade_date"] for s in sessions if s["underlying"] == view.underlying})
    return {
        "status": "success",
        "underlying": view.underlying,
        "expiry": chosen["expiry"],
        "date": chosen["trade_date"].isoformat(),
        "minute": _hhmm(read["minute"]),
        "epoch": _epoch(read["minute"]),
        "spot": read["spot"],
        "step": read["step"],
        # As the broker quotes it: lots on MCX, quantity on NSE and BSE.
        "oi_unit": "lots" if exchange == "MCX" else "quantity",
        "thresholds": read["thresholds"],
        "zones": [
            {
                **{k: v for k, v in z.items() if k not in ("since", "touches_today")},
                "since": _hhmm(z["since"]),
                "since_epoch": _epoch(z["since"]),
                "touches_today": [touch(x) for x in z["touches_today"]],
            }
            for z in read["zones"]
        ],
        "walls": walls,
        "history": {
            "epochs": [_epoch(r["time"]) for r in read["history"]],
            "support": [r["support"] for r in read["history"]],
            "resistance": [r["resistance"] for r in read["history"]],
        },
        "changes": [
            {**c, "time": _hhmm(c["time"]), "epoch": _epoch(c["time"])} for c in read["changes"]
        ],
        "gamma": {
            **{k: v for k, v in read["gamma"].items() if k != "series"},
            "epochs": [_epoch(g["time"]) for g in read["gamma"]["series"]],
            "flips": [g["flip"] for g in read["gamma"]["series"]],
        },
        "touches": [touch(x) for x in read["touches"]],
        "stats": {**read["stats"], "days": days},
    }


def _build(view: LevelsView, gen: int) -> Built:
    payload = levels_payload(view)
    body = json.dumps(payload, separators=(",", ":")).encode()
    return Built(
        generation=gen,
        status=200,
        body=body,
        gzipped=gzip.compress(body, 6),
        empty=payload.get("status") == "empty",
    )


def levels_answer(view: LevelsView) -> Built:
    """From memory while nothing new is recorded, else built once. Raises when the record cannot be read."""
    _asked[view] = True
    return _answer(view)


def _answer(view: LevelsView) -> Built:
    held = _views.get(view)
    if held is not None and held.generation == generation(view.underlying):
        return held
    with _building.hold(view):
        gen = generation(view.underlying)
        held = _views.get(view)
        if held is not None and held.generation == gen:
            return held
        built = _on_builder(_build, view, gen)
        _views[view] = built
        return built


def rebuild_watched(underlyings: set[str]) -> int:
    """After a write: rebuild the levels a chart is showing, so its next fetch is served from memory. Never raises."""
    rebuilt = 0
    for view in list(_asked.keys()):
        if view.underlying not in underlyings:
            continue
        try:
            _answer(view)
            rebuilt += 1
        except Exception:
            logger.exception(f"Option levels: rebuilding {view.underlying} failed")
    return rebuilt


def forget_views() -> None:
    """Drops every kept answer. For tests."""
    _views.clear()
    _asked.clear()
    _past.clear()
    _day_oi.clear()
