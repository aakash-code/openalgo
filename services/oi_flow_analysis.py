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
    quote quantity, so every reading is a whole number of lots (NIFTY: a
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
    # Read a side by its premium move net of what the underlying's own move
    # explains (delta times the underlying's change), not the raw premium.
    # On 3-Nov-2026 the raw reading disagreed with the net one on 30% of
    # NIFTY's moves and 38% of BANKNIFTY's: on a rally every call premium
    # rises, so falling call OI read as writers covering when the call's
    # implied volatility fell and buyers were the ones leaving. Where delta
    # cannot be known (no spot, no solvable volatility, the last minutes of
    # an expiry) the raw premium is used, so nothing reads worse than before.
    net_of_underlying: bool = True
    # Strikes either side of the at-the-money one that the trend reads.
    trend_strikes: int = 6


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
    # The premium move over the run net of the underlying's, when reading net.
    net_move: float | None = None

    @property
    def cls(self) -> str:
        # A premium unknown at either end says nothing about who acted.
        if self.start_ltp is None or self.last_ltp is None:
            return ADDING if self.up else REDUCING
        if self.net_move is not None:
            return direction_class(self.up, self.net_move)
        return direction_class(self.up, self.last_ltp - self.start_ltp)


# What each reading means for the underlying. Call writers covering and put
# writers adding are the two halves of a rally, which is what a trader looks
# for first, so the screens colour by this rather than by class.
BULLISH = {("CE", COVERING), ("CE", BUYING), ("PE", WRITING), ("PE", UNWINDING)}
BEARISH = {("CE", WRITING), ("CE", UNWINDING), ("PE", COVERING), ("PE", BUYING)}


# Option pricing, only to learn delta. Black-Scholes on the spot with a flat
# rate: the reading needs the sign of the premium's move beyond what the
# underlying explains, and that sign is not sensitive to the rate.
RATE = 0.065
YEAR_SECONDS = 365.0 * 86400.0
# Expiry close by exchange: index and stock options at 15:30 IST, MCX at 23:30.
EXPIRY_CLOSE = {"MCX": (23, 30)}
DEFAULT_EXPIRY_CLOSE = (15, 30)
# Under this many minutes to expiry, delta swings too fast to trust.
MIN_MINUTES_TO_EXPIRY = 20


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _black(
    forward: float, strike: float, years: float, vol: float, side: str
) -> tuple[float, float, float]:
    """Black-76 on the forward: price, delta to the underlying, and vega.

    Index options here are priced off the future, not the spot: on
    4-Nov-2026 the NIFTY forward implied by the at-the-money call and put sat
    17 points below spot. Priced off spot with a rate instead, the same
    strike's call and put gave 15.4 and 18.9 volatility where Sensibull
    showed one 17.0.
    """
    sq = math.sqrt(years)
    d1 = (math.log(forward / strike) + 0.5 * vol * vol * years) / (vol * sq)
    d2 = d1 - vol * sq
    disc = math.exp(-RATE * years)
    vega = disc * forward * math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi) * sq
    if side == "CE":
        return disc * (forward * _ncdf(d1) - strike * _ncdf(d2)), disc * _ncdf(d1), vega
    return disc * (strike * _ncdf(-d2) - forward * _ncdf(-d1)), -disc * _ncdf(-d1), vega


def implied_forward(spot: float, strike: float, call: float, put: float, years: float) -> float:
    """The forward put-call parity gives from one strike's call and put."""
    return strike + (call - put) * math.exp(RATE * years) if years > 0 else spot


