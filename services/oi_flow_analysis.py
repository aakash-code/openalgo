"""
Writer Flow analysis: what option writers did at each strike, step by step.

Pure: no database, no broker, no clock, no logging. The recorded samples come
in as an argument and every reading leaves as the return value, the same rule
services/risk/ follows, so the page, a /trading panel and an alert can share
one implementation and the tests need nothing running.

Reading a step. Open interest alone cannot tell a writer from a buyer, so a
strike side (call or put) is classed by its OI and its premium together:

    OI up,   premium down  -> writing    (writers adding: short build-up)
    OI down, premium up    -> covering   (writers exiting: short covering)
    OI up,   premium up    -> buying     (long build-up)
    OI down, premium down  -> unwinding  (long unwinding)
    OI up or down, premium unchanged -> adding / reducing (side unclear)
    a change below the noise floor   -> steady

A streak is a run of one OI direction, and its class is read from the
premium across the whole run rather than from each step: on 16-Oct-2026 the
23000 CE lost 95L of OI through a rally while its premium flickered up and
down minute to minute, so a class read per step alternated covering and
unwinding and no streak ever reached ten minutes. Each step's own class still
goes to the heatmap. A single quiet step does not end a run, because exchange
OI arrives in jumps: on 16-Oct-2026 NIFTY's ATM options changed OI in
80-85% of minutes, SENSEX's in 37% and CRUDEOIL's in about 24%, so a quiet
minute on SENSEX is usually the wait for the next publish, not writers
holding. The step itself is chosen from the same cadence unless set.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

WRITING = "writing"
COVERING = "covering"
BUYING = "buying"
UNWINDING = "unwinding"
ADDING = "adding"
REDUCING = "reducing"
STEADY = "steady"

# The two classes that say what writers are doing; events are raised for these.
WRITER_CLASSES = (WRITING, COVERING)

# Premiums sit on their contract's tick grid (0.05 on NFO and BFO, 0.10 on
# CRUDEOIL options, and so on), so two prices are either equal or at least one
# tick apart. This only absorbs floating-point noise, which keeps the reading
# right for every exchange without knowing any tick size.
PRICE_EPSILON = 1e-6
# Rows between calls to analyse()'s pause hook: about 3 ms of work.
PAUSE_EVERY = 400


# What a sample may be trusted for. A wrong number on screen is worse than a
# missing one, so anything that is not a reading is treated as no reading:
#
# - Open interest that is missing or not a finite number.
# - Open interest of 0 after the contract has held some. The option chain
#   reports 0 for a contract whose quote did not come back, and every writer
#   leaving a strike within one step does not happen in a session; read as a
#   reading, one dropped quote drew the strike's whole OI as covered and then
#   written back a minute later. A 0 before the contract has ever held OI is
#   real: nobody has traded it yet (16-Oct-2026, deep in-the-money SENSEX puts).
# - A premium that is missing, not finite, or 0. 0 is a contract that has not
#   traded yet, not a price: read as one, its first trade looked like the
#   premium leaping from nothing, and the side was misread.


def _number(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def reading_oi(value, held_before: bool) -> int | None:
    """The open interest in a sample, or None when it is not a reading."""
    f = _number(value)
    if f is None or f < 0 or (f == 0 and held_before):
        return None
    return int(f)


def oi_lot(series: list[dict]) -> int:
    """What one lot is in this contract's open interest numbers: lot size, or 1.

    Exchanges and brokers quote open interest in different units. NFO and BFO
    quote contracts, so every reading is a whole number of lots (NIFTY: a
    multiple of 65). MCX quotes lots: CRUDEOIL 9000 PE held 6,094 on
    16-Oct-2026 with a lot of 100. A noise floor of "five lots" multiplied by
    the lot size was 500 there, larger than nearly every real move (median 2
    lots), and every MCX minute read steady. Read from the data rather than
    from a table of exchanges, so a broker that quotes differently is right too.
    """
    lot = max(int(series[0].get("lot_size") or 1), 1) if series else 1
    if lot == 1:
        return 1
    held = [s["oi"] for s in series if s["oi"]]
    if not held:
        return lot
    return lot if all(v % lot == 0 for v in held) else 1


def reading_ltp(value) -> float | None:
    """The premium in a sample, or None when it is not a price (missing, 0, not finite)."""
    f = _number(value)
    return f if f is not None and f > 0 else None


@dataclass(frozen=True)
class FlowSettings:
    """How the day is read. Every field is a setting the trader can change."""

    # Seconds per step; None chooses 60 or 180 from how often OI changed today.
    step_seconds: int | None = None
    # A change smaller than both of these is noise: a percent of what the strike
    # side held before the step, and a number of lots.
    # 0.5%, not 2%: on 16-Oct-2026 a 2% floor read 63% of the minutes near
    # NIFTY's ATM as steady while its OI actually changed in 80-85% of them,
    # and the heatmap came out mostly grey. 0.5% read 30% as steady and still
    # ignores less than 50K contracts on a strike holding a crore.
    noise_pct: float = 0.5
    noise_lots: int = 5
    # Minutes without a meaningful move that end a streak. A run otherwise
    # lasts until OI moves against it by more than the noise floor: on
    # 16-Oct-2026 the 23000 CE covered for half an hour in chunks
    # (C.C.C..u.C.C.C), and ending a run on two quiet minutes split it into
    # pieces no longer than four minutes.
    quiet_minutes_to_break: float = 5.0
    # Streak lengths, in minutes, at which an event is raised.
    event_minutes: tuple[int, ...] = (10, 15, 20)
    # Windows, in minutes, the net change is reported over.
    windows: tuple[int, ...] = (3, 10, 15, 20)


@dataclass
class _Run:
    """A run of one OI direction. Its class is read from the premium across
    the whole run, not from any one step."""

    up: bool
    start_index: int
    start_ltp: float | None
    last_ltp: float | None
    oi_change: int = 0
    quiet_minutes: float = 0.0
    raised: set[int] = field(default_factory=set)

    @property
    def cls(self) -> str:
        # A premium unknown at either end says nothing about who acted.
        if self.start_ltp is None or self.last_ltp is None:
            return ADDING if self.up else REDUCING
        return direction_class(self.up, self.last_ltp - self.start_ltp)


# What each reading means for the underlying. Call writers covering and put
# writers adding are the two halves of a rally, which is what a trader looks
# for first, so the screens colour by this rather than by class.
BULLISH = {("CE", COVERING), ("CE", BUYING), ("PE", WRITING), ("PE", UNWINDING)}
BEARISH = {("CE", WRITING), ("CE", UNWINDING), ("PE", COVERING), ("PE", BUYING)}


def bias(side: str, cls: str) -> int:
    """+1 bullish, -1 bearish, 0 neither (steady, or the side is unclear)."""
    if (side, cls) in BULLISH:
        return 1
    if (side, cls) in BEARISH:
        return -1
    return 0


def choose_step_seconds(samples: list[dict]) -> int:
    """60 when OI changes about every minute, 180 when it arrives in slower jumps.

    The median gap between successive OI changes, across every contract. On
    16-Oct-2026 that was 60s for NIFTY and 180s for SENSEX and CRUDEOIL.
    """
    by_symbol: dict[str, list[tuple[datetime, int]]] = defaultdict(list)
    for s in sorted(samples, key=lambda r: r["sample_time"]):
        series = by_symbol[s["symbol"]]
        oi = reading_oi(s.get("oi"), any(v > 0 for _, v in series[-1:]))
        if oi is not None:
            series.append((s["sample_time"], oi))
    gaps: list[float] = []
    for series in by_symbol.values():
        changed = [series[i][0] for i in range(1, len(series)) if series[i][1] != series[i - 1][1]]
        gaps += [(b - a).total_seconds() for a, b in zip(changed, changed[1:], strict=False)]
    if not gaps:
        return 180
    return 60 if statistics.median(gaps) <= 90 else 180


def _step_start(ts: datetime, step: int) -> datetime:
    midnight = ts.replace(hour=0, minute=0, second=0, microsecond=0)
    seconds = int((ts - midnight).total_seconds())
    return midnight + timedelta(seconds=seconds - seconds % step)


def direction_class(up: bool, ltp_change: float) -> str:
    """The class of an OI move in one direction, given the premium over it."""
    if ltp_change > PRICE_EPSILON:
        return BUYING if up else COVERING
    if ltp_change < -PRICE_EPSILON:
        return WRITING if up else UNWINDING
    return ADDING if up else REDUCING


def classify(
    oi_change: int, ltp_change: float, prev_oi: int, lot_size: int, settings: FlowSettings
) -> str:
    """One step's class for one strike side."""
    floor = max(
        settings.noise_pct / 100.0 * max(prev_oi, 0), settings.noise_lots * max(lot_size, 1)
    )
    if abs(oi_change) < floor or oi_change == 0:
        return STEADY
    return direction_class(oi_change > 0, ltp_change)


