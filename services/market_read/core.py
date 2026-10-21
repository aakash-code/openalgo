"""The market read, computed from one day's recorded minutes. No I/O of any kind.

Everything is recomputed from the whole day on every call, so a restart, a
filled gap or a late minute changes nothing about what a minute says: the same
inputs always give the same events, with the same ids. The caller decides which
events are new to it.

Units: open interest is a quantity (shares), as Writer Flow records it, and is
shown in lakh. Times are IST wall-clock datetimes, one per minute.

Four readings, each held for HOLD_MINUTES before it counts, so one noisy minute
never announces a turn:

* Flow: open interest added to puts minus calls near the money over the last
  10 minutes. Put-led while that stays above FLOW_SHARE of the open interest near
  the money, call-led while it stays below minus that.
* Futures: price against open interest over 5 minutes, the current and next
  month added together so a rollover does not read as positions closing.
* Levels: the strike holding the most put open interest (support) and the most
  call open interest (resistance), and what happens at each.
* Volatility: India VIX and the implied volatility at the money.

BANKNIFTY and SENSEX are read for flow only, to say whether they agree or
turned first.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

LAKH = 100_000.0

HOLD_MINUTES = 3  # a reading must hold this long before it counts
FLOW_WINDOW = 10  # minutes summed for the flow reading
FLOW_SHARE = 0.01  # of the open interest near the money, per FLOW_WINDOW
# Chosen on the three recorded days: a 5-minute window flipped NIFTY's lead 24
# to 26 times on a full day at this share, a 10-minute one 8 to 10 times, and
# both called 21-Oct-2026's turn within a minute of each other (09:54, 09:55).
FUTURES_WINDOW = 5
BALANCED_MINUTES = 10  # no side above the threshold this long reads as balanced
BAND_STEPS = 6  # strikes either side of spot counted as near the money
LEVEL_STEPS = 10  # strikes either side of spot searched for support and resistance
SIGNIFICANT_SHARE = 0.5  # a level counts at half the biggest open interest on its side
WEAKEN_SHARE = 0.10  # a level weakens once its open interest is this far below its peak
WEAKEN_RESET_SHARE = 0.02  # and may weaken again once it is back within this of its peak
FUT_PRICE_SHARE = 0.0003  # a futures move smaller than this is no move (about 7 NIFTY points)
FUT_OI_SHARE = 0.0005  # an open interest change smaller than this is no change
VIX_JUMP = 0.25  # India VIX points within VIX_WINDOW
VIX_WINDOW = 10
VIX_COOLDOWN = 15
GAP_SHARE = 0.003  # an opening gap smaller than this is not worth filling
NEW_EXTREME_PAUSE = 15  # a new day high or low is news only after this long without one
# and only when it beats the old one by this much: on 21-Oct-2026 a low a
# fraction of a point under 10:13's read "22,929, below the 22,929".
NEW_EXTREME_SHARE = 0.0002
CHURN_RATIO = 20  # traded quantity per unit of open interest change that reads as churn
CHURN_WINDOW = 5
FLIP_SETTLE = 15  # a lead that changes back sooner than this is noise, not news
LEVEL_FLIP_COOLDOWN = 30  # minutes before the same strike may turn again

MAJOR = "major"
INFO = "info"

# The futures reading: (price direction, open interest direction) -> name.
FUTURES_CLASS = {
    (1, 1): "long_buildup",
    (1, -1): "short_covering",
    (-1, 1): "short_buildup",
    (-1, -1): "long_unwinding",
}
FUTURES_WORDS = {
    "long_buildup": "new longs coming in",
    "short_covering": "shorts covering",
    "short_buildup": "new shorts coming in",
    "long_unwinding": "longs exiting",
    "quiet": "no clear move",
}
FUTURES_TONE = {
    "long_buildup": 1,
    "short_covering": 1,
    "short_buildup": -1,
    "long_unwinding": -1,
    "quiet": 0,
}


@dataclass(frozen=True)
class MarketMinute:
    """The futures and volatility at one minute. Any field may be missing."""

    time: datetime
    fut: float | None = None  # current-month futures price
    fut_oi: float | None = None  # current plus next month open interest
    vix: float | None = None


@dataclass
class ReadInputs:
    underlying: str
    samples: list[dict]  # oi_flow_db.load_day rows for the underlying
    expiry_at: datetime | None = None  # the options' expiry, IST, for implied volatility
    market: list[MarketMinute] = field(default_factory=list)
    prev_close: float | None = None
    others: dict[str, list[dict]] = field(default_factory=dict)  # name -> samples
    # (strikes, ce_prices, pe_prices, forward, years) -> (ce, pe) lists of
    # {"iv", "delta"} or None: option_greeks_service.calculate_chain_greeks.
    greeks: Callable | None = None


# ---------------------------------------------------------------- minute book


@dataclass
class _Book:
    times: list[datetime]
    legs: dict[datetime, dict[tuple[str, float], tuple[float, float, float]]]
    spot: dict[datetime, float]
    step: float


def _book(samples: list[dict]) -> _Book | None:
    legs: dict[datetime, dict] = {}
    spots: dict[datetime, list[float]] = {}
    for r in samples:
        t = r["sample_time"]
        oi = r.get("oi")
        if oi is None:
            continue
        legs.setdefault(t, {})[(r["side"], float(r["strike"]))] = (
            float(oi),
            float(r.get("ltp") or 0.0),
            float(r.get("volume") or 0.0),
        )
        if r.get("spot"):
            spots.setdefault(t, []).append(float(r["spot"]))
    times = sorted(t for t in legs if t in spots)
    if len(times) < 2:
        return None
    strikes = sorted({k for t in times for _, k in legs[t]})
    gaps = [b - a for a, b in zip(strikes, strikes[1:], strict=False) if b > a]
    if not gaps:
        return None
    step = statistics.mode(gaps)
    return _Book(
        times=times,
        legs={t: legs[t] for t in times},
        spot={t: statistics.median(spots[t]) for t in times},
        step=step,
    )


def _near(book: _Book, t: datetime, steps: int) -> list[float]:
    spot = book.spot[t]
    return sorted({k for (_, k) in book.legs[t] if abs(k - spot) <= steps * book.step + 1e-9})


# ----------------------------------------------------------------------- flow


@dataclass
class _FlowMinute:
    time: datetime
    ce: float  # call open interest change near the money this minute
    pe: float
    dce: float | None  # the same, weighted by each strike's delta
    dpe: float | None
    band_oi: float  # all open interest near the money, both sides
    atm_volume: float  # traded at the money this minute, both sides
    atm_oi_change: float  # absolute open interest change at the money


def _flow(book: _Book, deltas: dict[datetime, dict] | None) -> list[_FlowMinute]:
    out = []
    for prev, t in zip(book.times, book.times[1:], strict=False):
        now, before = book.legs[t], book.legs[prev]
        near = _near(book, t, BAND_STEPS)
        atm = min(near, key=lambda k: abs(k - book.spot[t])) if near else None
        ce = pe = dce = dpe = band = vol = atm_change = 0.0
        weighted = deltas is not None and t in deltas
        for k in near:
            for side in ("CE", "PE"):
                leg, was = now.get((side, k)), before.get((side, k))
                if leg is None:
                    continue
                band += leg[0]
                if was is None:
                    continue
                change = leg[0] - was[0]
                d = abs(deltas[t].get((side, k), 0.0)) if weighted else 0.0
                if side == "CE":
                    ce += change
                    dce += change * d
                else:
                    pe += change
                    dpe += change * d
                if atm is not None and abs(k - atm) <= book.step + 1e-9:
                    vol += max(leg[2] - was[2], 0.0)
                    atm_change += abs(change)
        out.append(
            _FlowMinute(
                time=t,
                ce=ce,
                pe=pe,
                dce=dce if weighted else None,
                dpe=dpe if weighted else None,
                band_oi=band,
                atm_volume=vol,
                atm_oi_change=atm_change,
            )
        )
    return out


def _window_sum(values: list[float | None], i: int, n: int) -> float | None:
    part = values[max(0, i - n + 1) : i + 1]
    if any(v is None for v in part):
        return None
    return sum(part)  # type: ignore[arg-type]


@dataclass
class _Regime:
    time: datetime
    side: str  # "put", "call" or "balanced"
    since: datetime


def _regimes(flow: list[_FlowMinute]) -> tuple[list[_Regime], list[float], list[float]]:
    """Who leads the flow, held HOLD_MINUTES before it counts. Returns the
    changes, plus the FLOW_WINDOW net flow and its threshold for every minute."""
    net = [f.pe - f.ce for f in flow]
    changes: list[_Regime] = []
    current = None
    run_side, run_start, run_len, quiet = None, None, 0, 0
    nets, thresholds = [], []
    for i, f in enumerate(flow):
        n = _window_sum(net, i, FLOW_WINDOW) or 0.0
        threshold = FLOW_SHARE * f.band_oi
        nets.append(n)
        thresholds.append(threshold)
        side = "put" if n > threshold else "call" if n < -threshold else None
        if side is None:
            run_side, run_len = None, 0
            quiet += 1
            if current not in (None, "balanced") and quiet >= BALANCED_MINUTES:
                current = "balanced"
                changes.append(_Regime(f.time, "balanced", flow[i - quiet + 1].time))
            continue
        quiet = 0
        if side == run_side:
            run_len += 1
        else:
            run_side, run_start, run_len = side, f.time, 1
        if run_len >= HOLD_MINUTES and side != current:
            current = side
            changes.append(_Regime(f.time, side, run_start))
    return changes, nets, thresholds


# ------------------------------------------------------------------- greeks


def _deltas_and_iv(
    book: _Book, inputs: ReadInputs, forward: dict[datetime, float]
) -> tuple[dict[datetime, dict], dict[datetime, float]]:
    """Each leg's delta, and the implied volatility at the money, per minute."""
    if inputs.greeks is None or inputs.expiry_at is None:
        return {}, {}
    deltas: dict[datetime, dict] = {}
    atm_iv: dict[datetime, float] = {}
    for t in book.times:
        years = (inputs.expiry_at - t).total_seconds() / (365 * 86400)
        fwd = forward.get(t) or book.spot[t]
        strikes = _near(book, t, LEVEL_STEPS)
        if years <= 0 or not strikes or not fwd:
            continue
        legs = book.legs[t]
        ce_px = [legs.get(("CE", k), (0, 0, 0))[1] or None for k in strikes]
        pe_px = [legs.get(("PE", k), (0, 0, 0))[1] or None for k in strikes]
        try:
            ce, pe = inputs.greeks(strikes, ce_px, pe_px, fwd, years)
        except Exception:
            continue  # a minute without greeks reads unweighted; never stops the read
        row = {}
        for k, c, p in zip(strikes, ce, pe, strict=False):
            if c:
                row[("CE", k)] = c.get("delta") or 0.0
            if p:
                row[("PE", k)] = p.get("delta") or 0.0
        deltas[t] = row
        i = min(range(len(strikes)), key=lambda j: abs(strikes[j] - fwd))
        ivs = [g.get("iv") for g in (ce[i], pe[i]) if g and g.get("iv")]
        if ivs:
            atm_iv[t] = sum(ivs) / len(ivs)
    return deltas, atm_iv


