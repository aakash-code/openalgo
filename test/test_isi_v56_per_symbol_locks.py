"""
Concurrency regression tests for the per-symbol locking in
strategies/isi_v56_live_strategy.py.

Motivation: previously a single `positions_lock` guarded ALL per-position
management, and it was held across broker REST round-trips (sync_with_broker,
close_position, trail). A slow/hung broker response on symbol A's routine sync
therefore blocked symbol B's stop-loss from firing. The fix keeps
`positions_lock` only for the fast shared-aggregate mutations
(positions/pending dicts, _entering set, trades_today, day_realized_pnl) and
introduces a PER-SYMBOL lock for the management work.

Invariants these tests pin down:
  1. `_symbol_lock` is stable per symbol, distinct across symbols.
  2. Different symbols can be managed CONCURRENTLY (the whole point).
  3. The SAME symbol is still SERIALIZED (money-safety: no double-action).
  4. `day_realized_pnl` is correct under concurrent closes on distinct symbols
     (the positions_lock-guarded increment prevents a lost update).
  5. The nested per-symbol -> positions_lock path does not deadlock.
"""

import importlib.util
import site
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "strategies" / "isi_v56_live_strategy.py"


@pytest.fixture(scope="module")
def isi():
    # Pin the real installed SDK before loading the strategy (repo root shares
    # the name "openalgo" with an empty sentinel __init__.py).
    if "openalgo" not in sys.modules or not hasattr(sys.modules["openalgo"], "api"):
        for site_dir in site.getsitepackages() + [site.getusersitepackages()]:
            sdk = Path(site_dir) / "openalgo" / "__init__.py"
            if sdk.exists():
                spec = importlib.util.spec_from_file_location(
                    "openalgo", sdk, submodule_search_locations=[str(sdk.parent)]
                )
                mod = importlib.util.module_from_spec(spec)
                sys.modules["openalgo"] = mod
                spec.loader.exec_module(mod)
                break
    spec = importlib.util.spec_from_file_location("isi_locks_under_test", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bare_trader(isi):
    """An ISILiveTrader with only the locking attributes the tests touch —
    no threads, no network, no __init__."""
    t = isi.ISILiveTrader.__new__(isi.ISILiveTrader)
    t.positions = {}
    t.pending_entries = {}
    t._sl_breach_since = {}
    t.day_realized_pnl = 0.0
    t.trades_today = 0
    t.positions_lock = threading.Lock()
    t._symbol_locks = {}
    t._symbol_locks_guard = threading.Lock()
    return t


# ── 1. lock identity ─────────────────────────────────────────────────────────


def test_symbol_lock_is_stable_and_distinct(isi):
    t = _bare_trader(isi)
    a1 = t._symbol_lock("RELIANCE")
    a2 = t._symbol_lock("RELIANCE")
    b = t._symbol_lock("TCS")
    assert a1 is a2, "same symbol must return the same lock object"
    assert a1 is not b, "different symbols must get different locks"


# ── 2 & 3. concurrency vs serialization ──────────────────────────────────────


def _run_two(t, isi, sym_a, sym_b, hold_s=0.3):
    """Run two 'management' sections that each hold their symbol lock for
    hold_s seconds, and measure wall-clock. Different symbols overlap; the
    same symbol serializes."""
    barrier = threading.Barrier(2)

    def work(sym):
        barrier.wait()
        with t._symbol_lock(sym):
            time.sleep(hold_s)

    t0 = time.perf_counter()
    threads = [threading.Thread(target=work, args=(sym_a,)),
               threading.Thread(target=work, args=(sym_b,))]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    return time.perf_counter() - t0


def test_different_symbols_run_concurrently(isi):
    t = _bare_trader(isi)
    elapsed = _run_two(t, isi, "RELIANCE", "TCS", hold_s=0.3)
    # Two different symbols overlap -> ~0.3s, not ~0.6s. Generous ceiling for
    # scheduler jitter but well below the serialized 0.6s.
    assert elapsed < 0.5, f"different symbols should overlap; took {elapsed:.2f}s (expected ~0.3s)"


def test_same_symbol_is_serialized(isi):
    t = _bare_trader(isi)
    elapsed = _run_two(t, isi, "RELIANCE", "RELIANCE", hold_s=0.3)
    # Same symbol -> the two sections cannot overlap -> ~0.6s.
    assert elapsed >= 0.55, f"same symbol must serialize; took {elapsed:.2f}s (expected ~0.6s)"


# ── 4. day_realized_pnl under concurrent closes ──────────────────────────────


def test_concurrent_finalize_close_accumulates_pnl_correctly(isi, monkeypatch):
    t = _bare_trader(isi)
    # Stub the file-writing/alert side effects finalize_close performs.
    monkeypatch.setattr(t, "persist", lambda: None)
    monkeypatch.setattr(isi, "alert", lambda *a, **k: None)
    monkeypatch.setattr(isi, "jlog", lambda *a, **k: None)
    # compute_charges is pure; force it to a fixed small cost so expected P&L
    # is exactly computable regardless of the real charge model.
    monkeypatch.setattr(isi, "compute_charges", lambda *a, **k: {"total": 0.0})

    N = 40
    positions = {}
    for i in range(N):
        sym = f"SYM{i}"
        # +10 net each: exit 110 vs entry 100, qty 1, LONG, zero charges.
        positions[sym] = isi.LivePosition(
            symbol=sym, direction="LONG", qty=1, entry_price=100.0, entry_time="10:00",
            initial_sl=95.0, current_sl=95.0, sl_dist=5.0, target_price=None,
            be_triggered=False, sl_order_id="NONE", status="ACTIVE",
        )
    t.positions = dict(positions)

    barrier = threading.Barrier(N)

    def close_one(sym):
        barrier.wait()  # maximize contention on the shared accumulator
        pos = t.positions[sym]
        with t._symbol_lock(sym):
            t.finalize_close(sym, pos, "TARGET", 110.0)

    threads = [threading.Thread(target=close_one, args=(f"SYM{i}",)) for i in range(N)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert t.day_realized_pnl == pytest.approx(N * 10.0), (
        f"lost-update: expected {N*10.0}, got {t.day_realized_pnl}"
    )
    assert all(t.positions[f"SYM{i}"].status == "CLOSED" for i in range(N))


# ── 5. deadlock-freedom of the nested per-symbol -> positions_lock path ───────


def test_nested_lock_path_does_not_deadlock(isi, monkeypatch):
    """finalize_close acquires positions_lock while the caller holds the
    per-symbol lock — exercise that nesting from many threads on distinct
    symbols and require it to complete within a hard timeout."""
    t = _bare_trader(isi)
    monkeypatch.setattr(t, "persist", lambda: None)
    monkeypatch.setattr(isi, "alert", lambda *a, **k: None)
    monkeypatch.setattr(isi, "jlog", lambda *a, **k: None)
    monkeypatch.setattr(isi, "compute_charges", lambda *a, **k: {"total": 0.0})

    N = 25
    for i in range(N):
        sym = f"D{i}"
        t.positions[sym] = isi.LivePosition(
            symbol=sym, direction="SHORT", qty=2, entry_price=200.0, entry_time="10:00",
            initial_sl=205.0, current_sl=205.0, sl_dist=5.0, target_price=None,
            be_triggered=False, sl_order_id="NONE", status="ACTIVE",
        )

    def work(sym):
        pos = t.positions[sym]
        with t._symbol_lock(sym):           # outer (per-symbol)
            t.finalize_close(sym, pos, "SL_HIT", 205.0)  # acquires positions_lock inside

    threads = [threading.Thread(target=work, args=(f"D{i}",)) for i in range(N)]
    for th in threads:
        th.start()
    deadline = time.time() + 10
    for th in threads:
        th.join(timeout=max(0.0, deadline - time.time()))
    assert not any(th.is_alive() for th in threads), "deadlock: threads did not finish within 10s"
