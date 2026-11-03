#!/usr/bin/env python3
"""
Migration: Owner passcode lock (fork feature)

Adds the owner_lock table that holds the Argon2 hash of the owner passcode
(see utils/owner_lock.py). A new table: nothing to backfill, no existing
value to preserve. init_db() in database/owner_lock_db.py creates it too.

Idempotent - safe to run multiple times.

Usage:
    cd upgrade
    uv run migrate_owner_lock.py           # Apply migration
    uv run migrate_owner_lock.py --status  # Report status without changing anything
"""

import argparse
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
import _pragmas  # noqa: F401,E402
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

TABLE = "owner_lock"


def get_database_url():
    from dotenv import load_dotenv

    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
    url = os.getenv("DATABASE_URL", "sqlite:///db/openalgo.db")
    prefix = "sqlite:///"
    if url.startswith(prefix):
        path = url[len(prefix) :]
        if path and path != ":memory:" and not os.path.isabs(path):
            url = prefix + os.path.join(PROJECT_ROOT, path)
    return url


def table_exists(engine):
    return TABLE in inspect(engine).get_table_names()


def main():
    parser = argparse.ArgumentParser(description="Owner passcode lock table migration")
    parser.add_argument(
        "--status", action="store_true", help="Report status without changing anything"
    )
    args = parser.parse_args()

    print("\nOwner Passcode Lock Migration")
    print("-" * 46)
    try:
        url = get_database_url()
        engine = create_engine(url, poolclass=NullPool) if "sqlite" in url else create_engine(url)

        if args.status:
            present = table_exists(engine)
            print(f"  {TABLE:<34} {'present' if present else 'MISSING'}")
            print("All changes applied." if present else "Migration needed.")
            return 0 if present else 1

        if table_exists(engine):
            print(f"  [SKIP] {TABLE} table already exists")
            return 0

        pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if "sqlite" in url else "SERIAL PRIMARY KEY"
        with engine.connect() as conn:
            conn.execute(text(f"CREATE TABLE {TABLE} (id {pk}, passcode_hash TEXT)"))
            conn.commit()
        print(f"  [OK] {TABLE} table created")
        return 0
    except Exception as e:
        print(f"\n[ERROR] Migration failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