# ------------------------------------------------------------------- futures


def _forward(market: list[MarketMinute], book: _Book) -> dict[datetime, float]:
    """The futures price per recorded minute, carried from the last one known.

    Implied volatility needs the forward, not spot: priced off spot, NIFTY's
    calls read two points of volatility above its puts on 21-Oct-2026, because
    the future traded 25 points over the index.
    """
    by_time = {m.time: m.fut for m in market if m.fut}
    out, last_basis = {}, None
    for t in book.times:
        if t in by_time:
            last_basis = by_time[t] - book.spot[t]
        if last_basis is not None:
            out[t] = book.spot[t] + last_basis
    return out


@dataclass
class _FutState:
    time: datetime
    cls: str
    since: datetime
    price_change: float
    oi_change: float


def _futures(market: list[MarketMinute]) -> tuple[list[_FutState], _FutState | None]:
    rows = [m for m in market if m.fut and m.fut_oi]
    changes: list[_FutState] = []
    current = None
    run_cls, run_start, run_len = None, None, 0
    latest = None
    for i in range(FUTURES_WINDOW, len(rows)):
        a, b = rows[i - FUTURES_WINDOW], rows[i]
        dp, doi = b.fut - a.fut, b.fut_oi - a.fut_oi
        p_dir = 0 if abs(dp) < FUT_PRICE_SHARE * b.fut else (1 if dp > 0 else -1)
        o_dir = 0 if abs(doi) < FUT_OI_SHARE * b.fut_oi else (1 if doi > 0 else -1)
        cls = FUTURES_CLASS.get((p_dir, o_dir), "quiet")
        if cls == run_cls:
            run_len += 1
        else:
            run_cls, run_start, run_len = cls, b.time, 1
        latest = _FutState(b.time, cls, run_start, dp, doi)
        if run_len >= HOLD_MINUTES and cls != current and cls != "quiet":
            current = cls
            changes.append(_FutState(b.time, cls, run_start, dp, doi))
    return changes, latest


