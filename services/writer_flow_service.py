"""
Writer Flow answers, built once per recorded write and shared by every viewer.

The record changes once a minute, when the OI Flow recorder writes. Every chart,
page and tab polling /writerflow/api/flow used to rebuild the whole day for
itself: on 16-Oct-2026 at 13:00 that was 14,198 rows loaded and re-read, 0.5
to 0.9 s per request, growing with every minute of the session and repeated for
each open view.

Now an answer is kept, as the bytes sent, until its underlying's write count
(database.oi_flow_db.generation) moves. Two viewers asking at once wait on one
build. After each write the recorder rebuilds only the views somebody asked for
in the last ten minutes, so the next refresh is served from memory and nothing
is computed for a view nobody is watching.
"""

from __future__ import annotations

import gzip
import json
import os
from dataclasses import dataclass
from datetime import date, datetime

import pytz

from database.oi_flow_db import available_sessions, generation, load_day
from services.oi_flow_analysis import FlowSettings, analyse, bias
from utils.keyed_locks import KeyedLocks
from utils.logging import get_logger
from utils.thread_safe_cache import LockedTTLCache

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")
STEPS = {"auto": None, "60": 60, "180": 180, "300": 300}
CLASS_CODE = {
    "writing": "W",
    "covering": "C",
    "buying": "B",
    "unwinding": "U",
    "adding": "A",
    "reducing": "R",
    "steady": "S",
}
# A view not asked for in this long is dropped, and stops being rebuilt after
# writes. Sixty-four views is far more than one trader opens.
VIEW_TTL_SECONDS = 600
VIEWS_MAX = 64


@dataclass(frozen=True)
class FlowView:
    """What one request asks for, already validated. Hashable: it is the cache key."""

    underlying: str
    expiry: str = ""
    day: date | None = None
    step: str = "auto"
    noise_pct: float = 0.5
    noise_lots: int = 5
    quiet_minutes: float = 5.0
    all_strikes: bool = False
    # The chart draws no per-cell reading, so it leaves out the largest field.
    lite: bool = False


@dataclass(frozen=True)
class Built:
    generation: int
    status: int
    body: bytes
    gzipped: bytes


_views = LockedTTLCache(maxsize=VIEWS_MAX, ttl=VIEW_TTL_SECONDS)
# Views a request asked for, renewed only by requests. Kept apart from _views
# because a rebuild stores into _views and so renews it: keyed on that, a view
# nobody had looked at for hours was still rebuilt after every write.
_asked = LockedTTLCache(maxsize=VIEWS_MAX, ttl=VIEW_TTL_SECONDS)
_building = KeyedLocks(name="writer_flow")


def recorder_enabled() -> bool:
    return os.getenv("OI_FLOW_RECORDER_ENABLED", "false").lower() == "true"


def _hhmm(t: datetime) -> str:
    return t.strftime("%H:%M")


def _epoch(t: datetime) -> int:
    """UTC seconds for a recorded time, which is stored as IST wall clock.

    The chart places everything by UTC seconds, the same clock as its bars.
    """
    return int(IST.localize(t).timestamp())


def _side_state(side: str, state: dict | None) -> dict | None:
    if not state:
        return None
    return {
        **state,
        "bias": bias(side, state["cls"]),
        "window_change": {str(k): v for k, v in state["window_change"].items()},
        "last_seen": _hhmm(state["last_seen"]) if state.get("last_seen") else None,
    }


