"""End a dev server stopped by a signal once its own cleanup is done.

Every Ctrl+C on 19-Oct-2026 ended in ``Fatal Python error: gilstate_tss_set:
failed to set current tstate (TSS)`` and a macOS crash report. The crash comes
after everything OpenAlgo does at exit: the C library's ``exit()`` runs C++
static destructors, and DuckDB's ``PythonImportCache`` destructor takes a
Python thread state in an interpreter that is already gone (duckdb-python
issue 601, fix not released). It turned an ordinary stop into an abort.

So a stop that :mod:`utils.shutdown` began from a signal ends with
``os._exit`` as the very last exit hook: every other hook has run, Python's
threads have been joined, logs are flushed, and the exit code is the signal's.
Only the interpreter's own teardown and the native destructors are skipped,
which hold nothing of OpenAlgo's.

Imported first in ``app.py``, because ``atexit`` runs hooks in reverse order of
registration and this one must run after all of them. Any other exit (an
error, a normal return, gunicorn stopping a worker) is left alone: the signal
handler is the only caller of :func:`exit_after_cleanup`, and gunicorn never
installs it. Remove this module once a DuckDB release carries the fix.
"""

import atexit
import os
import sys

_exit_code: int | None = None


def exit_after_cleanup(code: int) -> None:
    """Ask for the process to end with ``code`` after every exit hook has run."""
    global _exit_code
    _exit_code = code


def _end_now() -> None:
    if _exit_code is None:
        return
    try:
        import logging

        logging.shutdown()
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass  # ending is the point; a flush that fails must not stop it
    os._exit(_exit_code)


atexit.register(_end_now)