# -------------------------------------------------------------------- events


def _event(kind, t, level, title, reason, key="") -> dict:
    return {
        "id": f"{kind}:{key}:{t:%H%M}",
        "kind": kind,
        "time": t,
        "level": level,
        "title": title,
        "reason": reason,
    }


def _lakh(x: float) -> str:
    return f"{x / LAKH:,.1f} lakh"


def _signed_lakh(x: float) -> str:
    return f"{x / LAKH:+,.1f} lakh"


def _strike(k: float) -> str:
    return f"{k:,.0f}" if float(k).is_integer() else f"{k:,.1f}"


def _hm(t: datetime) -> str:
    return t.strftime("%H:%M")


def _flow_events(underlying, regimes, flow, index_of) -> list[dict]:
    out = []
    previous, last_side = None, None
    for r in regimes:
        i = index_of[r.time]
        part = flow[max(0, i - FLOW_WINDOW + 1) : i + 1]
        ce, pe = sum(f.ce for f in part), sum(f.pe for f in part)
        if r.side == "balanced":
            out.append(
                _event(
                    "flow_balanced",
                    r.time,
                    INFO,
                    f"Neither side leads {underlying} option writing",
                    f"No clear lead from puts or calls since {_hm(r.since)}.",
                )
            )
            continue
        writers = "Put" if r.side == "put" else "Call"
        # A lead that lasted only minutes before this one is the flow swinging,
        # not a turn: on a full day the lead changed 15 to 17 times.
        # Nor is the same side returning after a quiet spell.
        settled = previous is None or r.since - previous >= timedelta(minutes=FLIP_SETTLE)
        turned = r.side != last_side
        previous, last_side = r.since, r.side
        out.append(
            _event(
                f"flow_{r.side}",
                r.time,
                MAJOR if settled and turned else INFO,
                f"{writers} writers took over {underlying}",
                f"Since {_hm(r.since)}, near the money: put open interest "
                f"{_signed_lakh(pe)}, call {_signed_lakh(ce)} in the last "
                f"{FLOW_WINDOW} minutes.",
            )
        )
    return out


