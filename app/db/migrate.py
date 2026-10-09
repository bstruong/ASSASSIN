"""Ordered SQL migrations.

Version ``0001`` is the baseline snapshot ``schema.sql``. Later versions are
``app/db/migrations/NNNN_name.sql`` in contiguous order. Already applied
versions are skipped. A missing file, a gap, or a checksum mismatch fails
loudly and is never rewritten in place.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg

from app.models.exceptions import MigrationError

logger = logging.getLogger(__name__)

DEFAULT_BASELINE_PATH = Path(__file__).parent / "schema.sql"
DEFAULT_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_VERSION_FILENAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY CHECK (version ~ '^[0-9]{4}$'),
    checksum TEXT NOT NULL CHECK (checksum ~ '^[0-9a-f]{64}$'),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


@dataclass(frozen=True)
class Migration:
    """One ordered migration file."""

    version: str
    path: Path


def migration_checksum(path: Path) -> str:
    """Return the SHA-256 hex digest of a migration file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def discover_migrations(baseline_path: Path, migrations_dir: Path) -> list[Migration]:
    """Return contiguous migrations starting with baseline version 0001."""
    if not baseline_path.is_file():
        raise FileNotFoundError(f"Schema file not found at {baseline_path}")

    found: list[Migration] = [Migration(version="0001", path=baseline_path)]
    seen = {"0001"}
    if migrations_dir.is_dir():
        for path in sorted(migrations_dir.iterdir(), key=lambda item: item.name):
            if path.name.startswith("."):
                continue
            if not path.is_file():
                raise MigrationError(f"Migration path {path.name} is not a file")
            match = _VERSION_FILENAME.fullmatch(path.name)
            if match is None:
                raise MigrationError(
                    f"Migration filename {path.name} is not an ordered migration"
                )
            version = match.group(1)
            if version in seen:
                raise MigrationError(f"duplicate migration version {version}")
            seen.add(version)
            found.append(Migration(version=version, path=path))

    found.sort(key=lambda item: int(item.version))
    numbers = [int(item.version) for item in found]
    expected = list(range(1, numbers[-1] + 1))
    if numbers != expected:
        missing = [f"{number:04d}" for number in expected if number not in numbers]
        raise MigrationError(
            "Migration versions are out of order or missing: "
            f"missing {', '.join(missing)}"
        )
    return found


def apply_migrations(
    conn: psycopg.Connection,
    *,
    baseline_path: Path | None = None,
    migrations_dir: Path | None = None,
) -> list[str]:
    """Apply pending migrations. Return the version ids applied in this call."""
    baseline = DEFAULT_BASELINE_PATH if baseline_path is None else baseline_path
    directory = DEFAULT_MIGRATIONS_DIR if migrations_dir is None else migrations_dir
    migrations = discover_migrations(baseline, directory)
    conn.execute(_LEDGER_DDL)
    conn.execute("LOCK TABLE schema_migrations IN EXCLUSIVE MODE")
    recorded = _recorded_checksums(conn)
    known = {item.version: item for item in migrations}

    for version, checksum in recorded.items():
        migration = known.get(version)
        if migration is None:
            logger.error(
                "Recorded schema migration is missing",
                extra={"migration_version": version},
            )
            raise MigrationError(
                f"Recorded migration version {version} is missing from disk"
            )
        actual = migration_checksum(migration.path)
        if checksum != actual:
            logger.error(
                "Schema migration checksum mismatch",
                extra={"migration_version": version},
            )
            raise MigrationError(
                f"Migration {version} checksum mismatch: "
                f"recorded {checksum!r} does not match file {actual!r}"
            )

    planned = [item.version for item in migrations]
    applied_versions = sorted(recorded, key=int)
    if applied_versions != planned[: len(applied_versions)]:
        logger.error(
            "Schema migrations are out of order",
            extra={"migration_version": ",".join(applied_versions)},
        )
        raise MigrationError(
            "Applied migration versions are out of order: "
            f"recorded {applied_versions}, expected prefix of {planned}"
        )

    newly_applied: list[str] = []
    for migration in migrations:
        if migration.version in recorded:
            logger.info(
                "Schema migration already applied",
                extra={"migration_version": migration.version},
            )
            continue
        _apply_one(conn, migration)
        newly_applied.append(migration.version)
    return newly_applied


def _recorded_checksums(conn: psycopg.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    return {str(version): str(checksum) for version, checksum in rows}


def _apply_one(conn: psycopg.Connection, migration: Migration) -> None:
    raw = migration.path.read_bytes()
    script = raw.decode("utf-8")
    if not script.strip():
        raise MigrationError(f"Migration {migration.version} is empty")
    logger.info(
        "Applying schema migration",
        extra={"migration_version": migration.version},
    )
    conn.execute(script)
    conn.execute(
        """
        INSERT INTO schema_migrations (version, checksum)
        VALUES (%s, %s)
        """,
        (migration.version, hashlib.sha256(raw).hexdigest()),
    )
