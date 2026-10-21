"""Market read: what option writers, futures traders and volatility are doing now.

Writer Flow shows one strike at a time. A trader watching a chart cannot also
follow every strike's open interest, the futures' price against their open
interest, implied volatility, India VIX and two other indices, so this reads
them together every minute and says what it finds in plain sentences.

It describes; it does not forecast. On the three days recorded when it was
built (16, 19 and 21-Oct-2026) the net writer flow followed NIFTY's price over
the previous 5 and 15 minutes (correlation +0.3 to +0.8) and said nothing
about the next 15 (-0.3 to +0.1). Every minute's reading is stored, so that can
be measured again over more days.

* ``core``  the reading itself: no I/O, no clock, no logging. Every input is an
            argument and every result a return value, as in ``services/risk``.
"""

from services.market_read.core import ReadInputs, build_read

__all__ = ["ReadInputs", "build_read"]
