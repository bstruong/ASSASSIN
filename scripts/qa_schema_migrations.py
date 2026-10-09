#!/usr/bin/env python3
"""QA: ordered schema migrations.

Run with: uv run python scripts/qa_schema_migrations.py

Verifies a fresh database reaches the current schema (including holdings,
integer cents) and that a tampered migration checksum fails loudly.

Manual UI QA: not applicable (no user-visible UI).
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import psycopg
from psycopg import sql

from app.db.connection import DEFAULT_DATABASE_URL, init_db
from app.db.migrate import apply_migrations
from app.models.exceptions import MigrationError


class QAReport:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[str] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        bucket = self.passed if ok else self.failed
        bucket.append(name)
        label = "PASS" if ok else "FAIL"
        suffix = f": {detail}" if detail else ""
        print(f"  [{label}] {name}{suffix}")

    def summary(self) -> bool:
        total = len(self.passed) + len(self.failed)
        print(f"\n{'=' * 60}")
        print(f"QA Summary: {len(self.passed)}/{total} passed")
        if self.failed:
            for name in self.failed:
                print(f"  - {name}")
            return False
        print("All tests passed!")
        return True


def _swap_database(database_url: str, database: str) -> str:
    parts = urlsplit(database_url)
    return urlunsplit(parts._replace(path=f"/{database}"))


def _admin_url(database_url: str) -> str:
    return _swap_database(database_url, "postgres")


def _create_database(admin_url: str, name: str) -> None:
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def _drop_database(admin_url: str, name: str) -> None:
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


def _holdings_type(conn: psycopg.Connection) -> str | None:
    row = conn.execute(
        """
        SELECT data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'holdings'
          AND column_name = 'market_value_cents'
        """
    ).fetchone()
    if row is None:
        return None
    return str(row[0])


def _verify(database_url: str, report: QAReport) -> None:
    print("Happy path: fresh apply reaches holdings stored as integer cents")
    init_db(url=database_url)
    with psycopg.connect(database_url) as conn:
        holdings_type = _holdings_type(conn)
        versions = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        report.record(
            "fresh apply creates holdings.market_value_cents as bigint",
            holdings_type == "bigint" and versions == [("0001",)],
            f"type={holdings_type} versions={versions}",
        )
        before = conn.execute(
            "SELECT version, checksum, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        conn.execute("DROP TABLE holdings")
        conn.commit()

    print("Happy path: applying again does not re-run version 0001")
    applied_again = None
    with psycopg.connect(database_url) as conn:
        applied_again = apply_migrations(conn)
        conn.commit()
        after = conn.execute(
            "SELECT version, checksum, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        holdings_after = _holdings_type(conn)
    report.record(
        "second apply is a no-op",
        applied_again == [] and after == before and holdings_after is None,
        f"applied={applied_again}",
    )

    print("Negative path: tampered checksum must fail loudly")
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "UPDATE schema_migrations SET checksum = %s WHERE version = %s",
            ("0" * 64, "0001"),
        )
        conn.commit()
        try:
            apply_migrations(conn)
        except MigrationError as exc:
            report.record(
                "tampered checksum fails loudly",
                "checksum mismatch" in str(exc),
                str(exc),
            )
        else:
            report.record(
                "tampered checksum fails loudly",
                False,
                "apply_migrations returned without raising",
            )


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    print("Feature: ordered schema migrations")
    print("Manual UI QA: not applicable (no user-visible UI).")
    database_url = DEFAULT_DATABASE_URL
    admin_url = _admin_url(database_url)
    name = f"assassin_qa_mig_{uuid4().hex[:12]}"
    report = QAReport()
    try:
        _create_database(admin_url, name)
    except psycopg.Error as exc:
        report.record("connect and create a private database", False, str(exc))
        ok = report.summary()
        return 0 if ok else 1
    url = _swap_database(database_url, name)
    try:
        _verify(url, report)
    except Exception as exc:  # noqa: BLE001 — QA must show the unexpected type
        report.record("schema migration QA", False, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            _drop_database(admin_url, name)
        except psycopg.Error as exc:
            report.record("drop private database", False, str(exc))
    ok = report.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
