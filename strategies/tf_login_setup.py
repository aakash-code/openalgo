#!/usr/bin/env python3
"""
ONE-TIME setup: sign into TradeFinder (via Google) in a persistent browser profile.

Run this once. A real Chromium window opens — log in with Google
(sheladiyaaakash123@gmail.com) and wait until you land on tradefinder.in/home.
The Google + NextAuth session is saved into strategies/.tf_browser_profile/, after
which tf_auth.refresh_tf_jwt() can refresh the JWT headlessly and unattended.

Usage:
    uv run python strategies/tf_login_setup.py
"""

import os
import sys

from tf_auth import (
    TF_HOME_URL,
    TF_JWT_FILE,
    TF_LS_KEY,
    TF_PROFILE_DIR,
    _write_file_jwt,
    jwt_expiry_seconds,
)


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"playwright not installed: {e}\n  run: uv add playwright && uv run playwright install chromium")
        return 1

    os.makedirs(TF_PROFILE_DIR, exist_ok=True)
    print("=" * 70)
    print("  TradeFinder one-time Google login")
    print("=" * 70)
    print(f"  Profile dir : {TF_PROFILE_DIR}")
    print("  A browser window will open.")
    print("  1) Click 'Login with Google'")
    print("  2) Choose sheladiyaaakash123@gmail.com")
    print("  3) Wait until you see the TradeFinder dashboard (/home)")
    print("  4) Return here and press ENTER")
    print("=" * 70)

    wait_s = int(os.getenv("TF_LOGIN_WAIT_S", "240"))   # how long to wait for you to log in
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            TF_PROFILE_DIR, headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(TF_HOME_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass

        # Poll localStorage for the `lt` token. As soon as you finish the Google login
        # and land on /home, the token appears and we capture it — no ENTER needed.
        # (If a real terminal is attached you can still just wait; this loop ends early.)
        print(f"\nWaiting up to {wait_s}s for you to finish the Google login in the browser window...")
        import time as _t
        jwt = None
        deadline = _t.time() + wait_s
        while _t.time() < deadline:
            try:
                jwt = page.evaluate(f"() => window.localStorage.getItem('{TF_LS_KEY}')")
            except Exception:
                jwt = None
            if jwt and jwt_expiry_seconds(jwt) > 60:
                break
            _t.sleep(3)
        ctx.close()

    if jwt and jwt_expiry_seconds(jwt) > 60:
        _write_file_jwt(jwt)
        print(f"\n✅ Login captured. JWT saved to {TF_JWT_FILE} "
              f"(expires in {jwt_expiry_seconds(jwt)/60:.0f} min).")
        print("   Headless auto-refresh is now ready: tf_auth.refresh_tf_jwt()")
        return 0

    print("\n⚠ Could not read the `lt` token. Make sure you reached /home before pressing ENTER.")
    print("   The profile is still saved; you can re-run this script to retry.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
