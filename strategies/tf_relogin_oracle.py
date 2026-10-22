#!/usr/bin/env python3
"""
Headless TradeFinder re-login for a server with no display (the OCI box).

The browser-based paths (tf_login_setup.py, the keepalive's headed fallback)
both open a visible Chromium window, which a headless server cannot do. When
TradeFinder logs the server's session out, this is how you hand it a fresh JWT
without a browser on the box:

  1. On your own laptop, log in at https://tradefinder.in/home
  2. DevTools -> Application -> Local Storage -> key `tradefinder_token`
  3. Copy its value and run this on the server, pasting when prompted:

        uv run python strategies/tf_relogin_oracle.py

     or in one line (history-safe only on a trusted box):

        uv run python strategies/tf_relogin_oracle.py "<paste-jwt>"

It sanitises the paste (strips quotes and a leading `tradefinder_token=` or
`Bearer `), refuses a malformed or already-expired token, writes it to the
canonical TF_JWT_FILE, then verifies it against the live sector_scope endpoint
so you know it actually works before you walk away.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)                         # so `import tf_auth` works
sys.path.insert(0, os.path.dirname(_HERE))        # repo root, for `services.*`

from tf_auth import TF_JWT_FILE, _write_file_jwt, jwt_expiry_seconds  # noqa: E402


def _clean(raw: str) -> str:
    """Strip the wrappers a copy-paste usually drags along."""
    s = raw.strip().strip("'\"").strip()
    for prefix in ("tradefinder_token=", "Bearer ", "bearer "):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()
    return s


def _looks_like_jwt(jwt: str) -> bool:
    return jwt.count(".") == 2 and all(part for part in jwt.split("."))


def _days(seconds: float) -> str:
    if seconds <= 0:
        return "expired"
    if seconds >= 86400:
        return f"{seconds / 86400:.1f}d"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 60:.0f}m"


def main() -> int:
    raw = sys.argv[1] if len(sys.argv) > 1 else ""
    if not raw:
        if sys.stdin.isatty():
            print("Paste the TradeFinder JWT (tradefinder_token) and press Enter:")
        raw = sys.stdin.readline()

    jwt = _clean(raw)
    if not _looks_like_jwt(jwt):
        print("error: that does not look like a JWT (expected three dot-separated parts).")
        return 1

    left = jwt_expiry_seconds(jwt)
    if left <= 0:
        print("error: that token is already expired (or has no readable expiry). "
              "Grab a fresh one from tradefinder.in and try again.")
        return 1

    _write_file_jwt(jwt)
    print(f"wrote {TF_JWT_FILE} (expires in {_days(left)})")

    # Verify against the live endpoint, same call the app makes, so a token that
    # is well-formed but rejected is caught here rather than in the UI later.
    try:
        from services.tradefinder_service import fetch_sector_scope
    except Exception as e:
        print(f"note: wrote the token but could not import the verifier ({e}); "
              "restart the app and check the TradeFinder panel.")
        return 0

    result = fetch_sector_scope()
    if result is None:
        print("warning: token written but the live sector_scope call still failed. "
              "Check log/app_stdout.log for the reason (a 401 means TradeFinder "
              "rejected this token; a timeout means the server cannot reach "
              "tradefinder.in).")
        return 2

    sectors = len(result.get("sectors") or {})
    print(f"verified: sector_scope returned {sectors} sectors. TradeFinder is working.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
