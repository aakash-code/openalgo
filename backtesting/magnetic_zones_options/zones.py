"""Magnetic Zones [Open] — indicator math, ported 1:1 from the Pine v6 source.

Support/resistance ZONES are derived from the PREVIOUS Daily/Weekly/Monthly
candle's High/Low, as symmetric Fibonacci levels around the period midpoint:

    center = (prevHigh + prevLow) / 2
    rng    = prevHigh - prevLow
    R2 = center + 0.786*rng    R1 = center + 0.236*rng
    S1 = center - 0.236*rng    S2 = center - 0.786*rng

Each level is a band (zone) whose half-width is ``width_pct`` of the range / 2
(13% by default — the indicator's `widthPct`). ``half`` is that half-width and is
used for "price entered the zone" detection (mirrors the indicator's inR2/inS2).
"""

from __future__ import annotations

import pandas as pd

# Indicator defaults (input.float defaults in the Pine source).
FIB_INNER = 0.236
FIB_OUTER = 0.786
WIDTH_PCT = 0.13


def compute_zones(
    prev_high: float,
    prev_low: float,
    fib_inner: float = FIB_INNER,
    fib_outer: float = FIB_OUTER,
    width_pct: float = WIDTH_PCT,
) -> dict | None:
    """Return the active zones for a period given the PRIOR period's H/L.

    Returns ``None`` when the range is non-positive (degenerate / missing data).
    Keys: center, rng, R2, R1, S1, S2, half.
    """
    if prev_high is None or prev_low is None:
        return None
    rng = float(prev_high) - float(prev_low)
    if rng <= 0:
        return None
    center = (float(prev_high) + float(prev_low)) / 2.0
    half = rng * width_pct / 2.0
    return {
        "center": center,
        "rng": rng,
        "R2": center + fib_outer * rng,
        "R1": center + fib_inner * rng,
        "S1": center - fib_inner * rng,
        "S2": center - fib_outer * rng,
        "half": half,
    }


def period_zones(spot_bars: pd.DataFrame, **kw) -> pd.DataFrame:
    """Vectorised zones for every period from a bar frame indexed by period start.

    ``spot_bars`` must have columns ``h``/``l`` (period High/Low). The active
    zones for period *i* are derived from period *i-1* (the ``.shift(1)``), exactly
    as the indicator uses ``high[n]``/``low[n]`` of the PREVIOUS completed bar.

    Returns a frame indexed like ``spot_bars`` with columns
    [center, rng, R2, R1, S1, S2, half]; rows with no prior bar are dropped.
    """
    prev_h = spot_bars["h"].shift(1)
    prev_l = spot_bars["l"].shift(1)
    fib_inner = kw.get("fib_inner", FIB_INNER)
    fib_outer = kw.get("fib_outer", FIB_OUTER)
    width_pct = kw.get("width_pct", WIDTH_PCT)

    rng = prev_h - prev_l
    center = (prev_h + prev_l) / 2.0
    out = pd.DataFrame(index=spot_bars.index)
    out["center"] = center
    out["rng"] = rng
    out["R2"] = center + fib_outer * rng
    out["R1"] = center + fib_inner * rng
    out["S1"] = center - fib_inner * rng
    out["S2"] = center - fib_outer * rng
    out["half"] = rng * width_pct / 2.0
    return out[rng > 0].dropna()
