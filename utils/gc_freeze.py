"""Keep Python's full garbage collection short in a process that holds a lot.

OpenAlgo keeps hundreds of thousands of long-lived objects: the symbol cache,
the master contract, scheduler state, imported modules. A full collection
(generation 2) walks every one of them, and it holds the interpreter while it
does, so every thread stops: requests, the tick fan-out, order placement.

Measured live on 16-Oct-2026 during the MCX session, with a Writer Flow chart
and the TradeFinder panels open: full collections every 15 to 20 seconds, each
246 to 1,483 ms, and each one showed up as a request taking that much longer.
After freezing the 613,409 objects held once startup had finished, no full
collection took longer than 70 ms.

``gc.freeze()`` moves every object currently tracked into a permanent
generation that collections skip. Reference counting still frees a frozen
object the moment nothing refers to it, so replacing the symbol cache at the
next login frees the old one as before; only garbage held in reference cycles
among frozen objects waits, and each call here unfreezes and collects first,
so that is reclaimed at the next freeze.

Called once the long-lived state is in place: after the startup cache restore
and after each master contract load. The one full collection each call makes
is the price, paid at a moment the process is already busy loading.
"""

import gc

from utils import real_threading
from utils.logging import get_logger

logger = get_logger(__name__)

# Two loads finishing together (a login during the startup restore) freeze once
# each, never interleaved. A real lock: either may run on a real thread.
_lock = real_threading.Lock()


def freeze_long_lived(reason: str) -> int:
    """Collect once, then exclude everything now held from later full collections.

    Returns the number of objects frozen. Never raises: a process that cannot
    freeze simply keeps collecting as before.
    """
    try:
        with _lock:
            gc.unfreeze()
            gc.collect()
            gc.freeze()
            frozen = gc.get_freeze_count()
        logger.info(f"Garbage collection: {frozen} long-lived objects set aside after {reason}")
        return frozen
    except Exception:
        logger.exception("Garbage collection: could not set long-lived objects aside")
        return 0
