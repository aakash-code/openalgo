"""A stopped dev server ends after its own cleanup, before C++ static destructors.

Every Ctrl+C on 19-Oct-2026 ended in ``Fatal Python error: gilstate_tss_set``
and a macOS crash report: DuckDB's ``PythonImportCache`` destructor, a C++
static run by the C library's ``exit()``, takes a Python thread state after the
interpreter has gone (duckdb-python #601, open). Run in a subprocess because
it is about how the process ends.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = textwrap.dedent(
    """
    import atexit, sys
    sys.path.insert(0, {root!r})
    out = {out!r}

    def after():                              # registered before: runs after it
        open(out + ".teardown", "w").write("ran")

    atexit.register(after)
    import utils.exit_after_cleanup as eac   # as app.py imports it, first

    def cleanup():                            # an exit hook registered later
        open(out + ".atexit", "w").write("ran")

    atexit.register(cleanup)
    import duckdb
    duckdb.connect().execute("select 1").fetchall()
    if {signal_path}:
        eac.exit_after_cleanup(130)
    raise SystemExit(130)
    """
)


def _run(tmp_path, signal_path):
    out = str(tmp_path / "marker")
    code = SCRIPT.format(root=str(ROOT), out=out, signal_path=signal_path)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=60)
    return proc.returncode, Path(out + ".atexit").exists(), Path(out + ".teardown").exists()


def test_a_signal_stop_keeps_its_code_and_cleanup_and_skips_native_teardown(tmp_path):
    code, cleaned, torn_down = _run(tmp_path, signal_path=True)
    assert code == 130
    assert cleaned, "an exit hook registered after it must still run"
    assert not torn_down, "nothing may run after it: that is where the native destructors are"


def test_any_other_exit_is_left_alone(tmp_path):
    code, cleaned, torn_down = _run(tmp_path, signal_path=False)
    assert (code, cleaned, torn_down) == (130, True, True)
