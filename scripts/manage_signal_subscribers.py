#!/usr/bin/env python
"""Create, list and revoke signal-feed subscribers.

There is no admin UI: subscribers are a handful of B2B consumers, added by hand.

    python scripts/manage_signal_subscribers.py list
    python scripts/manage_signal_subscribers.py create "Acme Platform" --webhook https://acme.example/hooks/signals
    python scripts/manage_signal_subscribers.py revoke 3

The API key and HMAC secret are printed ONCE at creation and are not
recoverable — the key is stored only as an argon2 hash. To re-issue, revoke and
create a new subscriber.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.signal_db import (  # noqa: E402
    SignalSubscriber,
    create_subscriber,
    db_session,
    init_db,
    revoke_subscriber,
)


def cmd_create(args) -> int:
    api_key, hmac_secret = create_subscriber(args.name, args.webhook)
    print(f"\nSubscriber created: {args.name}")
    print(f"  webhook : {args.webhook or '(pull-only)'}")
    print("\n  Store these now — they are not recoverable:\n")
    print(f"  API key     : {api_key}")
    print(f"  HMAC secret : {hmac_secret}")
    print("\nUsage:")
    print(f'  curl -H "Authorization: Bearer {api_key}" \\')
    print('       "http://127.0.0.1:5000/signals/v1/events?since_id=0&limit=50"\n')
    if args.webhook:
        print("Webhook requests carry X-Signal-Timestamp and X-Signal-Signature;")
        print('verify with HMAC-SHA256 over "<timestamp>.<raw body>" using the secret.\n')
    return 0


def cmd_list(_args) -> int:
    rows = db_session.query(SignalSubscriber).order_by(SignalSubscriber.id).all()
    if not rows:
        print("No subscribers.")
        return 0
    print(f"{'id':>4}  {'name':<28} {'active':<7} {'webhook'}")
    for r in rows:
        state = "yes" if (r.active and r.revoked_at is None) else "REVOKED"
        print(f"{r.id:>4}  {r.name[:28]:<28} {state:<7} {r.webhook_url or '(pull-only)'}")
    return 0


def cmd_revoke(args) -> int:
    if revoke_subscriber(args.id):
        print(f"Subscriber {args.id} revoked — its key stops working immediately.")
        return 0
    print(f"No subscriber with id {args.id}.")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="create a subscriber")
    p_create.add_argument("name")
    p_create.add_argument("--webhook", default=None, help="URL to push signals to")
    p_create.set_defaults(func=cmd_create)

    sub.add_parser("list", help="list subscribers").set_defaults(func=cmd_list)

    p_revoke = sub.add_parser("revoke", help="revoke a subscriber")
    p_revoke.add_argument("id", type=int)
    p_revoke.set_defaults(func=cmd_revoke)

    args = parser.parse_args()
    init_db()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