def _level_events(book: _Book, expiry_day: bool = False) -> tuple[list[dict], dict]:
    """Support and resistance from where open interest sits, and their changes."""
    events: list[dict] = []
    peak: dict[tuple[str, float], float] = {}
    weakened: set[tuple[str, float]] = set()
    held = {"PE": (None, None, 0), "CE": (None, None, 0)}  # strike, since, minutes
    bases = {"PE": {}, "CE": {}}  # strike -> first minute it held as the level
    crossed: dict[tuple[str, float], tuple[datetime, float, int]] = {}
    flipped: set[tuple[str, float]] = set()
    last_flip: dict[tuple[str, float], datetime] = {}
    state = {"support": None, "resistance": None}

    for t in book.times:
        legs, spot = book.legs[t], book.spot[t]
        near = _near(book, t, LEVEL_STEPS)
        for key, leg in legs.items():
            peak[key] = max(peak.get(key, 0.0), leg[0])
        for side, role in (("PE", "support"), ("CE", "resistance")):
            ois = {k: legs[(side, k)][0] for k in near if (side, k) in legs}
            # Support sits at or below the price and resistance at or above it:
            # the most put open interest above the price is not holding it up.
            half = book.step / 2
            own = {
                k: oi
                for k, oi in ois.items()
                if (k <= spot + half if side == "PE" else k >= spot - half)
            }
            if not own:
                continue
            top = max(own, key=own.get)
            strike, since, minutes = held[side]
            if top == strike:
                minutes += 1
            else:
                strike, since, minutes = top, t, 1
            held[side] = (strike, since, minutes)
            if minutes == HOLD_MINUTES:
                first = not bases[side]
                bases[side].setdefault(strike, since)
                word = "put" if side == "PE" else "call"
                if first:
                    title = f"{role.capitalize()} at {_strike(strike)}"
                else:
                    title = f"Biggest {word} base moved to {_strike(strike)}"
                events.append(
                    _event(
                        f"{role}_at",
                        t,
                        INFO,
                        title,
                        f"{_strike(strike)} holds the most {word} open interest near the "
                        f"money: {_lakh(ois[strike])}.",
                        key=_strike(strike),
                    )
                )
            if minutes >= HOLD_MINUTES:
                state[role] = strike

            # A big strike weakens as its writers leave: on 21-Oct-2026 put
            # writers left 23,100 from 09:48, minutes before the flow turned.
            biggest = max(ois.values())
            for k, oi in ois.items():
                key = (side, k)
                if peak[key] < SIGNIFICANT_SHARE * biggest or key in flipped:
                    continue
                if key not in weakened and oi <= (1 - WEAKEN_SHARE) * peak[key]:
                    weakened.add(key)
                    word = "Put" if side == "PE" else "Call"
                    # Major only at the level itself, and never on expiry day,
                    # when open interest falls at every strike as positions close
                    # (19-Oct-2026: 35 such warnings in a day).
                    close = k == state[role] and not expiry_day
                    events.append(
                        _event(
                            f"{role}_weakening",
                            t,
                            MAJOR if close else INFO,
                            f"{word} writers leaving {_strike(k)}",
                            f"{word} open interest at {_strike(k)} is {_lakh(oi)}, "
                            f"{(1 - oi / peak[key]) * 100:.0f}% below its peak of "
                            f"{_lakh(peak[key])}.",
                            key=_strike(k),
                        )
                    )
                elif key in weakened and oi >= (1 - WEAKEN_RESET_SHARE) * peak[key]:
                    weakened.discard(key)

        # A level the price has crossed, now defended from the other side.
        for side, opposite in (("PE", "CE"), ("CE", "PE")):
            for k in bases[side]:
                key = (side, k)
                beyond = spot < k if side == "PE" else spot > k
                if not beyond:
                    crossed.pop(key, None)
                    flipped.discard(key)
                    continue
                if (opposite, k) not in legs or (side, k) not in legs:
                    continue
                first_t, opp_then, minutes = crossed.get(key, (t, legs[(opposite, k)][0], 0))
                minutes += 1
                crossed[key] = (first_t, opp_then, minutes)
                opp_now, own_now = legs[(opposite, k)][0], legs[(side, k)][0]
                if (
                    key not in flipped
                    and minutes >= HOLD_MINUTES
                    and (
                        key not in last_flip
                        or t - last_flip[key] >= timedelta(minutes=LEVEL_FLIP_COOLDOWN)
                    )
                    and opp_now > opp_then
                    and own_now <= (1 - WEAKEN_SHARE / 2) * peak[key]
                ):
                    flipped.add(key)
                    last_flip[key] = t
                    if side == "PE":
                        title = f"{_strike(k)} turned from support to resistance"
                        what = (
                            f"Price below {_strike(k)} since {_hm(first_t)}. Put open "
                            f"interest there is down to {_lakh(own_now)} from "
                            f"{_lakh(peak[key])}, and calls written there rose "
                            f"{_signed_lakh(opp_now - opp_then)}."
                        )
                        kind = "support_to_resistance"
                    else:
                        title = f"{_strike(k)} turned from resistance to support"
                        what = (
                            f"Price above {_strike(k)} since {_hm(first_t)}. Call open "
                            f"interest there is down to {_lakh(own_now)} from "
                            f"{_lakh(peak[key])}, and puts written there rose "
                            f"{_signed_lakh(opp_now - opp_then)}."
                        )
                        kind = "resistance_to_support"
                    events.append(_event(kind, t, MAJOR, title, what, key=_strike(k)))
    return events, state


