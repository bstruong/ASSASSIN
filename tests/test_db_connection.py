"""Tests for database connection and initialization utilities."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.db.connection import (
    DEFAULT_DATABASE_URL,
    get_connection,
    get_database_url,
    get_db_connection,
    init_db,
)


def test_get_database_url_override():
    assert (
        get_database_url("postgresql://custom:123/db") == "postgresql://custom:123/db"
    )


def test_get_database_url_fallback(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    assert get_database_url() == DEFAULT_DATABASE_URL


def test_get_db_connection_context_manager(postgres_url: str):
    with get_db_connection(postgres_url) as conn:
        result = conn.execute("SELECT 1;").fetchone()
        assert result == (1,)

    standalone_conn = get_connection(postgres_url)
    try:
        init_db(standalone_conn)
    finally:
        standalone_conn.close()


def test_get_db_connection_rollback_on_error(postgres_url: str):
    with (
        pytest.raises(RuntimeError),
        get_db_connection(postgres_url) as conn,
    ):
        conn.execute("CREATE TEMPORARY TABLE test_rollback (val INT);")
        raise RuntimeError("Force rollback")


def test_init_db_without_explicit_conn(postgres_url: str):
    init_db(url=postgres_url)


def test_init_db_missing_schema_raises(monkeypatch):
    nonexistent = Path("/tmp/nonexistent_schema.sql")
    with (
        patch("app.db.connection.SCHEMA_PATH", nonexistent),
        pytest.raises(FileNotFoundError, match="Schema file not found"),
    ):
        init_db()
