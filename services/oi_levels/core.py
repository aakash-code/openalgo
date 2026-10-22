"""Support and resistance zones from one day's recorded option open interest. No I/O.

Recomputed from the whole day on every call, minute by minute, and each minute
uses only what was recorded by then: the same inputs always give the same zones,
and a minute never changes once the next one arrives.

* A zone is a run of neighbouring strikes holding at least SIGNIFICANT_SHARE of
  the biggest open interest on their side: puts at or below the price
  (support), calls at or above it (resistance). The zone's anchor is its
  biggest strike.
* Zones move with the data, but a new anchor takes over only after leading the
  old one by LEAD_SHARE for HOLD_MINUTES, so open interest trading places
  between two neighbours does not read as the level moving. On NIFTY calls on
  16-Oct-2026 the raw biggest strike changed 36 times; this rule left 13.
* A zone is broken once the price has stayed beyond it for HOLD_MINUTES, and
  turned when the market read saw its writers leave and the other side write
  there (the same rule, so the chart and the panel never disagree).
* Gamma: where option gamma concentrates and the price at which the net
  exposure changes sign. That is where price tends to stick, not a barrier.
* Every touch of a zone, and of every round strike as the plain baseline, is
  followed for AFTER_MINUTES: did price bounce BOUNCE_SHARE before breaking
  BREAK_SHARE beyond it. Chance alone gives BREAK / (BOUNCE + BREAK).

Thresholds are shares of the price, so they mean the same on NIFTY, SENSEX and
CRUDEOIL: at NIFTY 25,000 a touch is within 10 points, a bounce 30, a break 20.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from services.market_read.core import (
    HOLD_MINUTES,
    LEVEL_STEPS,
    SIGNIFICANT_SHARE,
    WEAKEN_SHARE,
    _book,
    _level_events,
    _near,
)

LEAD_SHARE = 0.10  # a challenger must hold this much more open interest to take over
ZONE_STRIKES = 3  # the widest a zone may be, in strikes
CHANGE_WINDOW = 10  # minutes for "writers adding or leaving"
TOUCH_SHARE = 0.0004  # within this of a zone is a touch
BOUNCE_SHARE = 0.0012  # a touch held once price moves this far back
BREAK_SHARE = 0.0008  # and broke once it goes this far through
AFTER_MINUTES = 20  # how long a touch is followed
TOUCH_COOLDOWN = 15  # a level touched again sooner is the same test
GAMMA_EVERY = 5  # minutes between gamma reads (always on the last minute)
FLIP_POINTS = 81  # hypothetical prices tried for the gamma flip
MIN_JUDGED = 20  # touches before a hold rate means anything
CHANCE = BREAK_SHARE / (BOUNCE_SHARE + BREAK_SHARE)

SUPPORT, RESISTANCE = "support", "resistance"
_SIDE = {SUPPORT: "PE", RESISTANCE: "CE"}
_OPPOSITE = {"PE": "CE", "CE": "PE"}
_TURN_KIND = {SUPPORT: "support_to_resistance", RESISTANCE: "resistance_to_support"}


@dataclass
class LevelInputs:
    underlying: str
    samples: list[dict]  # oi_flow_db.load_day rows
    expiry_at: datetime | None = None  # the options' expiry, IST, for gamma
    expiry_day: bool = False  # open interest falls everywhere on expiry day
    # (strikes, ce_prices, pe_prices, forward, years) -> (ce, pe) lists of
    # {"iv", "gamma", ...} or None: option_greeks_service.calculate_chain_greeks.
    greeks: Callable | None = None
    # (legs, spot, low, high, points) -> price or None, legs being
    # (strike, forward, years, iv, oi, sign): strike_profile_service.find_gamma_flip.
    flip: Callable | None = None
    past_touches: list[dict] = field(default_factory=list)  # earlier days, for the hold rates


# ----------------------------------------------------------------------- zones


def _own(book, t, side) -> dict[float, float]:
    """Open interest on one side, on the side of the price that side defends."""
    legs, spot, half = book.legs[t], book.spot[t], book.step / 2
    out = {}
    for k in _near(book, t, LEVEL_STEPS):
        if (side, k) not in legs:
            continue
        if (k <= spot + half) if side == "PE" else (k >= spot - half):
            out[k] = legs[(side, k)][0]
    return out


def _clusters(ois: dict[float, float], step: float) -> list[tuple[list[float], float]]:
    """Runs of neighbouring strong strikes, biggest total first."""
    if not ois:
        return []
    biggest = max(ois.values())
    groups: list[list[float]] = []
    for k in sorted(k for k, oi in ois.items() if oi >= SIGNIFICANT_SHARE * biggest):
        if groups and k - groups[-1][-1] <= step + 1e-9 and len(groups[-1]) < ZONE_STRIKES:
            groups[-1].append(k)
        else:
            groups.append([k])
    return sorted(((g, sum(ois[k] for k in g)) for g in groups), key=lambda x: -x[1])


@dataclass
class _Track:
    anchor: float | None = None
    since: datetime | None = None
    challenger: float | None = None
    challenger_minutes: int = 0
    beyond_minutes: int = 0


def _turns(book, expiry_day) -> dict[tuple[str, float], list[datetime]]:
    """When the market read saw each strike turn, by (role it turned from, strike)."""
    events, _ = _level_events(book, expiry_day)
    out: dict[tuple[str, float], list[datetime]] = {}
    for e in events:
        for role, kind in _TURN_KIND.items():
            if e["kind"] == kind:
                strike = float(e["id"].split(":")[1].replace(",", ""))
                out.setdefault((role, strike), []).append(e["time"])
    return out


def _zone(book, t, role, anchor, own, peak, expiry_day, index) -> dict:
    side = _SIDE[role]
    legs = book.legs[t]
    pool = dict(own)
    pool.setdefault(anchor, legs.get((side, anchor), (0.0,))[0])
    group = next((g for g, _ in _clusters(pool, book.step) if anchor in g), [anchor])
    total = sum(own.values()) or 1.0
    anchor_oi = legs.get((side, anchor), (0.0,))[0]
    top = peak.get((side, anchor), anchor_oi) or 1.0
    earlier = book.legs[book.times[max(0, index - CHANGE_WINDOW)]]
    own_change = anchor_oi - earlier.get((side, anchor), (anchor_oi,))[0]
    opp_now = legs.get((_OPPOSITE[side], anchor), (0.0,))[0]
    opp_change = opp_now - earlier.get((_OPPOSITE[side], anchor), (opp_now,))[0]
    weakening = not expiry_day and anchor_oi <= (1 - WEAKEN_SHARE) * top
    return {
        "role": role,
        "anchor": anchor,
        "low": min(group),
        "high": max(group),
        "strikes": group,
        "oi": sum(pool.get(k, 0.0) for k in group),
        "size": round(sum(pool.get(k, 0.0) for k in group) / total, 3),
        "from_peak_pct": round((anchor_oi / top - 1) * 100, 1),
        "change_own": own_change,
        "change_opposite": opp_change,
        "status": "weakening" if weakening else "holding",
    }


def _levels(book, expiry_day):
    """Per minute, the primary zone on each side, plus every change of anchor."""
    turns = _turns(book, expiry_day)
    peak: dict[tuple[str, float], float] = {}
    tracks = {SUPPORT: _Track(), RESISTANCE: _Track()}
    history: dict[datetime, dict] = {}
    changes: list[dict] = []
    last_zone: dict[str, dict] = {}
    for i, t in enumerate(book.times):
        legs, spot = book.legs[t], book.spot[t]
        for key, leg in legs.items():
            peak[key] = max(peak.get(key, 0.0), leg[0])
        through = BREAK_SHARE * spot
        minute = {}
        for role, tr in tracks.items():
            side = _SIDE[role]
            own = _own(book, t, side)
            if not own:
                # A minute recorded only in part (a late chain, a refill under
                # way) says nothing new: the zone stays where it was.
                minute[role] = last_zone.get(role)
                continue
            top = max(own, key=own.get)
            if tr.anchor is None:
                tr.anchor, tr.since = top, t
            beyond = spot < tr.anchor - through if side == "PE" else spot > tr.anchor + through
            tr.beyond_minutes = tr.beyond_minutes + 1 if beyond else 0
            # A level left behind as the price moved away is no longer what is
            # near the money: the market read stops looking past LEVEL_STEPS.
            if abs(tr.anchor - spot) > LEVEL_STEPS * book.step + 1e-9 and top != tr.anchor:
                changes.append(
                    {"time": t, "role": role, "from": tr.anchor, "to": top, "kind": "moved"}
                )
                tr.anchor, tr.since = top, t
                tr.challenger, tr.challenger_minutes, tr.beyond_minutes = None, 0, 0
            elif tr.beyond_minutes >= HOLD_MINUTES:
                turned = any(tr.since <= x <= t for x in turns.get((role, tr.anchor), []))
                changes.append(
                    {
                        "time": t,
                        "role": role,
                        "from": tr.anchor,
                        "to": top,
                        "kind": "turned" if turned else "broken",
                    }
                )
                tr.anchor, tr.since = top, t
                tr.challenger, tr.challenger_minutes, tr.beyond_minutes = None, 0, 0
            elif (
                top != tr.anchor
                and own[top] >= (1 + LEAD_SHARE) * legs.get((side, tr.anchor), (0.0,))[0]
            ):
                if tr.challenger == top:
                    tr.challenger_minutes += 1
                else:
                    tr.challenger, tr.challenger_minutes = top, 1
                if tr.challenger_minutes >= HOLD_MINUTES:
                    changes.append(
                        {"time": t, "role": role, "from": tr.anchor, "to": top, "kind": "moved"}
                    )
                    tr.anchor, tr.since = top, t
                    tr.challenger, tr.challenger_minutes = None, 0
            else:
                tr.challenger, tr.challenger_minutes = None, 0
            zone = _zone(book, t, role, tr.anchor, own, peak, expiry_day, i)
            zone["since"] = tr.since
            second = next(
                (
                    (g, oi)
                    for g, oi in _clusters(own, book.step)
                    if tr.anchor not in g and not (min(g) <= tr.anchor <= max(g))
                ),
                None,
            )
            zone["next"] = (
                {"low": min(second[0]), "high": max(second[0]), "oi": second[1]} if second else None
            )
            minute[role] = zone
            last_zone[role] = zone
        history[t] = minute
    return history, changes


# ----------------------------------------------------------------------- gamma


def _forward(book, t) -> float | None:
    """The parity forward: strike plus call minus put, nearest three strikes."""
    legs, spot = book.legs[t], book.spot[t]
    both = sorted(
        (k for (s, k) in legs if s == "CE" and ("PE", k) in legs),
        key=lambda k: abs(k - spot),
    )[:3]
    values = [
        k + legs[("CE", k)][1] - legs[("PE", k)][1]
        for k in both
        if legs[("CE", k)][1] > 0 and legs[("PE", k)][1] > 0
    ]
    return statistics.median(values) if values else None


def _gamma_at(book, t, inputs) -> dict | None:
    if inputs.greeks is None or inputs.expiry_at is None:
        return None
    years = (inputs.expiry_at - t).total_seconds() / (365.0 * 86400.0)
    forward = _forward(book, t)
    if years <= 0 or not forward:
        return None
    years = max(years, 0.0001)
    legs = book.legs[t]
    strikes = [k for k in _near(book, t, LEVEL_STEPS) if ("CE", k) in legs and ("PE", k) in legs]
    if len(strikes) < 3:
        return None
    ce, pe = inputs.greeks(
        strikes,
        [legs[("CE", k)][1] for k in strikes],
        [legs[("PE", k)][1] for k in strikes],
        forward,
        years,
    )
    net, flip_legs = {}, []
    for k, c, p in zip(strikes, ce or [], pe or [], strict=False):
        c, p = c or {}, p or {}
        c_oi, p_oi = legs[("CE", k)][0], legs[("PE", k)][0]
        net[k] = ((c.get("gamma") or 0) * c_oi - (p.get("gamma") or 0) * p_oi) * forward**2 * 0.01
        for leg, oi, sign in ((c, c_oi, 1.0), (p, p_oi, -1.0)):
            if (leg.get("iv") or 0) > 0:
                flip_legs.append((k, forward, years, leg["iv"] / 100.0, oi, sign))
    if not net:
        return None
    flip = None
    if inputs.flip is not None and flip_legs:
        flip = inputs.flip(flip_legs, book.spot[t], min(strikes), max(strikes), FLIP_POINTS)
    return {
        "forward": forward,
        "top_strike": max(net, key=lambda k: abs(net[k])),
        "flip": round(flip, 1) if flip else None,
    }


def _gamma(book, inputs) -> dict[datetime, dict]:
    out, last = {}, len(book.times) - 1
    for i, t in enumerate(book.times):
        if i % GAMMA_EVERY == 0 or i == last:
            g = _gamma_at(book, t, inputs)
            if g:
                out[t] = g
    return out


# --------------------------------------------------------------------- touches


def _outcome(book, i, role, low, high) -> str:
    """held, broke, neither, or pending while the window is still open."""
    t, spot = book.times[i], book.spot[book.times[i]]
    bounce, through = BOUNCE_SHARE * spot, BREAK_SHARE * spot
    end = t + timedelta(minutes=AFTER_MINUTES)
    for later in book.times[i + 1 :]:
        if later > end:
            return "neither"
        p = book.spot[later]
        if role == SUPPORT:
            if p >= high + bounce:
                return "held"
            if p <= low - through:
                return "broke"
        else:
            if p <= low - bounce:
                return "held"
            if p >= high + through:
                return "broke"
    return "pending"


def _touches(book, history) -> list[dict]:
    """Every touch of a zone and of every round strike, with what followed."""
    out: list[dict] = []
    last: dict[tuple[str, str, float], datetime] = {}
    round_step = 2 * book.step
    for i in range(1, len(book.times)):
        t, prev_t = book.times[i], book.times[i - 1]
        spot, prev = book.spot[t], book.spot[prev_t]
        tol = TOUCH_SHARE * spot
        tests = []
        for role, zone in (history[prev_t] or {}).items():
            if zone:
                tests.append(
                    ("zone", role, zone["anchor"], zone["low"], zone["high"], zone["status"])
                )
        for k in _near(book, t, LEVEL_STEPS):
            if abs(k / round_step - round(k / round_step)) < 1e-9:
                tests.append(("round", SUPPORT if k <= spot else RESISTANCE, k, k, k, "round"))
        for kind, role, anchor, low, high, status in tests:
            if role == SUPPORT:
                touched = low - tol <= spot <= high + tol and prev > high + tol
            else:
                touched = low - tol <= spot <= high + tol and prev < low - tol
            key = (kind, role, anchor)
            if not touched or (key in last and t - last[key] < timedelta(minutes=TOUCH_COOLDOWN)):
                continue
            last[key] = t
            out.append(
                {
                    "time": t,
                    "kind": kind,
                    "role": role,
                    "strike": anchor,
                    "status": status,
                    "outcome": _outcome(book, i, role, low, high),
                }
            )
    return out


def hold_rates(touches: list[dict]) -> dict:
    """Held against broke, by kind and status. Touches that did neither are left out."""
    out: dict[str, dict] = {}
    for x in touches:
        if x["outcome"] not in ("held", "broke"):
            continue
        name = "round" if x["kind"] == "round" else f"zone_{x['status']}"
        row = out.setdefault(name, {"n": 0, "held": 0})
        row["n"] += 1
        row["held"] += x["outcome"] == "held"
    for row in out.values():
        row["rate"] = round(row["held"] / row["n"], 3) if row["n"] else None
        row["enough"] = row["n"] >= MIN_JUDGED
    return out


# ------------------------------------------------------------------------ walls


def _walls(book, index: int) -> list[dict]:
    """Per near-the-money strike at one minute: put and call open interest held,
    and how each has changed over CHANGE_WINDOW minutes. The strength heatmap is
    shaded from these: green where put writers sit (support), red where call
    writers sit (resistance), darker the more is written and the more it grows."""
    t = book.times[index]
    legs = book.legs[t]
    earlier = book.legs[book.times[max(0, index - CHANGE_WINDOW)]]
    out = []
    for k in _near(book, t, LEVEL_STEPS):
        put = legs.get(("PE", k), (0.0,))[0]
        call = legs.get(("CE", k), (0.0,))[0]
        out.append(
            {
                "strike": k,
                "put": put,
                "call": call,
                "put_change": put - earlier.get(("PE", k), (put,))[0],
                "call_change": call - earlier.get(("CE", k), (call,))[0],
            }
        )
    return out


# ------------------------------------------------------------------------ build


def build_levels(inputs: LevelInputs) -> dict | None:
    """The zones for the day so far, or None when fewer than two minutes are held."""
    book = _book(inputs.samples)
    if book is None:
        return None
    history, changes = _levels(book, inputs.expiry_day)
    gamma = _gamma(book, inputs)
    touches = _touches(book, history)
    stats = hold_rates(list(inputs.past_touches) + touches)

    t = book.times[-1]
    g_now = gamma.get(max(gamma)) if gamma else None
    zones = []
    for role in (SUPPORT, RESISTANCE):
        zone = history[t].get(role)
        if not zone:
            continue
        zone = dict(zone)
        measured = stats.get(
            f"zone_{zone['status']}", {"n": 0, "held": 0, "rate": None, "enough": False}
        )
        zone["measured"] = measured
        zone["high_gamma"] = bool(g_now and zone["low"] <= g_now["top_strike"] <= zone["high"])
        zone["touches_today"] = [
            x
            for x in touches
            if x["kind"] == "zone" and x["role"] == role and x["strike"] == zone["anchor"]
        ]
        zones.append(zone)

    return {
        "underlying": inputs.underlying,
        "minute": t,
        "spot": book.spot[t],
        "step": book.step,
        "thresholds": {
            "touch_pct": TOUCH_SHARE * 100,
            "bounce_pct": BOUNCE_SHARE * 100,
            "break_pct": BREAK_SHARE * 100,
            "after_min": AFTER_MINUTES,
            "chance": round(CHANCE, 3),
            "min_judged": MIN_JUDGED,
        },
        "zones": zones,
        "walls": _walls(book, len(book.times) - 1),
        "history": [
            {
                "time": m,
                SUPPORT: _brief(history[m].get(SUPPORT)),
                RESISTANCE: _brief(history[m].get(RESISTANCE)),
            }
            for m in book.times
        ],
        "changes": changes,
        "gamma": {
            "forward": g_now["forward"] if g_now else None,
            "top_strike": g_now["top_strike"] if g_now else None,
            "flip": g_now["flip"] if g_now else None,
            "series": [{"time": m, "flip": g["flip"]} for m, g in sorted(gamma.items())],
        },
        "touches": touches,
        "stats": stats,
    }


def _brief(zone: dict | None) -> list | None:
    return [zone["low"], zone["high"], zone["status"], zone["size"]] if zone else None
