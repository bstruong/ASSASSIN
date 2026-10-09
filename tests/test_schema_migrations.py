"""Ordered schema migrations: fresh apply, no-op replay, and loud failures."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from app.db.connection import SCHEMA_PATH, init_db
from app.db.migrate import (
    DEFAULT_MIGRATIONS_DIR,
    apply_migrations,
    discover_migrations,
    migration_checksum,
)
from app.models.exceptions import MigrationError

BASELINE_SQL = "CREATE TABLE baseline_probe (probe_id BIGINT PRIMARY KEY);\n"
STEP_SQL = "CREATE TABLE step_probe (step_id BIGINT PRIMARY KEY);\n"


def _swap_database(database_url: str, database: str) -> str:
    parts = urlsplit(database_url)
    return urlunsplit(parts._replace(path=f"/{database}"))


@pytest.fixture
def empty_database_url(postgres_url: str) -> Iterator[str]:
    """Create a private database and drop it after the test."""
    admin_url = _swap_database(postgres_url, "postgres")
    name = f"assassin_mig_{uuid4().hex[:12]}"
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = _swap_database(postgres_url, name)
    try:
        yield url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
                """,
                (name,),
            )
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name))
            )


def _write_migration(directory: Path, name: str, ddl: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(ddl, encoding="utf-8")
    return path


def _column_type(conn: psycopg.Connection, table: str, column: str) -> str | None:
    row = conn.execute(
        """
        SELECT data_type
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s AND column_name = %s
        """,
        (table, column),
    ).fetchone()
    if row is None:
        return None
    return str(row[0])


def _ledger(conn: psycopg.Connection) -> list[tuple[str, str, object]]:
    return list(
        conn.execute(
            """
            SELECT version, checksum, applied_at
            FROM schema_migrations
            ORDER BY version
            """
        ).fetchall()
    )


def test_packaged_migrations_start_at_baseline_snapshot() -> None:
    discovered = discover_migrations(SCHEMA_PATH, DEFAULT_MIGRATIONS_DIR)
    assert [item.version for item in discovered] == ["0001"]
    assert discovered[0].path == SCHEMA_PATH
    assert discovered[0].path.is_file()


def test_fresh_apply_reaches_holdings_with_integer_cents(
    empty_database_url: str,
) -> None:
    with psycopg.connect(empty_database_url) as conn:
        applied = apply_migrations(conn)
        conn.commit()
        assert applied == ["0001"]
        assert _column_type(conn, "holdings", "market_value_cents") == "bigint"
        assert _column_type(conn, "statements", "opening_balance_cents") == "bigint"
        ledger = _ledger(conn)
        assert len(ledger) == 1
        assert ledger[0][0] == "0001"
        assert ledger[0][1] == migration_checksum(SCHEMA_PATH)


def test_init_db_on_fresh_database_uses_migration_runner(
    empty_database_url: str,
) -> None:
    init_db(url=empty_database_url)
    with psycopg.connect(empty_database_url) as conn:
        assert _column_type(conn, "holdings", "quantity_nanos") == "bigint"
        assert [row[0] for row in _ledger(conn)] == ["0001"]

    standalone = psycopg.connect(empty_database_url)
    try:
        init_db(standalone)
        assert [row[0] for row in _ledger(standalone)] == ["0001"]
    finally:
        standalone.close()


def test_second_apply_is_a_noop(empty_database_url: str) -> None:
    with psycopg.connect(empty_database_url) as conn:
        assert apply_migrations(conn) == ["0001"]
        conn.commit()
        before = _ledger(conn)
        conn.execute("DROP TABLE holdings")
        conn.commit()
        assert apply_migrations(conn) == []
        conn.commit()
        assert _ledger(conn) == before
        assert _column_type(conn, "holdings", "holding_id") is None


def test_tampered_checksum_fails_loudly(empty_database_url: str) -> None:
    with psycopg.connect(empty_database_url) as conn:
        apply_migrations(conn)
        conn.commit()
        conn.execute(
            "UPDATE schema_migrations SET checksum = %s WHERE version = %s",
            ("0" * 64, "0001"),
        )
        conn.commit()
        with pytest.raises(MigrationError, match="checksum mismatch"):
            apply_migrations(conn)
        conn.rollback()
        stored = conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version = %s",
            ("0001",),
        ).fetchone()
        assert stored == ("0" * 64,)


def test_missing_recorded_version_fails(empty_database_url: str) -> None:
    with psycopg.connect(empty_database_url) as conn:
        apply_migrations(conn)
        conn.commit()
        conn.execute(
            """
            INSERT INTO schema_migrations (version, checksum)
            VALUES (%s, %s)
            """,
            ("0002", "a" * 64),
        )
        conn.commit()
        with pytest.raises(MigrationError, match="missing"):
            apply_migrations(conn)