def option_delta(
    premium: float | None,
    forward: float | None,
    strike: float,
    years: float,
    side: str,
    seed: float = 0.2,
) -> tuple[float | None, float | None]:
    """(delta, implied volatility) from a premium, or (None, None) when it cannot be solved."""
    if not premium or not forward or forward <= 0 or strike <= 0 or years <= 0:
        return None, None
    disc = math.exp(-RATE * years)
    intrinsic = disc * (max(forward - strike, 0.0) if side == "CE" else max(strike - forward, 0.0))
    if premium <= intrinsic + 1e-9:
        return None, None
    vol = min(max(seed, 0.01), 3.0)
    for _ in range(12):
        price, _d, vega = _black(forward, strike, years, vol, side)
        diff = price - premium
        if abs(diff) < 1e-4:
            break
        if vega < 1e-8:
            return None, None
        vol -= diff / vega
        if not 0.005 < vol < 5.0:
            break
    if not 0.005 < vol < 5.0 or abs(
        _black(forward, strike, years, vol, side)[0] - premium
    ) > 0.01 * max(premium, 1):
        lo, hi = 0.005, 5.0
        for _ in range(50):
            mid = (lo + hi) / 2
            if _black(forward, strike, years, mid, side)[0] > premium:
                hi = mid
            else:
                lo = mid
        vol = (lo + hi) / 2
        if abs(_black(forward, strike, years, vol, side)[0] - premium) > 0.02 * max(premium, 1):
            return None, None
    return _black(forward, strike, years, vol, side)[1], vol


