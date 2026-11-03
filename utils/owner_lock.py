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