def flow_payload(view: FlowView) -> dict:
    """The day read strike by strike, as the page and the chart draw it. Raises on a read failure."""
    held = [s for s in available_sessions() if s["underlying"] == view.underlying]
    if view.day:
        held = [s for s in held if s["trade_date"] == view.day]
    if view.expiry:
        held = [s for s in held if s["expiry"] == view.expiry]
    if not held:
        reason = (
            "Nothing recorded for this yet. The recorder samples every minute while its market is open, if it is listed in OI_FLOW_UNDERLYINGS."
            if recorder_enabled()
            else "The Writer Flow recorder is off. Set OI_FLOW_RECORDER_ENABLED=true in .env and restart OpenAlgo."
        )
        return {"status": "empty", "message": reason, "recorder_enabled": recorder_enabled()}
    chosen = held[0]
    out = analyse(
        load_day(view.underlying, chosen["expiry"], chosen["trade_date"]),
        FlowSettings(
            step_seconds=STEPS[view.step],
            noise_pct=view.noise_pct,
            noise_lots=view.noise_lots,
            quiet_minutes_to_break=view.quiet_minutes,
        ),
    )

    steps = out["steps"]
    step_index = {t: i for i, t in enumerate(steps)}
    # By default only the strikes sampled at the latest step: the window the
    # recorder is watching now. The chart asks for all of them, because a
    # strike price passed through earlier still has a history to paint there.
    rows = [r for r in out["strikes"] if view.all_strikes or r.get("observed")]
    strikes = [r["strike"] for r in rows]
    strike_index = {k: i for i, k in enumerate(strikes)}
    heat = {
        "CE": [[None] * len(steps) for _ in strikes],
        "PE": [[None] * len(steps) for _ in strikes],
    }
    codes = {"CE": [[""] * len(steps) for _ in strikes], "PE": [[""] * len(steps) for _ in strikes]}
    for c in out["cells"]:
        if c["strike"] not in strike_index:
            continue
        i, j = strike_index[c["strike"]], step_index[c["time"]]
        # Signed by what it means for the underlying, sized by how much moved.
        heat[c["side"]][i][j] = bias(c["side"], c["cls"]) * abs(c["oi_change"])
        codes[c["side"]][i][j] = CLASS_CODE[c["cls"]]

    payload = {
        "status": "success",
        "underlying": view.underlying,
        "expiry": chosen["expiry"],
        "date": chosen["trade_date"].isoformat(),
        "recorder_enabled": recorder_enabled(),
        "step_seconds": out["step_seconds"],
        "oi_unit": out.get("oi_unit", "contracts"),
        "spot": out["spot"],
        "times": [_hhmm(t) for t in steps],
        "times_epoch": [_epoch(t) for t in steps],
        "strikes": strikes,
        "heat": {"ce": heat["CE"], "pe": heat["PE"]},
        "totals": [
            {**t, "time": _hhmm(t["time"]), "epoch": _epoch(t["time"])} for t in out["totals"]
        ],
        "ladder": [
            {
                "strike": r["strike"],
                "atm": r.get("atm", False),
                "observed": r.get("observed", False),
                "ce": _side_state("CE", r.get("ce")),
                "pe": _side_state("PE", r.get("pe")),
            }
            for r in rows
        ],
        "events": [
            {
                **e,
                "time": _hhmm(e["time"]),
                "epoch": _epoch(e["time"]),
                "bias": bias(e["side"], e["kind"]),
            }
            for e in out["events"]
        ],
    }
    if not view.lite:
        payload["heat_cls"] = {"ce": codes["CE"], "pe": codes["PE"]}
    return payload


def _build(view: FlowView, gen: int) -> Built:
    body = json.dumps(flow_payload(view), separators=(",", ":")).encode()
    return Built(generation=gen, status=200, body=body, gzipped=gzip.compress(body, 6))


def flow_answer(view: FlowView) -> Built:
    """The answer for a request: from memory while nothing new is recorded, else built once.

    Raises when the record cannot be read; nothing is kept for a failed build.
    """
    _asked[view] = True
    return _answer(view)


def _answer(view: FlowView) -> Built:
    held = _views.get(view)
    if held is not None and held.generation == generation(view.underlying):
        return held
    with _building.hold(view):
        # Read before building: a write that lands mid-build leaves this
        # answer stamped one behind, and the next request builds again.
        gen = generation(view.underlying)
        held = _views.get(view)
        if held is not None and held.generation == gen:
            return held
        built = _build(view, gen)
        _views[view] = built
        return built


def rebuild_watched(underlyings: set[str]) -> int:
    """After a write: rebuild the views of these underlyings that somebody is watching. Never raises."""
    rebuilt = 0
    for view in list(_asked.keys()):
        if view.underlying not in underlyings:
            continue
        try:
            _answer(view)
            rebuilt += 1
        except Exception:
            logger.exception(f"Writer Flow: rebuilding the {view.underlying} view failed")
    return rebuilt


def forget_views() -> None:
    """Drops every kept answer. For tests, which each expect a cold start."""
    _views.clear()
    _asked.clear()