def _vix_events(market: list[MarketMinute]) -> list[dict]:
    rows = [m for m in market if m.vix]
    out, last = [], {}
    for i, m in enumerate(rows):
        window = [r.vix for r in rows[max(0, i - VIX_WINDOW) : i + 1]]
        for kind, move, level in (
            ("vix_up", m.vix - min(window), MAJOR),
            ("vix_down", max(window) - m.vix, INFO),
        ):
            if move < VIX_JUMP:
                continue
            if kind in last and m.time - last[kind] < timedelta(minutes=VIX_COOLDOWN):
                continue
            last[kind] = m.time
            if kind == "vix_up":
                title = "Fear rising: India VIX jumped"
                reason = f"India VIX {m.vix:.2f}, up {move:.2f} in {VIX_WINDOW} minutes."
            else:
                title = "Fear easing: India VIX fell"
                reason = f"India VIX {m.vix:.2f}, down {move:.2f} in {VIX_WINDOW} minutes."
            out.append(_event(kind, m.time, level, title, reason))
    return out


def _price_events(underlying: str, book: _Book, prev_close: float | None) -> list[dict]:
    out = []
    times = book.times
    open_ = book.spot[times[0]]
    if prev_close and abs(open_ - prev_close) >= GAP_SHARE * prev_close:
        up = open_ > prev_close
        for t in times[1:]:
            s = book.spot[t]
            if (up and s <= prev_close) or (not up and s >= prev_close):
                out.append(
                    _event(
                        "gap_filled",
                        t,
                        MAJOR,
                        f"{underlying} filled its opening gap",
                        f"Opened {open_ - prev_close:+,.0f} from the previous close of "
                        f"{prev_close:,.0f}, and is now back at {s:,.0f}.",
                    )
                )
                break
    high = low = open_
    high_t = low_t = times[0]
    for t in times[1:]:
        s = book.spot[t]
        if s > high:
            if t - high_t >= timedelta(minutes=NEW_EXTREME_PAUSE) and s - high >= (
                NEW_EXTREME_SHARE * high
            ):
                out.append(
                    _event(
                        "day_high",
                        t,
                        INFO,
                        f"{underlying} made a new day high",
                        f"{s:,.0f}, above the {high:,.0f} it made at {_hm(high_t)}.",
                    )
                )
            high, high_t = s, t
        if s < low:
            if t - low_t >= timedelta(minutes=NEW_EXTREME_PAUSE) and low - s >= (
                NEW_EXTREME_SHARE * low
            ):
                out.append(
                    _event(
                        "day_low",
                        t,
                        INFO,
                        f"{underlying} made a new day low",
                        f"{s:,.0f}, below the {low:,.0f} it made at {_hm(low_t)}.",
                    )
                )
            low, low_t = s, t
    return out