def expiry_close(expiry: str, exchange: str | None) -> datetime | None:
    """When an expiry like '03NOV26' closes, in the samples' own clock (IST, naive)."""
    try:
        day = datetime.strptime(str(expiry), "%d%b%y")
    except (TypeError, ValueError):
        return None
    hour, minute = EXPIRY_CLOSE.get(str(exchange or "").upper(), DEFAULT_EXPIRY_CLOSE)
    return day.replace(hour=hour, minute=minute)


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
            "trend": None,
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
    # The open, read from the first sample time itself rather than the first
    # step: a step keeps the LAST reading of its window, so at three minutes
    # "the open" was 09:17, and the open's put-call ratio and price, and the
    # verdict with them, changed with the step (4-Nov-2026: 0.73 at one
    # minute, 0.61 at three).
    first_time: datetime | None = None
    opening_oi: dict[tuple[float, str], int] = {}
    opening_spot: float | None = None
    for n, s in enumerate(sorted(samples, key=lambda r: r["sample_time"])):
        if pause is not None and n % PAUSE_EVERY == PAUSE_EVERY - 1:
            pause()
        key = (float(s["strike"]), s["side"])
        oi = reading_oi(s.get("oi"), key in held)
        t = _step_start(s["sample_time"], step)
        spot = _number(s.get("spot"))
        if spot and spot > 0:
            spot_at[t] = spot
        if first_time is None:
            first_time = s["sample_time"]
        if s["sample_time"] == first_time:
            if spot and spot > 0 and opening_spot is None:
                opening_spot = spot
            if oi is not None:
                opening_oi[key] = oi
        if oi is None:
            continue
        if oi > 0:
            held.add(key)
        last[key][t] = {**s, "oi": oi, "ltp": reading_ltp(s.get("ltp"))}
    steps = sorted({t for series in last.values() for t in series})
    index = {t: i for i, t in enumerate(steps)}

    # The forward at each step, as put-call parity gives it at the strike
    # nearest the price, kept as its gap to spot so a step without a pair
    # carries the last one forward. Delta is taken on the forward.
    basis_at: dict[datetime, float] = {}
    if settings.net_of_underlying:
        by_step: dict[datetime, dict[float, dict[str, dict]]] = defaultdict(
            lambda: defaultdict(dict)
        )
        for (k, sd), series in last.items():
            for t, smp in series.items():
                by_step[t][k][sd] = smp
        basis = 0.0
        for t in steps:
            spot_t = spot_at.get(t)
            pairs = by_step.get(t, {})
            if spot_t:
                both = [
                    k
                    for k, v in pairs.items()
                    if "CE" in v and "PE" in v and v["CE"]["ltp"] and v["PE"]["ltp"]
                ]
                if both:
                    k = min(both, key=lambda x: abs(x - spot_t))
                    smp = pairs[k]["CE"]
                    close_t = expiry_close(smp.get("expiry"), smp.get("exchange"))
                    if close_t is not None:
                        years = (close_t - t).total_seconds() / YEAR_SECONDS
                        if years > 0:
                            fwd = implied_forward(
                                spot_t, k, pairs[k]["CE"]["ltp"], pairs[k]["PE"]["ltp"], years
                            )
                            # A forward far from spot is a stale quote, not a basis.
                            if abs(fwd / spot_t - 1.0) < 0.03:
                                basis = fwd - spot_t
            basis_at[t] = basis

    cells: list[dict] = []
    totals = {t: {"ce_change": 0, "pe_change": 0} for t in steps}
    strikes: dict[float, dict] = {}
    events: list[dict] = []

    in_lots = in_quantity = 0
    for (strike, side), series in sorted(last.items()):
        if pause is not None:
            pause()
        lot = oi_lot(list(series.values()))
        if lot == 1 and int(next(iter(series.values())).get("lot_size") or 1) > 1:
            in_lots += 1
        else:
            in_quantity += 1
        close = expiry_close(
            next(iter(series.values())).get("expiry"), next(iter(series.values())).get("exchange")
        )
        seed = 0.2

        def delta_of(sample: dict, close=close, strike=strike, side=side) -> float | None:
            nonlocal seed
            if not settings.net_of_underlying or close is None:
                return None
            left = (close - sample["sample_time"]).total_seconds()
            if left < MIN_MINUTES_TO_EXPIRY * 60:
                return None
            spot_s = _number(sample.get("spot"))
            forward = (
                spot_s + basis_at.get(_step_start(sample["sample_time"], step), 0.0)
                if spot_s
                else None
            )
            d, vol = option_delta(sample["ltp"], forward, strike, left / YEAR_SECONDS, side, seed)
            if vol is not None:
                seed = vol
            return d

        prev: dict | None = None
        prev_delta: float | None = None
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
                prev_delta = delta_of(s)
                continue
            prev_oi = prev["oi"]
            prev_ltp = prev["ltp"]
            d_oi = oi - prev_oi
            # An unknown premium at either end is no premium move: side unclear.
            d_ltp = ltp - prev_ltp if ltp is not None and prev_ltp is not None else 0.0
            # Net of the underlying: what is left of the premium's move once
            # delta times the underlying's move is taken out.
            d_read = d_ltp
            spot_now, spot_before = _number(s.get("spot")), _number(prev.get("spot"))
            if (
                settings.net_of_underlying
                and prev_delta is not None
                and spot_now
                and spot_before
                and ltp is not None
                and prev_ltp is not None
            ):
                d_read = d_ltp - prev_delta * (spot_now - spot_before)
            cls = classify(d_oi, d_read, prev_oi, lot, settings)
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
                    if run.net_move is not None:
                        run.net_move += d_read
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
                    if run.net_move is not None:
                        run.net_move += d_read
                    if ltp is not None:
                        run.last_ltp = ltp
                else:
                    run = _Run(
                        up=up,
                        start_index=i,
                        start_ltp=prev_ltp,
                        last_ltp=ltp,
                        oi_change=d_oi,
                        net_move=d_read if settings.net_of_underlying else None,
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
            prev_delta = delta_of(s)

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
    trend = trend_read(
        cells, steps, spot_at, settings, step_minutes, last, opening_oi, opening_spot
    )
    return {
        "trend": trend,
        "step_seconds": step,
        # How the open interest numbers are quoted: quantity (NFO, BFO) or
        # lots (MCX), read from the data by oi_lot, so the screens can say which.
        # Never "contracts": the exchange counts one lot as one contract.
        "oi_unit": "lots" if in_lots > in_quantity else "quantity",
        "steps": steps,
        "spot": spot,
        "cells": cells,
        "totals": [{"time": t, "spot": spot_at.get(t), **totals[t]} for t in steps],
        "strikes": rows,
        "events": sorted(events, key=lambda e: (e["time"], e["strike"], e["side"])),
    }


# How far the near-the-money pressure must lean, as a share of all of it, to
# call a direction. Below this the day reads as sideways.
TREND_LEAN = 0.15
# Minutes the "recent" reading looks back over.
TREND_RECENT_MINUTES = 15
# Under this many minutes recorded, the read is shown as early: on
# 4-Nov-2026 forty minutes in, a few steps of opening build-up decided the
# lean at a five minute step, and it read the opposite of one minute.
TREND_EARLY_MINUTES = 15


def _gap(strikes: list[float]) -> float:
    gaps = sorted(b - a for a, b in zip(strikes, strikes[1:], strict=False) if b > a)
    return gaps[len(gaps) // 2] if gaps else 0.0


def trend_read(
    cells: list[dict],
    steps: list[datetime],
    spot_at: dict[datetime, float],
    settings: FlowSettings,
    step_minutes: float,
    readings: dict[tuple[float, str], dict[datetime, dict]] | None = None,
    opening_oi: dict[tuple[float, str], int] | None = None,
    opening_spot: float | None = None,
) -> dict | None:
    """Which way writers are leaning, step by step from the first one.

    Each step adds up the open interest that moved within `trend_strikes` of
    the money, signed by what it means for the underlying (bias): call
    writers covering and put writers adding push it up, the reverse down.
    Summed from the open, that is the day's lean; its share of all the
    interest that moved is its strength. Alongside: the put-call ratio of the
    open interest near the money, and the strikes holding the most call open
    interest above the price (resistance) and put open interest below it
    (support), so a wall that moves shows up as it moves.
    """
    if not steps:
        return None
    strike_list = sorted({c["strike"] for c in cells})
    gap = _gap(strike_list)
    by_time: dict[datetime, list[dict]] = defaultdict(list)
    for c in cells:
        by_time[c["time"]].append(c)

    oi_now: dict[tuple[float, str], int] = {}
    pressure: list[float] = []
    cumulative: list[float] = []
    pcr: list[float | None] = []
    support: list[float | None] = []
    resistance: list[float | None] = []
    total = 0.0
    spot = None
    # Every contract's open interest at each step, carried forward, from all
    # its readings: the cells start one step after a contract's first reading,
    # so read from them alone the open's ratio and walls missed most strikes.
    at_step: dict[datetime, list[tuple[tuple[float, str], int]]] = defaultdict(list)
    for key, series in (readings or {}).items():
        for t, sample in series.items():
            at_step[t].append((key, sample["oi"]))
    for t in steps:
        spot = spot_at.get(t, spot)
        step_sum = 0.0
        for key, oi in at_step.get(t, ()):
            oi_now[key] = oi
        for c in by_time.get(t, ()):
            oi_now[(c["strike"], c["side"])] = c["oi"]
            if spot is None or gap <= 0:
                continue
            if abs(c["strike"] - spot) <= settings.trend_strikes * gap + 1e-9:
                step_sum += bias(c["side"], c["cls"]) * abs(c["oi_change"])
        total += step_sum
        pressure.append(step_sum)
        cumulative.append(total)
        if spot is None or gap <= 0:
            pcr.append(None)
            support.append(None)
            resistance.append(None)
            continue
        near = settings.trend_strikes * gap + 1e-9
        ce = sum(v for (k, sd), v in oi_now.items() if sd == "CE" and abs(k - spot) <= near)
        pe = sum(v for (k, sd), v in oi_now.items() if sd == "PE" and abs(k - spot) <= near)
        pcr.append(round(pe / ce, 3) if ce > 0 else None)
        above = [(v, k) for (k, sd), v in oi_now.items() if sd == "CE" and k >= spot]
        below = [(v, k) for (k, sd), v in oi_now.items() if sd == "PE" and k <= spot]
        resistance.append(max(above)[1] if above else None)
        support.append(max(below)[1] if below else None)

    moved = sum(abs(p) for p in pressure)
    strength = (total / moved) if moved > 0 else 0.0
    back = max(1, int(round(TREND_RECENT_MINUTES / step_minutes)))
    recent_steps = pressure[-back:]
    recent_moved = sum(abs(p) for p in recent_steps)
    recent = (sum(recent_steps) / recent_moved) if recent_moved > 0 else 0.0

    if strength >= TREND_LEAN:
        direction = 1
    elif strength <= -TREND_LEAN:
        direction = -1
    else:
        direction = 0
    # Turning: the last quarter hour leans clearly against the day.
    turning = direction != 0 and recent * direction <= -0.3

    # Since when the day's lean has held its sign.
    since_index = 0
    sign = 1 if total > 0 else -1 if total < 0 else 0
    for i in range(len(cumulative) - 1, -1, -1):
        v = cumulative[i]
        if (v > 0 and sign < 0) or (v < 0 and sign > 0) or v == 0:
            since_index = min(i + 1, len(cumulative) - 1)
            break

    def first_known(values):
        return next((v for v in values if v is not None), None)

    def last_known(values):
        return next((v for v in reversed(values) if v is not None), None)

    # Three readings that do not restate the price, each +1, 0 or -1:
    # writers' lean, the put-call ratio since the open (puts written faster
    # than calls supports the underlying), and the walls (support and
    # resistance strikes moving up is the market lifting its floor and ceiling).
    def snapshot(oi_by_key: dict[tuple[float, str], int], ref: float | None):
        if not oi_by_key or ref is None or gap <= 0:
            return None, None, None
        near = settings.trend_strikes * gap + 1e-9
        ce = sum(v for (k, sd), v in oi_by_key.items() if sd == "CE" and abs(k - ref) <= near)
        pe = sum(v for (k, sd), v in oi_by_key.items() if sd == "PE" and abs(k - ref) <= near)
        above = [(v, k) for (k, sd), v in oi_by_key.items() if sd == "CE" and k >= ref]
        below = [(v, k) for (k, sd), v in oi_by_key.items() if sd == "PE" and k <= ref]
        return (
            round(pe / ce, 3) if ce > 0 else None,
            max(below)[1] if below else None,
            max(above)[1] if above else None,
        )

    open_pcr, open_support, open_resistance = snapshot(opening_oi or {}, opening_spot)
    pcr_open = open_pcr if open_pcr is not None else first_known(pcr)
    pcr_now = last_known(pcr)
    pcr_vote = 0
    if pcr_open and pcr_now:
        change = (pcr_now - pcr_open) / pcr_open
        pcr_vote = 1 if change >= 0.05 else -1 if change <= -0.05 else 0
    s_open = open_support if open_support is not None else first_known(support)
    r_open = open_resistance if open_resistance is not None else first_known(resistance)
    s_now, r_now = last_known(support), last_known(resistance)
    wall_shift = 0
    if s_open is not None and s_now is not None:
        wall_shift += (s_now > s_open) - (s_now < s_open)
    if r_open is not None and r_now is not None:
        wall_shift += (r_now > r_open) - (r_now < r_open)
    wall_vote = 1 if wall_shift > 0 else -1 if wall_shift < 0 else 0
    score = direction + pcr_vote + wall_vote
    overall = 1 if score >= 2 else -1 if score <= -2 else 0

    # Price since the first step, for the reader to compare with.
    spots = [spot_at[t] for t in steps if spot_at.get(t)]
    base = opening_spot or (spots[0] if spots else None)
    price_change = (spots[-1] / base - 1.0) if spots and base else None

    minutes = (steps[-1] - steps[0]).total_seconds() / 60.0 + step_minutes
    return {
        # When each value below was read, so a reader on another step can
        # place them, and how much of the day they cover.
        "times": list(steps),
        "minutes": round(minutes, 1),
        "early": minutes < TREND_EARLY_MINUTES,
        "overall": overall,
        "score": score,
        "votes": {"writers": direction, "pcr": pcr_vote, "walls": wall_vote},
        "price_change": round(price_change, 5) if price_change is not None else None,
        "pressure": pressure,
        "cumulative": cumulative,
        "pcr": pcr,
        "support": support,
        "resistance": resistance,
        "direction": direction,
        "strength": round(strength, 3),
        "recent": round(recent, 3),
        "turning": turning,
        "since": steps[since_index],
        "pcr_open": pcr_open,
        "pcr_now": pcr_now,
        "support_open": s_open,
        "support_now": s_now,
        "resistance_open": r_open,
        "resistance_now": r_now,
        "strikes_read": settings.trend_strikes,
    }