def analyse(
    samples: list[dict],
    settings: FlowSettings | None = None,
    pause: Callable[[], None] | None = None,
) -> dict:
    """Read a day of samples for one underlying and expiry.

    `pause`, if given, is called every PAUSE_EVERY rows and every strike side.
    The Writer Flow builder passes a 1 ms sleep, so a request thread waiting
    for the interpreter gets in at once instead of queueing behind a whole
    day's analysis (measured: request-shaped work went from 0.5 ms to 15 ms
    while a build ran). It has no say in the result.

    `samples` are rows as database.oi_flow_db stores them: sample_time, strike,
    side ('CE'/'PE'), symbol, oi, ltp, spot, lot_size.

    Returns step_seconds, steps (step start times), cells (one per strike side
    per step, for a heatmap), totals (call and put change per step, for the
    aggregate chart), strikes (each side's current state) and events.
    """
    settings = settings or FlowSettings()
    if not samples:
        return {
            "step_seconds": settings.step_seconds or 180,
            "steps": [],
            "cells": [],
            "totals": [],
            "strikes": [],
            "events": [],
        }
    step = settings.step_seconds or choose_step_seconds(samples)
    step_minutes = step / 60.0
    # A contract unseen for longer than this starts over from its next reading
    # instead of being compared with the last one. A strike that left the
    # window at 10:00 and came back at 13:00 otherwise drew three hours of
    # change as one step's move, and began a streak on it.
    max_gap_steps = max(2, math.ceil(settings.quiet_minutes_to_break / step_minutes))

    # The last usable sample of each step, per contract. Unusable samples are
    # dropped before the step is chosen, so a dropout landing last in a step
    # does not hide a good reading from earlier in it.
    last: dict[tuple[float, str], dict[datetime, dict]] = defaultdict(dict)
    held: set[tuple[float, str]] = set()
    spot_at: dict[datetime, float] = {}
    for n, s in enumerate(sorted(samples, key=lambda r: r["sample_time"])):
        if pause is not None and n % PAUSE_EVERY == PAUSE_EVERY - 1:
            pause()
        key = (float(s["strike"]), s["side"])
        oi = reading_oi(s.get("oi"), key in held)
        t = _step_start(s["sample_time"], step)
        spot = _number(s.get("spot"))
        if spot and spot > 0:
            spot_at[t] = spot
        if oi is None:
            continue
        if oi > 0:
            held.add(key)
        last[key][t] = {**s, "oi": oi, "ltp": reading_ltp(s.get("ltp"))}
    steps = sorted({t for series in last.values() for t in series})
    index = {t: i for i, t in enumerate(steps)}

    cells: list[dict] = []
    totals = {t: {"ce_change": 0, "pe_change": 0} for t in steps}
    strikes: dict[float, dict] = {}
    events: list[dict] = []

    in_lots = in_contracts = 0
    for (strike, side), series in sorted(last.items()):
        if pause is not None:
            pause()
        lot = oi_lot(list(series.values()))
        if lot == 1 and int(next(iter(series.values())).get("lot_size") or 1) > 1:
            in_lots += 1
        else:
            in_contracts += 1
        prev: dict | None = None
        prev_index = -1
        run: _Run | None = None
        last_move_index: int | None = None
        oi_at: dict[int, int] = {}
        last_seen: datetime | None = None
        for t in steps:
            s = series.get(t)
            if s is None:
                continue
            i = index[t]
            oi = s["oi"]
            ltp = s["ltp"]
            oi_at[i] = oi
            last_seen = s["sample_time"]
            if prev is None or i - prev_index > max_gap_steps:
                # First reading, or back after too long away: a baseline, not a move.
                prev, prev_index, run = s, i, None
                continue
            prev_oi = prev["oi"]
            prev_ltp = prev["ltp"]
            d_oi = oi - prev_oi
            # An unknown premium at either end is no premium move: side unclear.
            d_ltp = ltp - prev_ltp if ltp is not None and prev_ltp is not None else 0.0
            cls = classify(d_oi, d_ltp, prev_oi, lot, settings)
            cells.append(
                {
                    "time": t,
                    "strike": strike,
                    "side": side,
                    "cls": cls,
                    "oi_change": d_oi,
                    "ltp_change": round(d_ltp, 2),
                    "oi": oi,
                }
            )
            totals[t]["ce_change" if side == "CE" else "pe_change"] += d_oi

            if cls == STEADY:
                if run is not None:
                    # A move too small to count still belongs to the run.
                    run.quiet_minutes += step_minutes
                    run.oi_change += d_oi
                    if ltp is not None:
                        run.last_ltp = ltp
                    if run.quiet_minutes >= settings.quiet_minutes_to_break:
                        run = None
            else:
                last_move_index = i
                up = d_oi > 0
                if run is not None and run.up == up:
                    run.quiet_minutes = 0.0
                    run.oi_change += d_oi
                    if ltp is not None:
                        run.last_ltp = ltp
                else:
                    run = _Run(
                        up=up, start_index=i, start_ltp=prev_ltp, last_ltp=ltp, oi_change=d_oi
                    )
                if run.cls in WRITER_CLASSES:
                    minutes = (i - run.start_index + 1) * step_minutes
                    for mark in settings.event_minutes:
                        if minutes >= mark and mark not in run.raised:
                            run.raised.add(mark)
                            events.append(
                                {
                                    "time": t,
                                    "strike": strike,
                                    "side": side,
                                    "kind": run.cls,
                                    "minutes": mark,
                                    "oi_change": run.oi_change,
                                }
                            )
            prev, prev_index = s, i

        end = len(steps) - 1
        if run is not None:
            # A run still alive counts to the last step, quiet tail included.
            state_cls = run.cls
            streak_minutes = (end - run.start_index + 1) * step_minutes
            streak_change = run.oi_change
        else:
            state_cls = STEADY
            streak_minutes = 0.0
            streak_change = 0
        steady_minutes = (
            (end - last_move_index) * step_minutes
            if last_move_index is not None
            else (end * step_minutes)
        )
        latest_i = max(oi_at) if oi_at else None
        latest_oi = oi_at[latest_i] if latest_i is not None else 0
        windows = {}
        for w in settings.windows:
            back = int(round(w / step_minutes))
            earlier = [k for k in oi_at if k <= end - back]
            windows[w] = latest_oi - oi_at[max(earlier)] if earlier else None
        strikes.setdefault(strike, {"strike": strike})[side.lower()] = {
            "cls": state_cls,
            "streak_minutes": streak_minutes,
            "streak_oi_change": streak_change,
            "steady_minutes": steady_minutes if state_cls == STEADY else 0.0,
            "oi": latest_oi,
            "window_change": windows,
            # Sampled at the last step. A strike that drifted out of the
            # recorder's window keeps its history but its present is unknown,
            # which is not the same as steady.
            "observed": latest_i == end,
            "last_seen": last_seen,
        }

    # The latest price known at the last step. An index stops trading at 15:30
    # while its options trade to 15:40, so the last minutes carry no price of
    # their own; without this the ATM strike went unmarked at every close.
    known = [t for t in spot_at if steps and t <= steps[-1]]
    spot = spot_at[max(known)] if known else None
    rows = [strikes[k] for k in sorted(strikes)]
    for r in rows:
        r["observed"] = any(r.get(side, {}).get("observed") for side in ("ce", "pe"))
    if spot is not None and rows:
        atm = min(rows, key=lambda r: abs(r["strike"] - spot))["strike"]
        for r in rows:
            r["atm"] = r["strike"] == atm
    return {
        "step_seconds": step,
        # How the open interest numbers are quoted: contracts (NFO, BFO) or
        # lots (MCX), read from the data by oi_lot, so the screens can say which.
        "oi_unit": "lots" if in_lots > in_contracts else "contracts",
        "steps": steps,
        "spot": spot,
        "cells": cells,
        "totals": [{"time": t, "spot": spot_at.get(t), **totals[t]} for t in steps],
        "strikes": rows,
        "events": sorted(events, key=lambda e: (e["time"], e["strike"], e["side"])),
    }
