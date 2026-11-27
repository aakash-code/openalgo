"""TradeSmart's shared per-user rate limiting.

Two budgets, as the limiter has had since #1928: /GetQuotes is paced on its own
100/sec window, and every other call (orders, funds, margin, history) shares
one per-user budget of 10/sec and 120/min. These tests had pinned the earlier
single-budget model and called ``_reserve_slot()`` with no arguments, which
the two-budget limiter no longer accepts.
"""

import threading
import time
from collections import deque

import pytest

from broker.tradesmart.api import rate_limiter as rl


@pytest.fixture(autouse=True)
def _reset_gate():
    """Each test starts with cold windows, and leaves them behind."""
    rl._reserved_call_times = deque()
    rl._reserved_quote_times = deque()
    yield
    rl._reserved_call_times = deque()
    rl._reserved_quote_times = deque()


def general():
    """One slot from the shared general budget, as apply_rate_limit books it."""
    return rl._reserve_slot(
        rl._lock,
        rl._reserved_call_times,
        rl.TRADESMART_MAX_PER_SECOND,
        rl.TRADESMART_MAX_PER_MINUTE,
    )


def quote():
    """One slot from the quote budget."""
    return rl._reserve_slot(
        rl._quote_lock,
        rl._reserved_quote_times,
        rl.TRADESMART_QUOTE_MAX_PER_SECOND,
        rl.TRADESMART_QUOTE_MAX_PER_MINUTE,
    )


class TestCeilings:
    def test_per_second_pace_stays_under_the_broker_cap(self):
        """Broker rejects the 11th call in a second; we stop short of that."""
        assert rl.TRADESMART_MAX_PER_SECOND < 10

    def test_per_minute_pace_stays_under_the_broker_cap(self):
        """Broker rejects the 121st call in a minute; we stop short of that."""
        assert rl.TRADESMART_MAX_PER_MINUTE < 120

    def test_quotes_have_their_own_budget(self):
        """A spent general minute does not hold back quotes, and the reverse."""
        for _ in range(rl.TRADESMART_MAX_PER_MINUTE):
            general()
        assert general() > 30.0
        assert quote() == pytest.approx(0.0, abs=0.05)


class TestPacing:
    def test_a_burst_up_to_the_cap_goes_out_immediately(self):
        """Burst-friendly: the gate does not space calls that fit the window."""
        started = time.monotonic()
        for _ in range(rl.TRADESMART_MAX_PER_SECOND):
            rl.apply_rate_limit("/GetQuotes")
        assert time.monotonic() - started < 0.1

    def test_the_call_after_the_cap_waits_a_full_second(self):
        for _ in range(rl.TRADESMART_MAX_PER_SECOND):
            general()
        wait = general()
        assert 0.9 <= wait <= 1.05, f"expected ~1s wait, got {wait:.3f}s"

    def test_an_option_chain_batch_is_paced_not_rejected(self):
        """82 quotes (a 41-strike chain) spread over ~10s instead of failing."""
        waits = [general() for _ in range(82)]
        expected = (82 - 1) // rl.TRADESMART_MAX_PER_SECOND
        assert expected - 0.5 <= max(waits) <= expected + 0.5

    def test_history_and_orders_share_one_budget(self):
        """Every non-quote path counts against the one per-user budget."""
        for _ in range(rl.TRADESMART_MAX_PER_SECOND):
            rl.apply_rate_limit("/TPSeries")
        assert general() > 0.9


class TestWindowBookkeeping:
    def test_entries_older_than_a_minute_stop_constraining(self):
        """A stale window must not throttle a fresh burst."""
        rl._reserved_call_times = deque([time.time() - 120.0] * rl.TRADESMART_MAX_PER_MINUTE)
        assert general() == pytest.approx(0.0, abs=0.05)

    def test_reservations_stay_ordered(self):
        for _ in range(50):
            general()
        stamps = list(rl._reserved_call_times)
        assert stamps == sorted(stamps)


class TestConcurrency:
    def test_threads_space_out_rather_than_firing_together(self):
        """The slot is reserved under the lock, so waiters do not collide.

        If the timestamp were only written after sleeping, every thread would
        measure against the same stale window and fire at once.
        """
        stamps: list[float] = []
        stamps_lock = threading.Lock()

        def call():
            rl.apply_rate_limit("/TPSeries")
            with stamps_lock:
                stamps.append(time.monotonic())

        n = rl.TRADESMART_MAX_PER_SECOND * 2
        threads = [threading.Thread(target=call) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(stamps) == n
        stamps.sort()
        # The second half must land a second after the first, not alongside it.
        assert stamps[-1] - stamps[0] >= 0.9

    def test_sleep_happens_outside_the_lock(self):
        """A waiter must not block others from computing their slot.

        Holding the lock across sleep would serialise the threads into
        sequential sleeps; with the sleep outside, total wall time stays close
        to the span of the reserved slots rather than their sum.
        """
        started = time.monotonic()
        threads = [
            threading.Thread(target=rl.apply_rate_limit, args=("/TPSeries",))
            for _ in range(rl.TRADESMART_MAX_PER_SECOND * 2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert time.monotonic() - started < 2.0


class TestRateLimitDetection:
    def test_detects_the_per_minute_message(self):
        assert rl.is_rate_limit_error(
            {
                "stat": "Not_Ok",
                "emsg": "Invalid Input :  Order Recieved 121 in a current minute "
                "exceeds Limit 120 for user",
            }
        )

    def test_detects_the_per_second_message(self):
        assert rl.is_rate_limit_error(
            {
                "stat": "Not_Ok",
                "emsg": "Invalid Input :  Order Recieved 11 in a current second "
                "exceeds Limit 10 for user",
            }
        )

    def test_matches_a_changed_ceiling(self):
        """The number is not hardcoded, so a new limit is still recognised."""
        assert rl.is_rate_limit_error(
            {"stat": "Not_Ok", "emsg": "Order Recieved 101 exceeds Limit 100 for user"}
        )

    @pytest.mark.parametrize(
        "response",
        [
            {"stat": "Ok", "lp": "100"},
            {"stat": "Not_Ok", "emsg": "Session expired"},
            {"stat": "Not_Ok"},
            {"stat": "Not_Ok", "emsg": None},
            None,
            [],
            "not a dict",
        ],
    )
    def test_ignores_everything_else(self, response):
        assert rl.is_rate_limit_error(response) is False


class TestRetryDelay:
    def test_backs_off_exponentially(self):
        assert [rl.retry_delay(i) for i in range(3)] == [2.0, 4.0, 8.0]
