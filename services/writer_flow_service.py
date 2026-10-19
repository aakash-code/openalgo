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

A build is 0.5 to 0.8 s of Python (measured on 16-Oct-2026's full NIFTY,
BANKNIFTY, SENSEX, CRUDEOIL and NATURALGAS days, 24,000 to 36,000 rows each).
Two things kept that from freezing the platform while a trader flicks through
symbols:

- **Only charts open now are rebuilt after a write.** A chart asks every
  minute, so a view asked for in the last WATCHED_SECONDS is on screen. Rebuilding every view
  asked for in the last ten minutes meant eight symbols looked at in passing
  cost five to six seconds of CPU every minute, all market day.
- **Every build runs on one real OS thread, one at a time.** Under the
  production eventlet worker a build run in a request or a scheduler job holds
  the only hub: orders, charts and the tick stream all waited it out. On its
  own thread the hub keeps running while the request waits cooperatively
  (utils.real_threading.wait_for), and one at a time means a burst of symbol
  switches queues builds instead of running them all at once on the same CPU.
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
from utils import real_threading
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
# The chart asks every 60 s; a view asked for within this long is on screen
# now and is rebuilt after each write. One looked at and left is rebuilt only
# if somebody asks for it again.
WATCHED_SECONDS = 150
# A build normally takes under a second. Waiting this long means the builder
# is stuck behind something, and the trader is told to try again.
BUILD_WAIT_SECONDS = 30
# How long a build steps aside each time it pauses (oi_flow_analysis.PAUSE_EVERY).
PAUSE_SECONDS = 0.001


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
    # Nothing recorded: the caller may say why in its own words.
    empty: bool = False


_views = LockedTTLCache(maxsize=VIEWS_MAX, ttl=VIEW_TTL_SECONDS)
# Views a request asked for, renewed only by requests. Kept apart from _views
# because a rebuild stores into _views and so renews it: keyed on that, a view
# nobody had looked at for hours was still rebuilt after every write.
_asked = LockedTTLCache(maxsize=VIEWS_MAX, ttl=WATCHED_SECONDS)
_building = KeyedLocks(name="writer_flow")


class _Job:
    """One build handed to the builder thread. Only a real Event crosses back."""

    __slots__ = ("args", "done", "error", "fn", "result")

    def __init__(self, fn, args):
        self.fn = fn
        self.args = args
        self.done = real_threading.Event()
        self.result = None
        self.error = None


_jobs = real_threading.Queue()
_builder = None
_builder_lock = real_threading.Lock()
_STOP = object()


def _builder_loop() -> None:
    while True:
        job = _jobs.get()
        if job is _STOP:
            return
        try:
            job.result = job.fn(*job.args)
        except BaseException as e:  # handed back to the waiting caller, who logs it
            job.error = e
        finally:
            job.done.set()


def _ensure_builder() -> None:
    global _builder
    with _builder_lock:
        if _builder is None or not _builder.is_alive():
            _builder = real_threading.Thread(
                target=_builder_loop, name="writer-flow-builder", daemon=True
            )
            _builder.start()


def _on_builder(fn, *args):
    """Run fn on the builder thread and wait for it without holding the hub. Raises what fn raised."""
    _ensure_builder()
    job = _Job(fn, args)
    _jobs.put(job)
    if not real_threading.wait_for(job.done, BUILD_WAIT_SECONDS):
        raise TimeoutError("Writer Flow build did not finish in time")
    if job.error is not None:
        raise job.error
    return job.result


def stop_builder() -> None:
    """Let the builder thread finish. Called when the recorder shuts down."""
    global _builder
    with _builder_lock:
        builder, _builder = _builder, None
    if builder is not None and builder.is_alive():
        _jobs.put(_STOP)
        real_threading.join(builder, timeout=5)


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


def _pause() -> None:
    """Step aside for a moment, so a request waiting for the interpreter runs now.

    The unpatched sleep: under eventlet the builder is a real thread, and the
    patched one would try to switch on a hub that is not its own.
    """
    real_threading.sleep(PAUSE_SECONDS)


def flow_payload(view: FlowView) -> dict:
    """The day read strike by strike, as the page and the chart draw it. Raises on a read failure."""
    held = [s for s in available_sessions() if s["underlying"] == view.underlying]
    if view.day:
        held = [s for s in held if s["trade_date"] == view.day]
    if view.expiry:
        held = [s for s in held if s["expiry"] == view.expiry]
    if not held:
        reason = (
            "Nothing recorded for this yet. Open its chart with Writer Flow on to start recording it, or list it in OI_FLOW_UNDERLYINGS."
            if recorder_enabled()
            else "The Writer Flow recorder is off. Set OI_FLOW_RECORDER_ENABLED=true in .env and restart OpenAlgo."
        )
        return {"status": "empty", "message": reason, "recorder_enabled": recorder_enabled()}
    chosen = held[0]
    out = analyse(
        load_day(view.underlying, chosen["expiry"], chosen["trade_date"], pause=_pause),
        pause=_pause,
        settings=FlowSettings(
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
        "oi_unit": out.get("oi_unit", "quantity"),
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
    payload = flow_payload(view)
    body = json.dumps(payload, separators=(",", ":")).encode()
    return Built(
        generation=gen,
        status=200,
        body=body,
        gzipped=gzip.compress(body, 6),
        empty=payload.get("status") == "empty",
    )


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
        built = _on_builder(_build, view, gen)
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
