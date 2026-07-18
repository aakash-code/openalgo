"""
Tests for TradeFinderSnapshotLogger in strategies/isi_v56_live_strategy.py.

Audit-only feature: records the TradeFinder Intraday Boost universe across the
day (snapshots.jsonl / changes.jsonl / symbol_lifetimes.json / metadata.json),
writing to disk only when the universe changes, with same-day-restart recovery
and total error isolation (a logging failure must never raise into trading).

Time is controlled by monkeypatching the module's now_ist().
"""

import importlib.util
import json
import site
import sys
from datetime import datetime
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "strategies" / "isi_v56_live_strategy.py"


@pytest.fixture(scope="module")
def isi():
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
    spec = importlib.util.spec_from_file_location("isi_tfsnap_under_test", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def clock(isi, monkeypatch):
    """Controllable now_ist(): tests advance `state['t']`."""
    state = {"t": datetime(2026, 7, 16, 9, 15, 0)}
    monkeypatch.setattr(isi, "now_ist", lambda: state["t"])
    return state


def _read_jsonl(p):
    return [json.loads(line) for line in Path(p).read_text().splitlines() if line.strip()]


def _read_json(p):
    return json.loads(Path(p).read_text())


# ── change detection ─────────────────────────────────────────────────────────


def test_first_record_writes_snapshot_and_change(isi, clock, tmp_path):
    lg = isi.TradeFinderSnapshotLogger(str(tmp_path))
    lg.record(["BEL", "TCS", "SBIN"])

    snaps = _read_jsonl(lg.snapshots_path)
    changes = _read_jsonl(lg.changes_path)
    assert len(snaps) == 1
    assert snaps[0]["symbol_count"] == 3
    assert snaps[0]["symbols"] == ["BEL", "SBIN", "TCS"]  # sorted
    assert len(changes) == 1
    assert changes[0]["added"] == ["BEL", "SBIN", "TCS"]
    assert changes[0]["removed"] == []


def test_identical_list_writes_nothing_new(isi, clock, tmp_path):
    lg = isi.TradeFinderSnapshotLogger(str(tmp_path))
    lg.record(["BEL", "TCS"])
    clock["t"] = datetime(2026, 7, 16, 9, 16, 0)
    lg.record(["TCS", "BEL"])  # same set, different order
    clock["t"] = datetime(2026, 7, 16, 9, 17, 0)
    lg.record(["BEL", "TCS"])

    assert len(_read_jsonl(lg.snapshots_path)) == 1, "unchanged universe must not add snapshots"
    assert len(_read_jsonl(lg.changes_path)) == 1


def test_change_records_added_and_removed(isi, clock, tmp_path):
    lg = isi.TradeFinderSnapshotLogger(str(tmp_path))
    lg.record(["BEL", "TCS"])
    clock["t"] = datetime(2026, 7, 16, 9, 20, 0)
    lg.record(["TCS", "SBIN"])  # -BEL +SBIN

    changes = _read_jsonl(lg.changes_path)
    assert len(changes) == 2
    assert changes[1]["added"] == ["SBIN"]
    assert changes[1]["removed"] == ["BEL"]
    assert len(_read_jsonl(lg.snapshots_path)) == 2


# ── lifetimes ────────────────────────────────────────────────────────────────


def test_lifetime_first_seen_last_seen_and_minutes(isi, clock, tmp_path):
    lg = isi.TradeFinderSnapshotLogger(str(tmp_path))
    lg.record(["BEL"])                                   # 09:15
    clock["t"] = datetime(2026, 7, 16, 9, 20, 0)
    lg.record(["BEL", "TCS"])                            # +5 min for BEL, TCS new
    clock["t"] = datetime(2026, 7, 16, 9, 30, 0)
    lg.record(["BEL", "TCS"])                            # unchanged set (no disk) but +10 min in memory
    lg.flush()                                           # force summaries to disk

    lt = _read_json(lg.lifetimes_path)
    assert lt["BEL"]["first_seen"] == "09:15:00"
    assert lt["BEL"]["last_seen"] == "09:30:00"
    assert lt["BEL"]["appearance_count"] == 1
    # BEL present 09:15->09:20 (+5) then 09:20->09:30 (+10) = 15 min
    assert lt["BEL"]["total_minutes_present"] == 15
    assert lt["TCS"]["first_seen"] == "09:20:00"
    assert lt["TCS"]["total_minutes_present"] == 10  # 09:20 -> 09:30


def test_reappearance_increments_count(isi, clock, tmp_path):
    lg = isi.TradeFinderSnapshotLogger(str(tmp_path))
    lg.record(["BEL"])                                   # appearance 1
    clock["t"] = datetime(2026, 7, 16, 9, 25, 0)
    lg.record(["TCS"])                                   # BEL gone
    clock["t"] = datetime(2026, 7, 16, 10, 0, 0)
    lg.record(["BEL"])                                   # BEL back -> appearance 2
    lg.flush()

    lt = _read_json(lg.lifetimes_path)
    assert lt["BEL"]["appearance_count"] == 2
    assert lt["BEL"]["first_seen"] == "09:15:00"          # unchanged
    assert lt["BEL"]["last_seen"] == "10:00:00"


# ── metadata ─────────────────────────────────────────────────────────────────


def test_metadata_fields(isi, clock, tmp_path):
    lg = isi.TradeFinderSnapshotLogger(str(tmp_path))
    lg.record(["BEL", "TCS"])
    clock["t"] = datetime(2026, 7, 16, 9, 20, 0)
    lg.record(["BEL", "SBIN"])                            # change (unique now BEL,TCS,SBIN)

    md = _read_json(lg.metadata_path)
    assert md["date"] == "2026-07-16"
    assert md["first_snapshot"] == "09:15:00"
    assert md["last_snapshot"] == "09:20:00"
    assert md["total_snapshots"] == 2
    assert md["unique_symbols"] == 3


# ── recovery ─────────────────────────────────────────────────────────────────


def test_restart_does_not_duplicate_snapshot_when_unchanged(isi, clock, tmp_path):
    lg1 = isi.TradeFinderSnapshotLogger(str(tmp_path))
    lg1.record(["BEL", "TCS"])
    assert len(_read_jsonl(lg1.snapshots_path)) == 1

    # Simulate a same-day restart: brand-new logger over the SAME folder.
    clock["t"] = datetime(2026, 7, 16, 9, 21, 0)
    lg2 = isi.TradeFinderSnapshotLogger(str(tmp_path))
    lg2.record(["TCS", "BEL"])                            # identical to pre-restart
    assert len(_read_jsonl(lg2.snapshots_path)) == 1, "restart must not re-snapshot an unchanged universe"

    # A real change after restart still records, and lifetime history persists.
    clock["t"] = datetime(2026, 7, 16, 9, 22, 0)
    lg2.record(["BEL"])                                   # -TCS
    snaps = _read_jsonl(lg2.snapshots_path)
    assert len(snaps) == 2
    lt = _read_json(lg2.lifetimes_path)
    assert lt["BEL"]["first_seen"] == "09:15:00"          # carried across the restart


def test_restart_preserves_first_snapshot_and_snapshot_count(isi, clock, tmp_path):
    lg1 = isi.TradeFinderSnapshotLogger(str(tmp_path))
    lg1.record(["A"])
    clock["t"] = datetime(2026, 7, 16, 9, 30, 0)
    lg1.record(["A", "B"])                                # 2 snapshots so far

    clock["t"] = datetime(2026, 7, 16, 10, 0, 0)
    lg2 = isi.TradeFinderSnapshotLogger(str(tmp_path))    # restart
    lg2.record(["A", "B", "C"])                           # 3rd snapshot
    md = _read_json(lg2.metadata_path)
    assert md["first_snapshot"] == "09:15:00", "true opening time must survive a restart"
    assert md["total_snapshots"] == 3


# ── error isolation ──────────────────────────────────────────────────────────


def test_record_never_raises_on_write_failure(isi, clock, tmp_path, monkeypatch):
    lg = isi.TradeFinderSnapshotLogger(str(tmp_path))
    # Make every disk append explode; record() must swallow it.
    monkeypatch.setattr(lg, "_append", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    lg.record(["BEL", "TCS"])   # must not raise


def test_disabled_logger_is_a_noop(isi, clock, tmp_path, monkeypatch):
    # Force init to fail -> _ready False -> record/flush are silent no-ops.
    monkeypatch.setattr(isi.os, "makedirs", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    lg = isi.TradeFinderSnapshotLogger(str(tmp_path / "x"))
    assert lg._ready is False
    lg.record(["BEL"])   # no raise
    lg.flush()           # no raise