def _futures_events(changes: list[_FutState]) -> list[dict]:
    out = []
    for c in changes:
        tone = FUTURES_TONE[c.cls]
        out.append(
            _event(
                "futures",
                c.time,
                INFO,
                f"Futures: {FUTURES_WORDS[c.cls]}",
                f"Since {_hm(c.since)}: price {c.price_change:+,.0f} and open interest "
                f"{_signed_lakh(c.oi_change)} over {FUTURES_WINDOW} minutes "
                f"({'supports a rise' if tone > 0 else 'weighs on price'}), current and "
                "next month together.",
                key=c.cls,
            )
        )
    return out


def _other_events(underlying, own: list[_Regime], others: dict[str, list[_Regime]]) -> list[dict]:
    """Another index whose writers turned before this one's did."""
    out = []
    for name, regimes in others.items():
        for r in regimes:
            if r.side == "balanced":
                continue
            own_then = [x for x in own if x.time <= r.time]
            if own_then and own_then[-1].side == r.side:
                continue  # already agrees
            writers = "put" if r.side == "put" else "call"
            out.append(
                _event(
                    "other_leads",
                    r.time,
                    INFO,
                    f"{name} {writers} writers took over first",
                    f"{name} turned {writers}-led at {_hm(r.since)}; {underlying} has not "
                    "turned the same way yet.",
                    key=f"{name}-{r.side}",
                )
            )
    return out


