"""Full garbage collections stop walking the state OpenAlgo holds for the day.

Live on 16-Oct-2026 each full collection took 246 to 1,483 ms and stopped every
thread; freezing the long-lived objects took them under 70 ms.
"""

import gc
import time

import pytest

from utils.gc_freeze import freeze_long_lived


@pytest.fixture(autouse=True)
def _unfreeze():
    yield
    gc.unfreeze()


def _full_collection_ms() -> float:
    best = float("inf")
    for _ in range(3):
        started = time.perf_counter()
        gc.collect(2)
        best = min(best, time.perf_counter() - started)
    return best * 1000


def test_a_full_collection_skips_what_was_frozen():
    held = [{"symbol": f"SYM{i}", "token": i, "legs": [i, i + 1]} for i in range(400_000)]
    before = _full_collection_ms()
    assert freeze_long_lived("a test") >= len(held)
    after = _full_collection_ms()
    assert after < before / 3, f"full collection {before:.0f} ms before, {after:.0f} ms after"


def test_a_frozen_object_is_still_freed_when_nothing_holds_it():
    import weakref

    class Contract:
        pass

    contract = Contract()
    gone = weakref.ref(contract)
    freeze_long_lived("a test")
    del contract
    assert gone() is None


def test_freezing_again_reclaims_cycles_left_among_frozen_objects():
    import weakref

    class Node:
        pass

    a, b = Node(), Node()
    a.peer, b.peer = b, a
    gone = weakref.ref(a)
    freeze_long_lived("a test")
    del a, b
    assert gone() is not None  # a frozen cycle waits
    freeze_long_lived("the next load")
    assert gone() is None
