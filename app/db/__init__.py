"""PostgreSQL persistence layer for append-only raw tables and canonical financial models."""

from app.db.connection import (
    get_connection,
    get_database_url,
    get_db_connection,
    init_db,
)
from app.db.repository import (
    get_canonical_statement,
    get_extraction_run,
    get_or_create_account,
    get_raw_pages,
    get_raw_payload_by_sha256,
    persist_canonical_statement,
    persist_extraction_run,
    persist_raw_extraction,
    persist_raw_pages,
    persist_raw_payload,
    update_run_status,
)

__all__ = [
    "get_canonical_statement",
    "get_connection",
    "get_database_url",
    "get_db_connection",
    "get_extraction_run",
    "get_or_create_account",
    "get_raw_pages",
    "get_raw_payload_by_sha256",
    "init_db",
    "persist_canonical_statement",
    "persist_extraction_run",
    "persist_raw_extraction",
    "persist_raw_pages",
    "persist_raw_payload",
    "update_run_status",
]
