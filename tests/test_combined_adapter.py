"""Tests for Step 8: Combined Multi-Account Adapter."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from reportlab.pdfgen import canvas

from app.adapters.combined import StandardCombinedDepositoryAdapter
from app.adapters.depository import StandardDepositoryAdapter
from app.adapters.registry import get_default_registry
from app.db.connection import init_db
from app.db.repository import (
    persist_canonical_statement,
    persist_raw_extraction,
)
from app.models.enums import AccountDomain, AccountType, RunStatus, TransactionCategory
from app.models.exceptions import (
    AmbiguousAccountsError,
    InvariantError,
    MissingSectionError,
)
from app.models.raw import ExtractionRun, RawExtraction, RawPage, RawPayload

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> RawExtraction:
    path = FIXTURES_DIR / f"{name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    text = data["raw_text"]
    content_sha256 = hashlib.sha256(f"{name}_{uuid4().hex}".encode()).hexdigest()
    payload = RawPayload(
        content_sha256=content_sha256,
        byte_length=len(text.encode()),
        original_basename=f"{name}.pdf",
    )
    run = ExtractionRun(
        raw_payload_id=payload.raw_payload_id,
        adapter_id="standard_combined_depository",
        adapter_version="1.0.0",
        status=RunStatus.EXTRACTED,
    )
    return RawExtraction(
        payload=payload,
        run=run,
        pages=[RawPage(run_id=run.run_id, page_number=1, page_text=text)],
    )


def create_sample_pdf(path: Path, header_lines: list[str]) -> Path:
    c = canvas.Canvas(str(path))
    y = 750
    for line in header_lines:
        c.drawString(100, y, line)
        y -= 25
    c.save()
    return path


class TestCombinedAdapterWiring:
    """Validate metadata and contract wiring."""

    def test_adapter_attributes(self) -> None:
        adapter = StandardCombinedDepositoryAdapter()
        assert adapter.adapter_id == "standard_combined_depository"
        assert adapter.institution_id == "standard_bank"
        assert adapter.adapter_version == "1.0.0"
        assert adapter.account_domain == AccountDomain.DEPOSITORY
        assert adapter.account_types == frozenset(
            {AccountType.CHECKING, AccountType.SAVINGS}
        )


class TestCombinedAdapterMatchesExclusivity:
    """Validate mutual exclusivity between single and combined depository adapters."""

    def test_combined_matches_on_combined_pdf(self, tmp_path: Path) -> None:
        combined_pdf = create_sample_pdf(
            tmp_path / "combined.pdf",
            [
                "STANDARD BANK",
                "COMBINED ACCOUNT STATEMENT",
                "Checking Account Number: *1234",
                "Savings Account Number: *5678",
            ],
        )
        combined_adapter = StandardCombinedDepositoryAdapter()
        single_adapter = StandardDepositoryAdapter()

        assert combined_adapter.matches(combined_pdf) is True
        assert single_adapter.matches(combined_pdf) is False

    def test_single_matches_on_checking_only_pdf(self, tmp_path: Path) -> None:
        checking_pdf = create_sample_pdf(
            tmp_path / "checking.pdf",
            [
                "STANDARD BANK",
                "CHECKING ACCOUNT STATEMENT",
                "Checking Account Number: *1234",
            ],
        )
        combined_adapter = StandardCombinedDepositoryAdapter()
        single_adapter = StandardDepositoryAdapter()

        assert combined_adapter.matches(checking_pdf) is False
        assert single_adapter.matches(checking_pdf) is True

    def test_single_matches_on_savings_only_pdf(self, tmp_path: Path) -> None:
        savings_pdf = create_sample_pdf(
            tmp_path / "savings.pdf",
            [
                "STANDARD BANK",
                "SAVINGS ACCOUNT STATEMENT",
                "Savings Account Number: *5678",
            ],
        )
        combined_adapter = StandardCombinedDepositoryAdapter()
        single_adapter = StandardDepositoryAdapter()

        assert combined_adapter.matches(savings_pdf) is False
        assert single_adapter.matches(savings_pdf) is True

    def test_combined_rejects_missing_file(self, tmp_path: Path) -> None:
        combined_adapter = StandardCombinedDepositoryAdapter()
        with pytest.raises(FileNotFoundError):
            combined_adapter.matches(tmp_path / "nonexistent.pdf")

    def test_registry_exclusive_dispatch(self, tmp_path: Path) -> None:
        combined_pdf = create_sample_pdf(
            tmp_path / "combined.pdf",
            [
                "STANDARD BANK",
                "COMBINED ACCOUNT STATEMENT",
                "Checking Account Number: *1234",
                "Savings Account Number: *5678",
            ],
        )
        checking_pdf = create_sample_pdf(
            tmp_path / "checking.pdf",
            [
                "STANDARD BANK",
                "CHECKING ACCOUNT STATEMENT",
                "Account Number: *1234",
            ],
        )

        registry = get_default_registry()
        # Verify both adapters can be in the same registry without collision
        matched_combined = registry.match(combined_pdf)
        assert isinstance(matched_combined, StandardCombinedDepositoryAdapter)

        matched_single = registry.match(checking_pdf)
        assert isinstance(matched_single, StandardDepositoryAdapter)


class TestCombinedAdapterParsing:
    """Validate parsing of multi-account aggregate statements."""

    def test_parse_happy_combined_fixture(self) -> None:
        adapter = StandardCombinedDepositoryAdapter()
        extraction = load_fixture("combined_checking_savings_happy")

        bundles = adapter.parse_canonical(extraction)
        assert len(bundles) == 2

        # 1. Checking bundle
        chk_acc, chk_stmt, chk_sum, chk_txns = bundles[0]
        assert chk_acc.institution == "standard_bank"
        assert chk_acc.account_mask == "*1234"
        assert chk_acc.account_type == AccountType.CHECKING
        assert chk_stmt.opening_balance_cents == 100000
        assert chk_stmt.closing_balance_cents == 129500
        assert chk_stmt.net_change_cents == 29500
        assert chk_sum.deposits_cents == 30000
        assert chk_sum.fees_cents == 500
        assert len(chk_txns) == 2
        assert chk_txns[0].amount_cents == 30000
        assert chk_txns[0].transaction_category == TransactionCategory.DEPOSIT
        assert chk_txns[1].amount_cents == -500
        assert chk_txns[1].transaction_category == TransactionCategory.FEE

        # 2. Savings bundle
        sav_acc, sav_stmt, sav_sum, sav_txns = bundles[1]
        assert sav_acc.institution == "standard_bank"
        assert sav_acc.account_mask == "*5678"
        assert sav_acc.account_type == AccountType.SAVINGS
        assert sav_stmt.opening_balance_cents == 500000
        assert sav_stmt.closing_balance_cents == 502500
        assert sav_stmt.net_change_cents == 2500
        assert sav_sum.interest_paid_cents == 2500
        assert len(sav_txns) == 1
        assert sav_txns[0].amount_cents == 2500
        assert sav_txns[0].transaction_category == TransactionCategory.INTEREST_PAID

        # Shared metadata
        assert chk_stmt.raw_payload_id == extraction.payload.raw_payload_id
        assert sav_stmt.raw_payload_id == extraction.payload.raw_payload_id
        assert chk_stmt.run_id == extraction.run.run_id
        assert sav_stmt.run_id == extraction.run.run_id
        assert chk_stmt.statement_id != sav_stmt.statement_id

    def test_single_account_adapter_fails_on_combined(self) -> None:
        single_adapter = StandardDepositoryAdapter()
        extraction = load_fixture("combined_checking_savings_happy")
        with pytest.raises(AmbiguousAccountsError):
            single_adapter.parse_canonical(extraction)

    def test_combined_total_mismatch_fails_invariant(self) -> None:
        adapter = StandardCombinedDepositoryAdapter()
        extraction = load_fixture("combined_checking_savings_happy")
        # Tamper combined total by 1 cent
        bad_text = extraction.pages[0].page_text.replace(
            "Combined Total Balance: $6,320.00",
            "Combined Total Balance: $6,320.01",
        )
        bad_extraction = RawExtraction(
            payload=extraction.payload,
            run=extraction.run,
            pages=[
                RawPage(run_id=extraction.run.run_id, page_number=1, page_text=bad_text)
            ],
        )
        with pytest.raises(InvariantError, match="Combined total balance mismatch"):
            adapter.parse_canonical(bad_extraction)

    def test_checking_reconciliation_break_fails_invariant(self) -> None:
        adapter = StandardCombinedDepositoryAdapter()
        extraction = load_fixture("combined_checking_savings_happy")
        # Tamper checking ending balance by 1 cent
        bad_text = extraction.pages[0].page_text.replace(
            "Ending Balance: $1,295.00",
            "Ending Balance: $1,295.01",
            1,  # replace first occurrence (checking)
        )
        bad_extraction = RawExtraction(
            payload=extraction.payload,
            run=extraction.run,
            pages=[
                RawPage(run_id=extraction.run.run_id, page_number=1, page_text=bad_text)
            ],
        )
        with pytest.raises(InvariantError):
            adapter.parse_canonical(bad_extraction)

    def test_missing_section_fails(self) -> None:
        adapter = StandardCombinedDepositoryAdapter()
        extraction = load_fixture("combined_checking_savings_happy")
        bad_text = extraction.pages[0].page_text.replace(
            "CHECKING TRANSACTION DETAILS\n",
            "",
        )
        bad_extraction = RawExtraction(
            payload=extraction.payload,
            run=extraction.run,
            pages=[
                RawPage(run_id=extraction.run.run_id, page_number=1, page_text=bad_text)
            ],
        )
        with pytest.raises(MissingSectionError):
            adapter.parse_canonical(bad_extraction)

    def test_missing_period_dates_fails(self) -> None:
        adapter = StandardCombinedDepositoryAdapter()
        extraction = load_fixture("combined_checking_savings_happy")
        bad_text = extraction.pages[0].page_text.replace(
            "Statement Period: 2025-01-01 to 2025-01-31", ""
        )
        bad_extraction = RawExtraction(
            payload=extraction.payload,
            run=extraction.run,
            pages=[
                RawPage(run_id=extraction.run.run_id, page_number=1, page_text=bad_text)
            ],
        )
        with pytest.raises(MissingSectionError, match="statement period dates"):
            adapter.parse_canonical(bad_extraction)

    def test_missing_checking_account_number_fails(self) -> None:
        adapter = StandardCombinedDepositoryAdapter()
        extraction = load_fixture("combined_checking_savings_happy")
        bad_text = extraction.pages[0].page_text.replace(
            "Checking Account Number: *1234", ""
        )
        bad_extraction = RawExtraction(
            payload=extraction.payload,
            run=extraction.run,
            pages=[
                RawPage(run_id=extraction.run.run_id, page_number=1, page_text=bad_text)
            ],
        )
        with pytest.raises(MissingSectionError, match="checking account number"):
            adapter.parse_canonical(bad_extraction)

    def test_missing_savings_account_number_fails(self) -> None:
        adapter = StandardCombinedDepositoryAdapter()
        extraction = load_fixture("combined_checking_savings_happy")
        bad_text = extraction.pages[0].page_text.replace(
            "Savings Account Number: *5678", ""
        )
        bad_extraction = RawExtraction(
            payload=extraction.payload,
            run=extraction.run,
            pages=[
                RawPage(run_id=extraction.run.run_id, page_number=1, page_text=bad_text)
            ],
        )
        with pytest.raises(MissingSectionError, match="savings account number"):
            adapter.parse_canonical(bad_extraction)

    def test_missing_summary_section_fails(self) -> None:
        adapter = StandardCombinedDepositoryAdapter()
        extraction = load_fixture("combined_checking_savings_happy")
        bad_text = extraction.pages[0].page_text.replace("Checking Summary", "")
        bad_extraction = RawExtraction(
            payload=extraction.payload,
            run=extraction.run,
            pages=[
                RawPage(run_id=extraction.run.run_id, page_number=1, page_text=bad_text)
            ],
        )
        with pytest.raises(MissingSectionError, match="Checking Summary section"):
            adapter.parse_canonical(bad_extraction)

    def test_missing_starting_balance_fails(self) -> None:
        adapter = StandardCombinedDepositoryAdapter()
        extraction = load_fixture("combined_checking_savings_happy")
        bad_text = extraction.pages[0].page_text.replace(
            "Starting Balance: $1,000.00", ""
        )
        bad_extraction = RawExtraction(
            payload=extraction.payload,
            run=extraction.run,
            pages=[
                RawPage(run_id=extraction.run.run_id, page_number=1, page_text=bad_text)
            ],
        )
        with pytest.raises(MissingSectionError, match="Starting Balance"):
            adapter.parse_canonical(bad_extraction)

    def test_missing_ending_balance_fails(self) -> None:
        adapter = StandardCombinedDepositoryAdapter()
        extraction = load_fixture("combined_checking_savings_happy")
        bad_text = extraction.pages[0].page_text.replace(
            "Ending Balance: $1,295.00", ""
        )
        bad_extraction = RawExtraction(
            payload=extraction.payload,
            run=extraction.run,
            pages=[
                RawPage(run_id=extraction.run.run_id, page_number=1, page_text=bad_text)
            ],
        )
        with pytest.raises(MissingSectionError, match="Ending Balance"):
            adapter.parse_canonical(bad_extraction)

    def test_parse_transactions_fee_reversals_and_withdrawals(self) -> None:
        adapter = StandardCombinedDepositoryAdapter()
        raw_text = (
            "STANDARD BANK\n"
            "COMBINED ACCOUNT STATEMENT\n"
            "Statement Period: 2025-01-01 to 2025-01-31\n"
            "Combined Total Balance: $6,000.00\n\n"
            "Checking Account Number: *1234\n"
            "Checking Summary\n"
            "Starting Balance: $1,000.00\n"
            "Deposits: $0.00\n"
            "Withdrawals: $50.00\n"
            "Fees: $0.00\n"
            "Ending Balance: $950.00\n\n"
            "Savings Account Number: *5678\n"
            "Savings Summary\n"
            "Starting Balance: $5,000.00\n"
            "Deposits: $50.00\n"
            "Ending Balance: $5,050.00\n\n"
            "CHECKING ACCOUNT SUMMARY\n"
            "CHECKING TRANSACTION DETAILS\n"
            "Date    Description    Amount    Balance\n"
            "2025-01-05    ATM Withdrawal    -$50.00    $950.00\n"
            "Invalid Row Skipped\n"
            "9999-99-99    Bad Date    -$10.00\n\n"
            "SAVINGS ACCOUNT SUMMARY\n"
            "SAVINGS TRANSACTION DETAILS\n"
            "Date    Description    Amount    Balance\n"
            "2025-01-10    Transfer Deposit    $50.00    $5,050.00\n\n"
            "END OF STATEMENT"
        )
        extraction = load_fixture("combined_checking_savings_happy")
        custom_extraction = RawExtraction(
            payload=extraction.payload,
            run=extraction.run,
            pages=[
                RawPage(run_id=extraction.run.run_id, page_number=1, page_text=raw_text)
            ],
        )
        bundles = adapter.parse_canonical(custom_extraction)
        _chk_acc, chk_stmt, _chk_sum, chk_txns = bundles[0]
        assert chk_stmt.closing_balance_cents == 95000
        assert chk_txns[0].transaction_category == TransactionCategory.WITHDRAWAL

    def test_missing_transaction_start_marker_fails(self) -> None:
        adapter = StandardCombinedDepositoryAdapter()
        with pytest.raises(MissingSectionError, match="Mandatory marker"):
            adapter._parse_account_transactions(
                "TEXT WITHOUT MARKER",
                start_marker="NONEXISTENT MARKER",
                end_marker="END",
                statement_id=uuid4(),
                account_type=AccountType.CHECKING,
            )

    def test_matches_with_corrupt_or_empty_pdf(self, tmp_path: Path) -> None:
        adapter = StandardCombinedDepositoryAdapter()
        # Empty file
        empty_pdf = tmp_path / "empty.pdf"
        empty_pdf.write_bytes(b"")
        assert adapter.matches(empty_pdf) is False

        # Non-pdf binary garbage
        garbage_file = tmp_path / "garbage.pdf"
        garbage_file.write_bytes(b"NOT A REAL PDF FILE")
        assert adapter.matches(garbage_file) is False

    def test_matches_with_other_bank_and_zero_pages(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter = StandardCombinedDepositoryAdapter()
        other_bank = create_sample_pdf(
            tmp_path / "chase.pdf",
            ["CHASE BANK", "COMBINED STATEMENT"],
        )
        assert adapter.matches(other_bank) is False

        # Zero pages PDF
        class MockPdf:
            def __init__(self) -> None:
                self.pages: list = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        import pdfplumber

        monkeypatch.setattr(pdfplumber, "open", lambda _: MockPdf())
        assert adapter.matches(other_bank) is False

    def test_parse_transactions_fee_reversal_category(self) -> None:
        adapter = StandardCombinedDepositoryAdapter()
        raw_text = (
            "STANDARD BANK\n"
            "COMBINED ACCOUNT STATEMENT\n"
            "Statement Period: 2025-01-01 to 2025-01-31\n"
            "Combined Total Balance: $6,000.00\n\n"
            "Checking Account Number: *1234\n"
            "Checking Summary\n"
            "Starting Balance: $1,000.00\n"
            "Deposits: $0.00\n"
            "Fees: $0.00\n"
            "Ending Balance: $1,000.00\n\n"
            "Savings Account Number: *5678\n"
            "Savings Summary\n"
            "Starting Balance: $5,000.00\n"
            "Ending Balance: $5,000.00\n\n"
            "CHECKING ACCOUNT SUMMARY\n"
            "CHECKING TRANSACTION DETAILS\n"
            "Date    Description    Amount    Balance\n"
            "2025-01-05    Monthly Fee    -$15.00    $985.00\n"
            "2025-01-06    Fee Reversal Courtesy    $15.00    $1,000.00\n\n"
            "SAVINGS ACCOUNT SUMMARY\n"
            "SAVINGS TRANSACTION DETAILS\n"
            "Date    Description    Amount    Balance\n\n"
            "END OF STATEMENT"
        )
        extraction = load_fixture("combined_checking_savings_happy")
        custom_extraction = RawExtraction(
            payload=extraction.payload,
            run=extraction.run,
            pages=[
                RawPage(run_id=extraction.run.run_id, page_number=1, page_text=raw_text)
            ],
        )
        bundles = adapter.parse_canonical(custom_extraction)
        _chk_acc, _chk_stmt, _chk_sum, chk_txns = bundles[0]
        assert chk_txns[0].transaction_category == TransactionCategory.FEE
        assert chk_txns[1].transaction_category == TransactionCategory.FEE_REVERSAL


class TestCombinedAdapterPostgresPersistence:
    """Validate persistence of multi-account statements in PostgreSQL."""

    def test_persist_both_statements_same_run(self, postgres_url: str) -> None:
        init_db(url=postgres_url)
        adapter = StandardCombinedDepositoryAdapter()
        extraction = load_fixture("combined_checking_savings_happy")
        bundles = adapter.parse_canonical(extraction)

        with psycopg.connect(postgres_url) as conn:
            persist_raw_extraction(conn, extraction)

            for acc, stmt, summary, txns in bundles:
                persist_canonical_statement(conn, acc, stmt, summary, txns)
            conn.commit()

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM statements WHERE run_id = %s;",
                    (str(extraction.run.run_id),),
                )
                stmt_count = cur.fetchone()[0]
                assert stmt_count == 2

                cur.execute(
                    "SELECT account_type, closing_balance_cents FROM statements s "
                    "JOIN accounts a ON s.account_id = a.account_id "
                    "WHERE s.run_id = %s ORDER BY account_type;",
                    (str(extraction.run.run_id),),
                )
                rows = cur.fetchall()
                assert rows == [("checking", 129500), ("savings", 502500)]


class TestCombinedAdapterCodeInvariants:
    """Validate static invariants: no dynamic metaprogramming."""

    def test_no_dynamic_metaprogramming(self) -> None:
        file_path = Path(__file__).parents[1] / "app" / "adapters" / "combined.py"
        if not file_path.exists():
            pytest.skip("combined.py not yet created")
        tree = ast.parse(file_path.read_text(encoding="utf-8"))
        forbidden_calls = {"eval", "exec", "getattr", "setattr", "delattr"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_calls, (
                    f"Forbidden dynamic call '{node.func.id}' detected in combined.py"
                )
