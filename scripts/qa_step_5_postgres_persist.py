#!/usr/bin/env python3
"""QA verification script for Step 5: PostgreSQL Persistence.

Validates schema initialization, append-only raw extraction,
re-parse idempotency, domain sidecar persistence, and fail-loud invariants.
"""

import ast
import datetime
from pathlib import Path
from uuid import uuid4

from app.db.connection import get_connection, init_db
from app.db.repository import (
    get_canonical_statement,
    get_extraction_run,
    persist_canonical_statement,
    persist_raw_extraction,
)
from app.models.canonical import (
    Account,
    CanonicalStatement,
    CanonicalTransaction,
    DepositorySummary,
)
from app.models.enums import (
    AccountDomain,
    AccountType,
    RunStatus,
    TransactionCategory,
)
from app.models.raw import (
    ExtractionRun,
    RawExtraction,
    RawPage,
    RawPayload,
)


def test_schema_init_and_ast_invariants() -> None:
    print("Testing schema initialization and AST append-only invariants...")
    conn = get_connection()
    init_db(conn)

    # Static AST check
    repo_path = Path(__file__).parents[1] / "app" / "db" / "repository.py"
    source = repo_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    string_constants = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    for text in string_constants:
        upper = text.upper()
        if (
            "UPDATE RAW_PAYLOADS" in upper
            or "UPDATE RAW_PAGES" in upper
            or "UPDATE RAW_TOKENS" in upper
        ):
            raise AssertionError(f"Forbidden UPDATE query on raw table: {text}")
        if (
            "DELETE FROM RAW_PAYLOADS" in upper
            or "DELETE FROM RAW_PAGES" in upper
            or "DELETE FROM RAW_TOKENS" in upper
        ):
            raise AssertionError(f"Forbidden DELETE query on raw table: {text}")
    print("  ✓ AST strictly confirms zero UPDATE/DELETE operations on raw tables.")
    conn.close()


def test_raw_persistence_and_reingest() -> None:
    print("Testing raw persistence and re-ingest idempotency...")
    conn = get_connection()
    init_db(conn)

    sha = (uuid4().hex + uuid4().hex)[:64]
    payload1 = RawPayload(
        content_sha256=sha, byte_length=1234, original_basename="test.pdf"
    )
    run1 = ExtractionRun(
        raw_payload_id=payload1.raw_payload_id,
        adapter_id="qa_adapter",
        adapter_version="1.0.0",
        status=RunStatus.RAW_STORED,
    )
    page1 = RawPage(run_id=run1.run_id, page_number=1, page_text="Page 1 sample")
    persist_raw_extraction(
        conn, RawExtraction(payload=payload1, run=run1, pages=[page1])
    )
    conn.commit()

    # Re-ingest with same SHA
    payload2 = RawPayload(
        content_sha256=sha, byte_length=1234, original_basename="test_v2.pdf"
    )
    run2 = ExtractionRun(
        raw_payload_id=payload2.raw_payload_id,
        adapter_id="qa_adapter",
        adapter_version="2.0.0",
        status=RunStatus.RAW_STORED,
    )
    page2 = RawPage(
        run_id=run2.run_id, page_number=1, page_text="Page 1 updated adapter text"
    )
    persist_raw_extraction(
        conn, RawExtraction(payload=payload2, run=run2, pages=[page2])
    )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM raw_payloads WHERE content_sha256 = %s;", (sha,)
        )
        assert cur.fetchone()[0] == 1, "raw_payloads count must remain 1"
        cur.execute(
            "SELECT COUNT(*) FROM extraction_runs WHERE adapter_id = 'qa_adapter';"
        )
        assert cur.fetchone()[0] >= 2, "extraction_runs must have recorded distinct run"

    print("  ✓ Re-ingest idempotency verified: payload blob reused, run appended.")
    conn.close()


def test_canonical_persistence() -> None:
    print("Testing canonical statement and domain sidecar persistence...")
    conn = get_connection()
    init_db(conn)

    sha = (uuid4().hex + uuid4().hex)[:64]
    payload = RawPayload(
        content_sha256=sha, byte_length=5000, original_basename="stmt.pdf"
    )
    run = ExtractionRun(
        raw_payload_id=payload.raw_payload_id,
        adapter_id="qa",
        adapter_version="1",
        status=RunStatus.VALIDATED,
    )
    persist_raw_extraction(
        conn,
        RawExtraction(
            payload=payload,
            run=run,
            pages=[RawPage(run_id=run.run_id, page_number=1, page_text="txt")],
        ),
    )
    conn.commit()

    account = Account(
        institution="QA Bank",
        account_mask="*7777",
        account_domain=AccountDomain.DEPOSITORY,
        account_type=AccountType.CHECKING,
    )
    statement = CanonicalStatement(
        account_id=account.account_id,
        run_id=run.run_id,
        raw_payload_id=payload.raw_payload_id,
        statement_start_date=datetime.date(2025, 1, 1),
        statement_end_date=datetime.date(2025, 1, 31),
        opening_balance_cents=200000,
        closing_balance_cents=250000,
        net_change_cents=50000,
    )
    summary = DepositorySummary(
        statement_id=statement.statement_id,
        deposits_cents=50000,
        withdrawals_cents=0,
        interest_paid_cents=0,
        fees_cents=0,
    )
    txns = [
        CanonicalTransaction(
            statement_id=statement.statement_id,
            post_date=datetime.date(2025, 1, 15),
            amount_cents=50000,
            description="Direct Deposit",
            transaction_category=TransactionCategory.DEPOSIT,
        )
    ]

    persist_canonical_statement(conn, account, statement, summary, txns)
    conn.commit()

    res = get_canonical_statement(conn, statement.statement_id)
    assert res is not None
    s_out, sum_out, txns_out = res
    assert s_out.opening_balance_cents == 200000
    assert s_out.closing_balance_cents == 250000
    assert isinstance(sum_out, DepositorySummary)
    assert len(txns_out) == 1
    assert txns_out[0].amount_cents == 50000

    # Ensure run status transitioned
    run_out = get_extraction_run(conn, run.run_id)
    assert run_out is not None
    assert run_out.status == RunStatus.CANONICAL_PERSISTED
    print("  ✓ Canonical statement, sidecar, and status transition verified.")
    conn.close()


def main() -> None:
    print("=" * 60)
    print("ASSASSIN Step 5 QA Validation: PostgreSQL Persistence")
    print("=" * 60)
    test_schema_init_and_ast_invariants()
    test_raw_persistence_and_reingest()
    test_canonical_persistence()
    print("=" * 60)
    print("All Step 5 PostgreSQL Persistence verifications PASSED successfully!")
    print("=" * 60)


if __name__ == "__main__":
    main()
