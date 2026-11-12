"""Owner passcode routes (fork feature, see utils/owner_lock.py).

Registering this blueprint also installs the request guard, so app.py needs
one line and no upstream route changes.
"""

from flask import Blueprint, jsonify, request, session

from database.owner_lock_db import is_passcode_set, set_passcode, verify_passcode
from database.user_db import authenticate_user
from limiter import limiter
from utils.logging import get_logger
from utils.owner_lock import (
    UNLOCK_SECONDS,
    guard_request,
    is_owner_unlocked,
    lockout_message,
    lockout_remaining,
    mark_locked,
    mark_unlocked,
    record_failure,
    record_success,
    unlock_expires_in,
)
from utils.session import check_session_validity

logger = get_logger(__name__)

owner_lock_bp = Blueprint("owner_lock", __name__, url_prefix="/owner-lock")

MIN_LENGTH = 6
MAX_LENGTH = 128


@owner_lock_bp.before_app_request
def _owner_lock_guard():
    return guard_request()


def _payload() -> dict:
    return request.get_json(silent=True) or request.form.to_dict() or {}


def _error(message: str, status: int = 400):
    return jsonify({"status": "error", "message": message}), status


def _check_passcode(code: str):
    """Verify against the stored passcode, counting wrong ones across all IPs.

    Returns None when it is correct, else the error response to send. While a
    lockout runs nothing is verified, so guesses made during it cost nothing
    and reveal nothing.
    """
    waiting = lockout_remaining()
    if waiting:
        return _error(lockout_message(waiting), 429)
    if verify_passcode(code):
        record_success()
        return None
    started = record_failure()
    if started:
        logger.warning("Owner passcode: too many wrong passcodes, entry paused for %ss", started)
        return _error(lockout_message(started), 429)
    logger.warning("Owner passcode: wrong passcode entered")
    return _error("That passcode is not correct.")


@owner_lock_bp.route("/", methods=["GET"], strict_slashes=False)
def page():
    """The React settings page. Registered here, not in react_app.py, so the
    fork adds no line to an upstream file; a known route is not counted
    against a logged-out visitor's IP."""
    from blueprints.react_app import serve_react_app

    return serve_react_app()


@owner_lock_bp.route("/status", methods=["GET"])
@check_session_validity
def status():
    enabled = is_passcode_set()
    return jsonify(
        {
            "status": "success",
            "enabled": enabled,
            "unlocked": is_owner_unlocked(),
            "expires_in": unlock_expires_in() if enabled else 0,
            "unlock_minutes": UNLOCK_SECONDS // 60,
        }
    )


@owner_lock_bp.route("/unlock", methods=["POST"])
@check_session_validity
@limiter.limit("5 per 15 minutes")
def unlock():
    if not is_passcode_set():
        return jsonify({"status": "success", "unlocked": True})
    refused = _check_passcode(str(_payload().get("passcode", "")))
    if refused is not None:
        return refused
    mark_unlocked()
    return jsonify({"status": "success", "unlocked": True, "expires_in": unlock_expires_in()})


@owner_lock_bp.route("/lock", methods=["POST"])
@check_session_validity
def lock():
    mark_locked()
    return jsonify({"status": "success", "unlocked": not is_passcode_set()})


@owner_lock_bp.route("/passcode", methods=["POST"])
@check_session_validity
@limiter.limit("5 per 15 minutes")
def change_passcode():
    """Set, change or remove the passcode.

    Setting the first one needs the account login password. Changing or
    removing it needs the current passcode, so someone with only the login
    cannot replace it.
    """
    data = _payload()
    new = str(data.get("new_passcode", "") or "")
    remove = bool(data.get("remove"))

    if is_passcode_set():
        refused = _check_passcode(str(data.get("current_passcode", "")))
        if refused is not None:
            return refused
    else:
        if not authenticate_user(session.get("user"), str(data.get("login_password", ""))):
            return _error("Your login password is not correct.")

    if remove:
        set_passcode(None)
        mark_locked()
        logger.info("Owner passcode removed")
        return jsonify({"status": "success", "enabled": False})

    if not (MIN_LENGTH <= len(new) <= MAX_LENGTH):
        return _error(f"Choose a passcode of at least {MIN_LENGTH} characters.")

    # The point of the passcode is that someone holding the login does not
    # have it, so it may not be the login password.
    if authenticate_user(session.get("user"), new):
        return _error("Choose a passcode that is different from your OpenAlgo login password.")

    set_passcode(new)
    mark_unlocked()
    logger.info("Owner passcode set")
    return jsonify({"status": "success", "enabled": True, "expires_in": unlock_expires_in()})