# --------------------------------------------------------------------- read


def _tone_word(side: str | None) -> int:
    return {"put": 1, "call": -1}.get(side or "", 0)


def build_read(inputs: ReadInputs) -> dict | None:
    """The read for the day so far, or None when fewer than two minutes are held."""
    book = _book(inputs.samples)
    if book is None:
        return None
    u = inputs.underlying
    forward = _forward(inputs.market, book)
    deltas, atm_iv = _deltas_and_iv(book, inputs, forward)
    flow = _flow(book, deltas or None)
    index_of = {f.time: i for i, f in enumerate(flow)}
    regimes, nets, thresholds = _regimes(flow)

    other_regimes, other_now = {}, {}
    for name, samples in inputs.others.items():
        ob = _book(samples)
        if ob is None:
            continue
        of = _flow(ob, None)
        changes, onets, othr = _regimes(of)
        other_regimes[name] = changes
        last = changes[-1].side if changes else None
        other_now[name] = {
            "side": last,
            "since": _hm(changes[-1].since) if changes else None,
            "net": onets[-1] / LAKH,
            "threshold": othr[-1] / LAKH,
        }

    fut_changes, fut_latest = _futures(inputs.market)
    expiry_day = inputs.expiry_at is not None and inputs.expiry_at.date() == book.times[0].date()
    level_events, levels = _level_events(book, expiry_day)

    events = (
        _flow_events(u, regimes, flow, index_of)
        + level_events
        + _futures_events(fut_changes)
        + _vix_events(inputs.market)
        + _price_events(u, book, inputs.prev_close)
        + _other_events(u, regimes, other_regimes)
    )
    first, last_t = book.times[0], book.times[-1]
    events = [e for e in events if first <= e["time"] <= last_t]
    events.sort(key=lambda e: (e["time"], e["level"] != MAJOR, e["kind"]))

    # The current state, for the panel's chips.
    regime = regimes[-1] if regimes else None
    dnet = [None if f.dpe is None else f.dpe - f.dce for f in flow]
    dnet_window = _window_sum(dnet, len(flow) - 1, FLOW_WINDOW)
    churn_part = flow[-CHURN_WINDOW:]
    churn_vol = sum(f.atm_volume for f in churn_part)
    churn_oi = sum(f.atm_oi_change for f in churn_part)
    vix_rows = [m for m in inputs.market if m.vix]
    vix_now = vix_rows[-1].vix if vix_rows else None
    vix_open = vix_rows[0].vix if vix_rows else None
    vix_10 = [m.vix for m in vix_rows if m.time >= last_t - timedelta(minutes=VIX_WINDOW)]
    iv_now = atm_iv.get(last_t)
    iv_then = next(
        (
            atm_iv[t]
            for t in book.times
            if t >= last_t - timedelta(minutes=VIX_WINDOW) and t in atm_iv
        ),
        None,
    )
    legs = book.legs[last_t]

    def level(role, side):
        k = levels[role]
        if k is None or (side, k) not in legs:
            return None
        oi = legs[(side, k)][0]
        peak = max(book.legs[t][(side, k)][0] for t in book.times if (side, k) in book.legs[t])
        return {
            "strike": k,
            "oi_lakh": round(oi / LAKH, 1),
            "peak_lakh": round(peak / LAKH, 1),
            "from_peak_pct": round((oi / peak - 1) * 100, 1) if peak else 0.0,
        }

    spot = book.spot[last_t]
    day_high = max(book.spot.values())
    day_low = min(book.spot.values())

    half = thresholds[-1] / 2
    leaning = "put" if nets[-1] > half else "call" if nets[-1] < -half else None
    state = {
        "flow": {
            "side": regime.side if regime else None,
            # Which way the last FLOW_WINDOW minutes lean now. Differs from the
            # lead while a turn is under way but not yet held HOLD_MINUTES:
            # on 21-Oct-2026 at 11:13 the lead read put writers while the last
            # ten minutes were puts -12.9 lakh, calls +3.7.
            "leaning": leaning,
            "since": _hm(regime.since) if regime else None,
            "net_lakh": round(nets[-1] / LAKH, 1),
            "threshold_lakh": round(thresholds[-1] / LAKH, 1),
            "ce_lakh": round(sum(f.ce for f in flow[-FLOW_WINDOW:]) / LAKH, 1),
            "pe_lakh": round(sum(f.pe for f in flow[-FLOW_WINDOW:]) / LAKH, 1),
            "delta_net_lakh": None if dnet_window is None else round(dnet_window / LAKH, 1),
            # Coloured only while the last minutes still agree with the lead.
            "tone": _tone_word(regime.side) if regime and regime.side == leaning else 0,
        },
        "futures": {
            "cls": fut_latest.cls if fut_latest else None,
            "words": FUTURES_WORDS.get(fut_latest.cls) if fut_latest else None,
            "since": _hm(fut_latest.since) if fut_latest else None,
            "price_change": round(fut_latest.price_change, 1) if fut_latest else None,
            "oi_change_lakh": round(fut_latest.oi_change / LAKH, 2) if fut_latest else None,
            "tone": FUTURES_TONE.get(fut_latest.cls, 0) if fut_latest else 0,
        },
        "levels": {"support": level("support", "PE"), "resistance": level("resistance", "CE")},
        "volatility": {
            "vix": vix_now,
            "vix_from_open": round(vix_now - vix_open, 2) if vix_now and vix_open else None,
            "vix_10m": round(vix_now - vix_10[0], 2) if vix_now and vix_10 else None,
            "atm_iv": round(iv_now, 2) if iv_now else None,
            "atm_iv_10m": round(iv_now - iv_then, 2) if iv_now and iv_then else None,
        },
        "churn": {
            "traded_lakh": round(churn_vol / LAKH, 1),
            "oi_change_lakh": round(churn_oi / LAKH, 1),
            "changing_hands": churn_oi > 0 and churn_vol / churn_oi >= CHURN_RATIO,
        },
        "others": other_now,
        "price": {
            "spot": round(spot, 2),
            "open": round(book.spot[first], 2),
            "prev_close": inputs.prev_close,
            "day_high": round(day_high, 2),
            "day_low": round(day_low, 2),
        },
    }
    series = [
        {
            "time": f.time,
            "spot": round(book.spot[f.time], 2),
            "net_lakh": round(n / LAKH, 1),
            "threshold_lakh": round(th / LAKH, 1),
        }
        for f, n, th in zip(flow, nets, thresholds, strict=False)
    ]
    return {
        "underlying": u,
        "minute": last_t,
        "headline": _headline(u, state, events),
        "state": state,
        "events": events,
        "series": series,
    }


def _headline(u: str, state: dict, events: list[dict]) -> str:
    flow = state["flow"]
    if flow["side"] in ("put", "call"):
        who = "Put writers" if flow["side"] == "put" else "Call writers"
        parts = [f"{who} lead {u} since {flow['since']}"]
        if flow["leaning"] and flow["leaning"] != flow["side"]:
            parts[0] += f", but {flow['leaning']} writers are taking over"
        elif not flow["leaning"]:
            parts[0] += ", fading"
    else:
        parts = [f"Neither puts nor calls lead {u}"]
    fut = state["futures"]
    if fut["cls"] and fut["cls"] != "quiet":
        parts.append(f"futures show {fut['words']}")
    major_levels = [
        e
        for e in events
        if e["kind"] in ("support_to_resistance", "resistance_to_support", "gap_filled")
    ]
    sentence = "; ".join(parts) + "."
    if major_levels:
        last = major_levels[-1]
        sentence += f" {last['title']} at {_hm(last['time'])}."
    return sentence