def test_gap_in_migration_files_fails(empty_database_url: str, tmp_path: Path) -> None:
    baseline = _write_migration(tmp_path, "baseline.sql", BASELINE_SQL)
    migrations = tmp_path / "migrations"
    _write_migration(migrations, "0003_gap.sql", STEP_SQL)
    with psycopg.connect(empty_database_url) as conn:
        with pytest.raises(MigrationError, match="out of order"):
            apply_migrations(conn, baseline_path=baseline, migrations_dir=migrations)
        conn.rollback()
        missing = conn.execute(
            "SELECT to_regclass('public.schema_migrations')"
        ).fetchone()
        assert missing == (None,)


def test_later_migration_applies_once(empty_database_url: str, tmp_path: Path) -> None:
    baseline = _write_migration(tmp_path, "baseline.sql", BASELINE_SQL)
    migrations = tmp_path / "migrations"
    step = _write_migration(migrations, "0002_step.sql", STEP_SQL)
    with psycopg.connect(empty_database_url) as conn:
        assert apply_migrations(
            conn, baseline_path=baseline, migrations_dir=migrations
        ) == ["0001", "0002"]
        conn.commit()
        assert _column_type(conn, "step_probe", "step_id") == "bigint"
        conn.execute("DROP TABLE step_probe")
        conn.commit()
        assert (
            apply_migrations(conn, baseline_path=baseline, migrations_dir=migrations)
            == []
        )
        conn.commit()
        assert _column_type(conn, "step_probe", "step_id") is None
        stored = conn.execute(
            "SELECT checksum FROM schema_migrations WHERE version = %s",
            ("0002",),
        ).fetchone()
        assert stored == (migration_checksum(step),)


def test_applied_versions_must_be_a_prefix(
    empty_database_url: str, tmp_path: Path
) -> None:
    baseline = _write_migration(tmp_path, "baseline.sql", BASELINE_SQL)
    migrations = tmp_path / "migrations"
    _write_migration(migrations, "0002_step.sql", STEP_SQL)
    _write_migration(
        migrations,
        "0003_later.sql",
        "CREATE TABLE later_probe (later_id BIGINT PRIMARY KEY);\n",
    )
    with psycopg.connect(empty_database_url) as conn:
        apply_migrations(conn, baseline_path=baseline, migrations_dir=migrations)
        conn.commit()
        conn.execute("DELETE FROM schema_migrations WHERE version = %s", ("0002",))
        conn.commit()
        with pytest.raises(MigrationError, match="out of order"):
            apply_migrations(conn, baseline_path=baseline, migrations_dir=migrations)


def test_unexpected_migration_filename_fails(tmp_path: Path) -> None:
    baseline = _write_migration(tmp_path, "baseline.sql", BASELINE_SQL)
    migrations = tmp_path / "migrations"
    _write_migration(migrations, "notes.sql", STEP_SQL)
    with pytest.raises(MigrationError, match="not an ordered migration"):
        discover_migrations(baseline, migrations)


def test_duplicate_migration_version_fails(tmp_path: Path) -> None:
    baseline = _write_migration(tmp_path, "baseline.sql", BASELINE_SQL)
    migrations = tmp_path / "migrations"
    _write_migration(migrations, "0002_a.sql", STEP_SQL)
    _write_migration(migrations, "0002_b.sql", STEP_SQL)
    with pytest.raises(MigrationError, match="duplicate"):
        discover_migrations(baseline, migrations)


def test_empty_migration_file_fails(empty_database_url: str, tmp_path: Path) -> None:
    baseline = _write_migration(tmp_path, "baseline.sql", BASELINE_SQL)
    migrations = tmp_path / "migrations"
    _write_migration(migrations, "0002_empty.sql", "   \n")
    with (
        psycopg.connect(empty_database_url) as conn,
        pytest.raises(MigrationError, match="empty"),
    ):
        apply_migrations(conn, baseline_path=baseline, migrations_dir=migrations)


def test_absent_migrations_directory_uses_baseline_only(tmp_path: Path) -> None:
    baseline = _write_migration(tmp_path, "baseline.sql", BASELINE_SQL)
    discovered = discover_migrations(baseline, tmp_path / "missing-dir")
    assert [item.version for item in discovered] == ["0001"]


def test_migration_directory_entry_must_be_a_file(tmp_path: Path) -> None:
    baseline = _write_migration(tmp_path, "baseline.sql", BASELINE_SQL)
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0002_not_a_file.sql").mkdir()
    with pytest.raises(MigrationError, match="not a file"):
        discover_migrations(baseline, migrations)


def test_missing_baseline_file_fails(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sql"
    with pytest.raises(FileNotFoundError, match="Schema file not found"):
        discover_migrations(missing, tmp_path / "migrations")


def test_migration_logs_omit_financial_amounts(
    empty_database_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    with psycopg.connect(empty_database_url) as conn:
        apply_migrations(conn)
        conn.commit()
        apply_migrations(conn)
    messages = " ".join(record.getMessage() for record in caplog.records)
    extras = " ".join(str(record.__dict__) for record in caplog.records)
    assert "0001" in extras
    assert "amount_cents" not in messages
    assert "amount_cents" not in extras
