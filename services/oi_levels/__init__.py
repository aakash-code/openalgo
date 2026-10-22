"""Support and resistance zones from the recorded option open interest.

Built from the Writer Flow record minute by minute, with no lookahead. It
describes where option writers are positioned and how that is changing; it
does not forecast. On the four days it was built from (16, 19, 21 and
22-Oct-2026, NIFTY) the strike with the most open interest was rarely reached
and did not hold when it was, the day broke the morning's walls on three days
of four, and whether writers were adding at a strike made no difference to
whether price bounced there. So every zone carries its own measured hold rate
next to the rate chance would give, and the overlay says so.
"""

from services.oi_levels.core import LevelInputs, build_levels

__all__ = ["LevelInputs", "build_levels"]
