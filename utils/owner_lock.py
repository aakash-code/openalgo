"""Owner passcode lock (fork feature).

A deployment is single-user, but the owner may hand the login to someone who
should only watch results, such as a demo customer. With a passcode set, the
Python strategy host (code, logs, start, stop, delete, schedule), the API key
page and password change are refused until the passcode is entered in this
browser session. The unlock lasts ``UNLOCK_SECONDS`` and ends at logout,
because logout clears the session.

The guard is one ``before_app_request`` hook matching paths, rather than a
decorator on each upstream route, so upstream files stay untouched and
``git merge upstream/main`` does not conflict. A new upstream route under
``/python/`` is locked by default; only the read-only paths below stay open.

No passcode set means nothing is locked.
"""

import re
import time

from flask import jsonify, redirect, request, session

from database.owner_lock_db import is_passcode_set
from utils import real_threading

UNLOCK_SECONDS = 30 * 60
SESSION_KEY = "owner_unlocked_until"
LOCKED_MESSAGE = "This is locked by the owner. Enter the owner passcode to continue."

# Read-only /python endpoints a watcher may use: the list, live status and the
# event stream. None of them returns code.
_OPEN_PYTHON_PATHS = {
    "/python",
    "/python/",
    "/python/status",
    "/python/api/strategies",
    "/python/api/events",
    "/python/api/exchanges",
}
_OPEN_PYTHON_PATTERNS = (re.compile(r"^/python/api/strategy/[^/]+/?$"),)

# React page shells. A browser GET for HTML only loads the app; the page's own
# data calls are what the guard refuses, and the page then shows the lock.
_PAGE_PATTERNS = (
    re.compile(r"^/python/(new|guide)/?$"),
    re.compile(r"^/python/[^/]+/(edit|logs|schedule)/?$"),
    re.compile(r"^/apikey/?$"),
)

_LOCKED_EXACT = {"/apikey", "/apikey/", "/apikey/mode", "/auth/change", "/auth/change-password"}


# Wrong passcodes counted across every IP. The per-IP rate limit alone let a
# watcher who can switch networks (mobile data, Wi-Fi, a VPN) try five more
# passcodes on each one. After FAIL_LIMIT wrong tries in a row from anywhere,
# every passcode check is refused for a lockout that doubles each time, up to
# a day. A correct passcode clears it. Kept in memory: a watcher cannot
# restart the server, and a restart by the owner clearing it is harmless.
FAIL_LIMIT = 5
LOCKOUT_FIRST_SECONDS = 15 * 60
LOCKOUT_MAX_SECONDS = 24 * 3600

_attempts_lock = real_threading.Lock()
_attempts = {"failures": 0, "lockouts": 0, "locked_until": 0.0}


def lockout_remaining() -> int:
    """Seconds until passcode checks are accepted again, 0 when they are."""
    with _attempts_lock:
        left = _attempts["locked_until"] - time.time()
    return max(int(left + 0.999), 0)


def record_failure() -> int:
    """Count a wrong passcode. Returns the lockout it started, 0 if none."""
    with _attempts_lock:
        _attempts["failures"] += 1
        if _attempts["failures"] < FAIL_LIMIT:
            return 0
        seconds = min(LOCKOUT_FIRST_SECONDS * (2 ** _attempts["lockouts"]), LOCKOUT_MAX_SECONDS)
        _attempts["lockouts"] += 1
        _attempts["failures"] = 0
        _attempts["locked_until"] = time.time() + seconds
        return int(seconds)


def record_success() -> None:
    with _attempts_lock:
        _attempts.update(failures=0, lockouts=0, locked_until=0.0)


def lockout_message(seconds: int) -> str:
    minutes = max(1, (seconds + 59) // 60)
    if minutes >= 120:
        wait = f"{(minutes + 59) // 60} hours"
    else:
        wait = f"{minutes} minutes"
    return (
        "Too many wrong passcodes. Passcode entry is paused for everyone for "
        f"{wait}. The owner can wait it out; no settings need changing."
    )


def unlock_expires_in() -> int:
    """Seconds left on this session's unlock, 0 when locked."""
    try:
        left = int(float(session.get(SESSION_KEY) or 0) - time.time())
    except (TypeError, ValueError):
        return 0
    return max(left, 0)


def is_owner_unlocked() -> bool:
    if not is_passcode_set():
        return True
    return unlock_expires_in() > 0


def mark_unlocked() -> None:
    session[SESSION_KEY] = time.time() + UNLOCK_SECONDS


def mark_locked() -> None:
    session.pop(SESSION_KEY, None)


def _is_html_page_load(path: str) -> bool:
    if request.method != "GET":
        return False
    if request.headers.get("Accept", "").startswith("application/json"):
        return False
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return False
    return any(p.match(path) for p in _PAGE_PATTERNS)


def path_is_locked(path: str) -> bool:
    """True when ``path`` belongs to the owner-only surface."""
    if path in _LOCKED_EXACT:
        return True
    if path == "/python" or path.startswith("/python/"):
        if path in _OPEN_PYTHON_PATHS:
            return False
        return not any(p.match(path) for p in _OPEN_PYTHON_PATTERNS)
    return False


def guard_request():
    """before_app_request hook. Returns a response to refuse, else None."""
    path = request.path
    if not path_is_locked(path) or _is_html_page_load(path):
        return None
    if not session.get("logged_in"):
        return None  # the route's own login check answers this
    if is_owner_unlocked():
        return None
    if (
        request.method == "GET"
        and request.accept_mimetypes.accept_html
        and not (
            request.headers.get("Accept", "").startswith("application/json")
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        )
    ):
        return redirect("/python")
    return jsonify({"status": "error", "error": "owner_locked", "message": LOCKED_MESSAGE}), 403
