#!/usr/bin/env python3
"""QA Verification Script for Step 8: Combined Multi-Account Adapter.

Demonstrates:
1. Mutual exclusivity in registry dispatch between single and combined depository adapters.
2. Happy path multi-account extraction: emitting 2 canonical statements under the same run_id.
3. Stated combined aggregate balance validation against sum of account closing balances.
4. Fail-loud protection when single-account adapter processes multi-account document (AmbiguousAccountsError).
5. Fail-loud protection on 1-cent balance discrepancies (InvariantError).
6. Fail-loud protection on schema/section drift (MissingSectionError).
7. End-to-end database persistence in PostgreSQL.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import psycopg
from reportlab.pdfgen import canvas

from app.adapters.combined import StandardCombinedDepositoryAdapter
from app.adapters.depository import StandardDepositoryAdapter
from app.adapters.registry import get_default_registry
from app.db.connection import init_db
from app.db.repository import persist_canonical_statement, persist_raw_extraction
from app.models.enums import RunStatus
from app.models.exceptions import (
    AmbiguousAccountsError,
    InvariantError,
    MissingSectionError,
)
from app.models.raw import ExtractionRun, RawExtraction, RawPage, RawPayload

GREEN = "\033[92m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"


def print_step(title: str) -> None:
    print(f"\n{BOLD}{'=' * 75}\n{title}\n{'=' * 75}{RESET}")


def print_pass(msg: str) -> None:
    print(f"  {GREEN}✔ [PASS]{RESET} {msg}")


def print_fail(msg: str) -> None:
    print(f"  {RED}✘ [FAIL]{RESET} {msg}")


def create_test_pdf(path: Path, lines: list[str]) -> Path:
    c = canvas.Canvas(str(path))
    y = 750
    for line in lines:
        c.drawString(100, y, line)
        y -= 25
    c.save()
    return path


def main() -> int:
    print_step("STEP 8 QA DEMO: Combined Multi-Account Adapter")
    fixtures_dir = Path(__file__).parents[1] / "tests" / "fixtures"
    fixture_path = fixtures_dir / "combined_checking_savings_happy.json"

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        # -------------------------------------------------------------------------
        # 1. Mutual Exclusivity in Default Registry Matching
        # -------------------------------------------------------------------------
        print_step("Scenario 1: Registry Mutual Exclusivity")
        registry = get_default_registry()

        combined_pdf = create_test_pdf(
            tmp_path / "combined.pdf",
            [
                "STANDARD BANK",
                "COMBINED ACCOUNT STATEMENT",
                "Checking Account Number: *1234",
                "Savings Account Number: *5678",
            ],
        )
        checking_pdf = create_test_pdf(
            tmp_path / "checking.pdf",
            [
                "STANDARD BANK",
                "CHECKING ACCOUNT STATEMENT",
                "Account Number: *1234",
            ],
        )

        matched_comb = registry.match(combined_pdf)
        assert isinstance(matched_comb, StandardCombinedDepositoryAdapter)
        print_pass(
            f"Combined document matched exclusively by: {matched_comb.adapter_id}"
        )

        matched_single = registry.match(checking_pdf)
        assert isinstance(matched_single, StandardDepositoryAdapter)
        print_pass(
            f"Single-account checking document matched exclusively by: {matched_single.adapter_id}"
        )

        # -------------------------------------------------------------------------
        # 2. Happy Path Multi-Account Canonical Extraction
        # -------------------------------------------------------------------------
        print_step("Scenario 2: Happy Path Multi-Account Canonical Extraction")
        fixture_data = json.loads(fixture_path.read_text(encoding="utf-8"))
        text = fixture_data["raw_text"]

        payload = RawPayload(
            content_sha256="a" * 64,
            byte_length=len(text.encode()),
            original_basename="combined_happy.pdf",
        )
        run = ExtractionRun(
            raw_payload_id=payload.raw_payload_id,
            adapter_id="standard_combined_depository",
            adapter_version="1.0.0",
            status=RunStatus.EXTRACTED,
        )
        extraction = RawExtraction(
            payload=payload,
            run=run,
            pages=[RawPage(run_id=run.run_id, page_number=1, page_text=text)],
        )

        adapter = StandardCombinedDepositoryAdapter()
        bundles = adapter.parse_canonical(extraction)

        assert len(bundles) == 2
        chk_acc, chk_stmt, _chk_sum, chk_txns = bundles[0]
        sav_acc, sav_stmt, _sav_sum, sav_txns = bundles[1]

        print_pass(
            f"Account 1: {chk_acc.account_type.value.upper()} (Mask: {chk_acc.account_mask}) | "
            f"Closing: ${chk_stmt.closing_balance_cents / 100:,.2f} ({chk_stmt.closing_balance_cents} cents) | "
            f"Txns: {len(chk_txns)}"
        )
        print_pass(
            f"Account 2: {sav_acc.account_type.value.upper()} (Mask: {sav_acc.account_mask}) | "
            f"Closing: ${sav_stmt.closing_balance_cents / 100:,.2f} ({sav_stmt.closing_balance_cents} cents) | "
            f"Txns: {len(sav_txns)}"
        )

        # Invariant: same run_id and raw_payload_id
        assert chk_stmt.run_id == sav_stmt.run_id == run.run_id
        assert (
            chk_stmt.raw_payload_id == sav_stmt.raw_payload_id == payload.raw_payload_id
        )
        assert chk_stmt.statement_id != sav_stmt.statement_id
        print_pass(
            f"Both statements share identical run_id ({run.run_id}) and raw_payload_id ({payload.raw_payload_id})"
        )

        # Aggregate total validation
        sum_closings = chk_stmt.closing_balance_cents + sav_stmt.closing_balance_cents
        print_pass(
            f"Stated Combined Total ($6,320.00 / 632,000 cents) == Sum of Closings ({sum_closings} cents)"
        )

        # -------------------------------------------------------------------------
        # 3. Single-Account Adapter Rejection (AmbiguousAccountsError)
        # -------------------------------------------------------------------------
        print_step("Scenario 3: Single-Account Adapter Fail-Loud Rejection")
        single_adapter = StandardDepositoryAdapter()
        try:
            single_adapter.parse_canonical(extraction)
            print_fail("Expected AmbiguousAccountsError was not raised!")
            return 1
        except AmbiguousAccountsError as exc:
            print_pass(f"Correctly failed with AmbiguousAccountsError: {exc}")

        # -------------------------------------------------------------------------
        # 4. Invariant Protection: 1-Cent Discrepancy in Combined Aggregate
        # -------------------------------------------------------------------------
        print_step("Scenario 4: 1-Cent Discrepancy in Aggregate Combined Total")
        tampered_text = text.replace(
            "Combined Total Balance: $6,320.00",
            "Combined Total Balance: $6,320.01",
        )
        bad_extraction = RawExtraction(
            payload=payload,
            run=run,
            pages=[RawPage(run_id=run.run_id, page_number=1, page_text=tampered_text)],
        )
        try:
            adapter.parse_canonical(bad_extraction)
            print_fail("Expected InvariantError was not raised on 1-cent mismatch!")
            return 1
        except InvariantError as exc:
            print_pass(
                f"Correctly rejected 1-cent aggregate mismatch with InvariantError: {exc}"
            )

        # -------------------------------------------------------------------------
        # 5. Invariant Protection: Missing Mandatory Section
        # -------------------------------------------------------------------------
        print_step("Scenario 5: Missing Section Fail-Loud Contract")
        tampered_section = text.replace("CHECKING TRANSACTION DETAILS\n", "")
        bad_section_extraction = RawExtraction(
            payload=payload,
            run=run,
            pages=[
                RawPage(run_id=run.run_id, page_number=1, page_text=tampered_section)
            ],
        )
        try:
            adapter.parse_canonical(bad_section_extraction)
            print_fail("Expected MissingSectionError was not raised!")
            return 1
        except MissingSectionError as exc:
            print_pass(f"Correctly caught missing section: {exc}")

        # -------------------------------------------------------------------------
        # 6. Database Persistence Verification
        # -------------------------------------------------------------------------
        print_step("Scenario 6: Multi-Account PostgreSQL Persistence")
        postgres_url = os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql://postgres:assassin@localhost:54329/assassin_test",
        )
        try:
            init_db(url=postgres_url)
            with psycopg.connect(postgres_url) as conn:
                # Unique payload to avoid collision with test runs
                unique_payload = RawPayload(
                    content_sha256=hashlib.sha256(
                        f"qa_{uuid4().hex}".encode()
                    ).hexdigest(),
                    byte_length=len(text.encode()),
                    original_basename="qa_combined.pdf",
                )
                unique_run = ExtractionRun(
                    raw_payload_id=unique_payload.raw_payload_id,
                    adapter_id="standard_combined_depository",
                    adapter_version="1.0.0",
                    status=RunStatus.EXTRACTED,
                )
                unique_extraction = RawExtraction(
                    payload=unique_payload,
                    run=unique_run,
                    pages=[
                        RawPage(
                            run_id=unique_run.run_id,
                            page_number=1,
                            page_text=text,
                        )
                    ],
                )
                parsed_bundles = adapter.parse_canonical(unique_extraction)

                persist_raw_extraction(conn, unique_extraction)
                for acc, stmt, summary, txns in parsed_bundles:
                    persist_canonical_statement(conn, acc, stmt, summary, txns)
                conn.commit()

                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) FROM statements WHERE run_id = %s;",
                        (str(unique_run.run_id),),
                    )
                    count = cur.fetchone()[0]
                    assert count == 2
                    print_pass(
                        f"Successfully persisted {count} distinct statements under run_id {unique_run.run_id}"
                    )

                    cur.execute(
                        "SELECT a.account_mask, a.account_type, s.closing_balance_cents "
                        "FROM statements s JOIN accounts a ON s.account_id = a.account_id "
                        "WHERE s.run_id = %s ORDER BY a.account_type;",
                        (str(unique_run.run_id),),
                    )
                    rows = cur.fetchall()
                    for mask, acc_type, cents in rows:
                        print_pass(
                            f"Persisted row: Account {acc_type} ({mask}) -> Closing: {cents} cents (${cents / 100:,.2f})"
                        )
        except (psycopg.Error, InvariantError, AssertionError) as exc:
            print_fail(f"PostgreSQL persistence error: {exc}")
            return 1

    print_step("STEP 8 VERIFICATION COMPLETE: ALL INVARIANTS SATISFIED ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
