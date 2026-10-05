"""Pytest fixtures shared across the ASSASSIN test suite."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from app.db.connection import init_db
from app.models.canonical import RawStatement

logger = logging.getLogger(__name__)
FIXTURES_DIR: Path = Path(__file__).parent / "fixtures"


@pytest.fixture()
def schwab_sample_path() -> Path:
    """Return the absolute path to the Schwab sample JSON fixture."""
    path: Path = FIXTURES_DIR / "schwab_sample.json"
    assert path.is_file(), f"Fixture missing: {path}"
    return path


@pytest.fixture()
def schwab_raw_statement(schwab_sample_path: Path) -> RawStatement:
    """Load and parse the Schwab fixture into a ``RawStatement``.

    Uses ``model_validate_json`` so that Pydantic's native JSON parser
    handles ``str → date`` and ``str → enum`` coercion correctly under
    strict mode.
    """
    raw_json: str = schwab_sample_path.read_text(encoding="utf-8")
    return RawStatement.model_validate_json(raw_json)


@pytest.fixture(scope="session")
def postgres_url() -> str:
    """Resolve and guarantee an active PostgreSQL database URL for test suite."""
    candidate_urls = [
        os.environ.get("TEST_DATABASE_URL"),
        os.environ.get("DATABASE_URL"),
        "postgresql://postgres:assassin@localhost:54329/assassin_test",
        "postgresql://postgres:postgres@localhost:5432/assassin_test",
    ]
    for url in candidate_urls:
        if not url:
            continue
        try:
            with psycopg.connect(url, connect_timeout=1) as conn:
                conn.execute("SELECT 1;")
                return url
        except psycopg.Error as exc:
            logger.debug("Failed connecting to candidate %s: %s", url, exc)

    # Try starting docker container if docker is available
    if shutil.which("docker"):
        try:
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--name",
                    "assassin-test-db",
                    "-e",
                    "POSTGRES_PASSWORD=assassin",
                    "-e",
                    "POSTGRES_DB=assassin_test",
                    "-p",
                    "54329:5432",
                    "-d",
                    "postgres:16",
                ],
                capture_output=True,
                check=False,
            )
            # wait up to 10 seconds for container readiness
            container_url = (
                "postgresql://postgres:assassin@localhost:54329/assassin_test"
            )
            for _ in range(20):
                time.sleep(0.5)
                try:
                    with psycopg.connect(container_url, connect_timeout=1) as conn:
                        conn.execute("SELECT 1;")
                        return container_url
                except psycopg.Error as exc:
                    logger.debug("Waiting for postgres container: %s", exc)
        except OSError as exc:
            logger.debug("Docker execution failed: %s", exc)

    pytest.skip("PostgreSQL test database is not accessible")
    return ""


@pytest.fixture()
def db_conn(postgres_url: str) -> Iterator[psycopg.Connection]:
    """Yield a connection to the initialized database, truncating tables after each test."""
    conn = psycopg.connect(postgres_url)
    init_db(conn)
    try:
        yield conn
    finally:
        try:
            conn.execute(
                """
                TRUNCATE TABLE raw_tokens, raw_pages, transactions,
                               statement_depository_summaries, statement_credit_summaries,
                               statement_brokerage_summaries, statements, accounts,
                               extraction_runs, raw_payloads CASCADE;
                """
            )
            conn.commit()
        except psycopg.Error as exc:
            logger.debug("Cleanup TRUNCATE failed: %s", exc)
        conn.close()
