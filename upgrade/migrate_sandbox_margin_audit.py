#!/usr/bin/env python
"""
Sandbox Margin Audit Migration Script

Adds margin audit columns for analyzer-mode broker margin tracking.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text

from utils.logging import get_logger

logger = get_logger(__name__)

MIGRATION_NAME = "sandbox_margin_audit_columns"
MIGRATION_VERSION = "001"

parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(parent_dir, ".env"))


def get_engine():
    database_url = os.getenv("SANDBOX_DATABASE_URL", "sqlite:///db/sandbox.db")

    if database_url.startswith("sqlite:///"):
        db_path = database_url.replace("sqlite:///", "")
        if not os.path.isabs(db_path):
            db_path = os.path.join(parent_dir, db_path)
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        database_url = f"sqlite:///{db_path}"

    return create_engine(database_url)


TABLE_COLUMNS = {
    "sandbox_orders": [
        ("margin_source", "VARCHAR(30)"),
        ("margin_snapshot", "TEXT"),
    ],
    "sandbox_positions": [
        ("margin_source", "VARCHAR(30)"),
        ("margin_snapshot", "TEXT"),
    ],
}


def upgrade():
    try:
        logger.info(f"Starting migration: {MIGRATION_NAME} (v{MIGRATION_VERSION})")
        engine = get_engine()
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())

        with engine.connect() as conn:
            added = 0
            for table_name, columns in TABLE_COLUMNS.items():
                if table_name not in table_names:
                    continue

                existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
                for column_name, column_type in columns:
                    if column_name in existing_columns:
                        logger.info(f"Column already exists: {table_name}.{column_name}")
                        continue

                    conn.execute(
                        text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
                    )
                    logger.info(f"Added column: {table_name}.{column_name}")
                    added += 1

            conn.commit()

        if added > 0:
            logger.info(f"Migration {MIGRATION_NAME} completed: added {added} column(s)")
        else:
            logger.info(f"Migration {MIGRATION_NAME}: all columns already exist")
        return True
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        return False


def status():
    try:
        logger.info(f"Checking status of migration: {MIGRATION_NAME}")
        engine = get_engine()
        inspector = inspect(engine)

        missing = []
        for table_name, columns in TABLE_COLUMNS.items():
            if table_name not in inspector.get_table_names():
                missing.extend(f"{table_name}.{column_name}" for column_name, _ in columns)
                continue

            existing_columns = {col["name"] for col in inspector.get_columns(table_name)}
            for column_name, _ in columns:
                if column_name not in existing_columns:
                    missing.append(f"{table_name}.{column_name}")

        if missing:
            logger.info(f"Missing columns: {', '.join(missing)} - migration needed")
            return False

        logger.info("All sandbox margin audit columns exist")
        return True
    except Exception as e:
        logger.error(f"Status check failed: {e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=f"Migration: {MIGRATION_NAME} (v{MIGRATION_VERSION})",
    )
    parser.add_argument("--status", action="store_true", help="Check migration status")

    args = parser.parse_args()
    success = status() if args.status else upgrade()
    sys.exit(0 if success else 1)
