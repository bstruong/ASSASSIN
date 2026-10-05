"""Database connection management and initialization for PostgreSQL."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg

logger = logging.getLogger(__name__)

DEFAULT_DATABASE_URL = "postgresql://postgres:assassin@localhost:54329/assassin_test"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_database_url(override_url: str | None = None) -> str:
    """Return database connection URL from override, environment, or default."""
    if override_url:
        return override_url
    return (
        os.environ.get("DATABASE_URL")
        or os.environ.get("TEST_DATABASE_URL")
        or DEFAULT_DATABASE_URL
    )


def get_connection(url: str | None = None) -> psycopg.Connection:
    """Open and return a PostgreSQL connection."""
    target_url = get_database_url(url)
    logger.info(
        "Opening database connection",
        extra={"database_host": target_url.split("@")[-1]},
    )
    return psycopg.connect(target_url)


@contextmanager
def get_db_connection(url: str | None = None) -> Iterator[psycopg.Connection]:
    """Context manager yielding a connection, committing on success or rolling back on error."""
    conn = get_connection(url)
    try:
        with conn.transaction():
            yield conn
    except Exception as exc:
        logger.error("Transaction rolled back due to error", extra={"error": str(exc)})
        raise
    finally:
        conn.close()


def init_db(conn: psycopg.Connection | None = None, url: str | None = None) -> None:
    """Initialize schema tables, constraints, and indexes from schema.sql."""
    if not SCHEMA_PATH.is_file():
        raise FileNotFoundError(f"Schema file not found at {SCHEMA_PATH}")

    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    if conn is not None:
        logger.info("Executing schema DDL against provided connection")
        conn.execute(ddl)
        conn.commit()
    else:
        logger.info("Executing schema DDL against managed connection")
        with get_db_connection(url) as connection:
            connection.execute(ddl)
