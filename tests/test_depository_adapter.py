"""Unit and fixture tests for DepositoryStatementAdapter (Step 3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.adapters.depository import StandardDepositoryAdapter
from app.models.enums import (
    AccountDomain,
    AccountType,
    RunStatus,
    TransactionCategory,
)
from app.models.exceptions import (
    AmbiguousAccountsError,
    InvariantError,
    MissingSectionError,
    TokenError,
)
from app.models.raw import (
    ExtractionRun,
    RawExtraction,
    RawPage,
    RawPayload,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture_as_raw_extraction(fixture_name: str) -> RawExtraction:
    """Helper to convert JSON fixture text into a RawExtraction."""
    fixture_path = FIXTURES_DIR / f"{fixture_name}.json"
    data = json.loads(fixture_path.read_text())
    text = data["raw_text"]

    payload = RawPayload(
        content_sha256="1" * 64,
        byte_length=len(text.encode("utf-8")),
        original_basename=f"{fixture_name}.pdf",
    )
    run = ExtractionRun(
        raw_payload_id=payload.raw_payload_id,
        adapter_id="standard_depository",
        adapter_version="1.0.0",
        status=RunStatus.EXTRACTED,
    )
    page = RawPage(
        run_id=run.run_id,
        page_number=1,
        page_text=text,
        tokens=[],
    )
    return RawExtraction(payload=payload, run=run, pages=[page])


class TestDepositoryAdapter:
    """Test suite for StandardDepositoryAdapter."""

    def setup_method(self) -> None:
        self.adapter = StandardDepositoryAdapter()

    def test_checking_happy(self) -> None:
        extraction = load_fixture_as_raw_extraction("checking_happy")
        account, statement, summary, txns = self.adapter.parse_canonical(extraction)

        assert account.account_domain == AccountDomain.DEPOSITORY
        assert account.account_type == AccountType.CHECKING
        assert account.account_mask == "*1234"

        assert statement.opening_balance_cents == 100000
        assert statement.closing_balance_cents == 129500
        assert statement.net_change_cents == 29500

        assert summary.deposits_cents == 50000
        assert summary.withdrawals_cents == 20000
        assert summary.interest_paid_cents == 500
        assert summary.fees_cents == 1000

        assert len(txns) == 4
        assert txns[0].amount_cents == 50000
        assert txns[0].transaction_category == TransactionCategory.DEPOSIT
        assert txns[1].amount_cents == -20000
        assert txns[1].transaction_category == TransactionCategory.WITHDRAWAL
        assert txns[2].amount_cents == -1000
        assert txns[2].transaction_category == TransactionCategory.FEE
        assert txns[3].amount_cents == 500
        assert txns[3].transaction_category == TransactionCategory.INTEREST_PAID

    def test_savings_interest(self) -> None:
        extraction = load_fixture_as_raw_extraction("savings_interest")
        account, statement, summary, txns = self.adapter.parse_canonical(extraction)

        assert account.account_type == AccountType.SAVINGS
        assert account.account_mask == "*5678"
        assert statement.opening_balance_cents == 500000
        assert statement.closing_balance_cents == 502500
        assert statement.net_change_cents == 2500

        assert summary.interest_paid_cents == 2500
        assert len(txns) == 1
        assert txns[0].amount_cents == 2500
        assert txns[0].transaction_category == TransactionCategory.INTEREST_PAID

    def test_fee_reversal_bank(self) -> None:
        extraction = load_fixture_as_raw_extraction("fee_reversal_bank")
        account, statement, summary, txns = self.adapter.parse_canonical(extraction)

        assert account.account_mask == "*1234"
        assert summary.fees_cents == 0
        assert len(txns) == 2
        assert txns[0].amount_cents == -1500
        assert txns[0].transaction_category == TransactionCategory.FEE
        assert txns[1].amount_cents == 1500
        assert txns[1].transaction_category == TransactionCategory.FEE_REVERSAL
        assert statement.net_change_cents == 0

    def test_checking_plus_savings_fails_ambiguous_accounts(self) -> None:
        extraction = load_fixture_as_raw_extraction("checking_plus_savings_page1")
        with pytest.raises(AmbiguousAccountsError):
            self.adapter.parse_canonical(extraction)

    def test_ambiguous_sign_fails_token_error(self) -> None:
        extraction = load_fixture_as_raw_extraction("ambiguous_sign")
        with pytest.raises(TokenError):
            self.adapter.parse_canonical(extraction)

    def test_missing_section_fails(self) -> None:
        payload = RawPayload(
            content_sha256="2" * 64,
            byte_length=100,
            original_basename="broken.pdf",
        )
        run = ExtractionRun(
            raw_payload_id=payload.raw_payload_id,
            adapter_id="standard_depository",
            adapter_version="1.0.0",
            status=RunStatus.EXTRACTED,
        )
        page = RawPage(
            run_id=run.run_id,
            page_number=1,
            page_text="STANDARD BANK Checking Account *1234 but no summary sections",
            tokens=[],
        )
        extraction = RawExtraction(payload=payload, run=run, pages=[page])

        with pytest.raises(MissingSectionError):
            self.adapter.parse_canonical(extraction)

    def test_running_balance_mismatch_fails_invariant_error(self) -> None:
        extraction = load_fixture_as_raw_extraction("checking_happy")
        # Tamper running balance on row 1
        tampered_text = extraction.pages[0].page_text.replace(
            "2025-01-05    Payroll Deposit    +$500.00    $1,500.00",
            "2025-01-05    Payroll Deposit    +$500.00    $1,499.00",
        )
        extraction.pages[0] = RawPage(
            run_id=extraction.run.run_id,
            page_number=1,
            page_text=tampered_text,
            tokens=[],
        )
        with pytest.raises(InvariantError, match="Running balance mismatch"):
            self.adapter.parse_canonical(extraction)

    def test_depository_bucket_equation_mismatch_fails(self) -> None:
        extraction = load_fixture_as_raw_extraction("checking_happy")
        # Tamper deposits summary bucket
        tampered_text = extraction.pages[0].page_text.replace(
            "Deposits and Additions: $500.00",
            "Deposits and Additions: $501.00",
        )
        extraction.pages[0] = RawPage(
            run_id=extraction.run.run_id,
            page_number=1,
            page_text=tampered_text,
            tokens=[],
        )
        with pytest.raises(InvariantError, match="Depository bucket equation failed"):
            self.adapter.parse_canonical(extraction)

    def test_missing_summary_amount_fails(self) -> None:
        extraction = load_fixture_as_raw_extraction("checking_happy")
        extraction.pages[0] = RawPage(
            run_id=extraction.run.run_id,
            page_number=1,
            page_text=extraction.pages[0].page_text.replace(
                "Starting Balance: $1,000.00\n",
                "",
            ),
            tokens=[],
        )
        with pytest.raises(
            MissingSectionError, match=r"^Missing summary field: Starting Balance$"
        ):
            self.adapter.parse_summary(extraction)

    def test_negative_opening_balance_stays_signed(self) -> None:
        """Printed minus after the dollar sign is a signed balance, not a magnitude."""
        extraction = load_fixture_as_raw_extraction("checking_happy")
        extraction.pages[0] = RawPage(
            run_id=extraction.run.run_id,
            page_number=1,
            page_text=extraction.pages[0].page_text.replace(
                "Starting Balance: $1,000.00",
                "Starting Balance: $-10.00",
            ),
            tokens=[],
        )
        opening_cents, closing_cents, summary = self.adapter.parse_summary(extraction)
        assert opening_cents == -1000
        assert closing_cents == 129500
        assert summary.deposits_cents == 50000

    def test_parenthetical_closing_balance_stays_signed(self) -> None:
        """Accounting parentheses on a balance stay negative integer cents."""
        extraction = load_fixture_as_raw_extraction("checking_happy")
        extraction.pages[0] = RawPage(
            run_id=extraction.run.run_id,
            page_number=1,
            page_text=extraction.pages[0].page_text.replace(
                "Ending Balance: $1,295.00",
                "Ending Balance: ($1,295.00)",
            ),
            tokens=[],
        )
        _opening_cents, closing_cents, _summary = self.adapter.parse_summary(extraction)
        assert closing_cents == -129500

    @pytest.mark.parametrize(
        ("printed", "replacement", "message"),
        [
            (
                "Deposits and Additions: $500.00",
                "Deposits and Additions: ($500.00)",
                "deposits_cents must be >= 0",
            ),
            (
                "Withdrawals and Subtractions: $200.00",
                "Withdrawals and Subtractions: -$200.00",
                "withdrawals_cents must be >= 0",
            ),
            (
                "Interest Paid: $5.00",
                "Interest Paid: $-5.00",
                "interest_paid_cents must be >= 0",
            ),
            (
                "Fees Charged: $10.00",
                "Fees Charged: ($10.00)",
                "fees_cents must be >= 0",
            ),
        ],
    )
    def test_negative_sidecar_magnitude_fails_before_summary(
        self, printed: str, replacement: str, message: str
    ) -> None:
        """A printed negative bucket must not be flipped positive with abs()."""
        extraction = load_fixture_as_raw_extraction("checking_happy")
        extraction.pages[0] = RawPage(
            run_id=extraction.run.run_id,
            page_number=1,
            page_text=extraction.pages[0].page_text.replace(printed, replacement),
            tokens=[],
        )
        with pytest.raises(InvariantError, match=rf"^{message}$"):
            self.adapter.parse_summary(extraction)
