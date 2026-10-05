"""Unit and fixture tests for InvestmentStatementAdapter and SchwabAdapter (Step 6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.adapters.schwab import SchwabAdapter
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
    SchemaDriftError,
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
        content_sha256="6" * 64,
        byte_length=len(text.encode("utf-8")),
        original_basename=f"{fixture_name}.pdf",
    )
    run = ExtractionRun(
        raw_payload_id=payload.raw_payload_id,
        adapter_id="schwab_brokerage",
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


def create_raw_extraction_from_text(text: str) -> RawExtraction:
    """Helper to wrap arbitrary string text into a RawExtraction."""
    payload = RawPayload(
        content_sha256="e" * 64,
        byte_length=len(text.encode("utf-8")),
        original_basename="custom_schwab.pdf",
    )
    run = ExtractionRun(
        raw_payload_id=payload.raw_payload_id,
        adapter_id="schwab_brokerage",
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


class TestInvestmentAdapter:
    """Test suite for SchwabAdapter conforming to InvestmentStatementAdapter."""

    def setup_method(self) -> None:
        self.adapter = SchwabAdapter()

    def test_brokerage_cash_happy(self) -> None:
        """Universal cash identity reconciliation on happy path."""
        extraction = load_fixture_as_raw_extraction("brokerage_cash_happy")
        account, statement, summary, txns = self.adapter.parse_canonical(extraction)

        assert account.account_domain == AccountDomain.CUSTODIAL_BROKERAGE
        assert account.account_type == AccountType.BROKERAGE_CASH
        assert account.account_mask == "****7842"
        assert account.institution == "Charles Schwab"

        assert statement.opening_balance_cents == 1_000_000
        assert statement.closing_balance_cents == 1_390_425
        assert statement.net_change_cents == 390_425
        assert statement.statement_id == summary.statement_id

        assert summary.opening_cash_cents == 1_000_000
        assert summary.closing_cash_cents == 1_390_425
        assert summary.opening_portfolio_cents is None
        assert summary.closing_portfolio_cents is None

        assert len(txns) == 5
        assert txns[0].amount_cents == 200_000
        assert txns[0].transaction_category == TransactionCategory.TRANSFER_IN
        assert txns[1].amount_cents == 200_000
        assert txns[1].transaction_category == TransactionCategory.TRANSFER_IN
        assert txns[2].amount_cents == -10_000
        assert txns[2].transaction_category == TransactionCategory.TRANSFER_OUT
        assert txns[3].amount_cents == 10_000
        assert txns[3].transaction_category == TransactionCategory.DIVIDEND
        assert txns[4].amount_cents == -9_575
        assert txns[4].transaction_category == TransactionCategory.FEE

        for t in txns:
            assert t.statement_id == statement.statement_id

    def test_brokerage_portfolio_happy(self) -> None:
        """Cash identity plus full portfolio summary bridge reconciliation."""
        extraction = load_fixture_as_raw_extraction("brokerage_portfolio_happy")
        account, statement, summary, _ = self.adapter.parse_canonical(extraction)

        assert account.account_domain == AccountDomain.CUSTODIAL_BROKERAGE
        assert statement.opening_balance_cents == 1_000_000
        assert statement.closing_balance_cents == 1_390_425

        # Portfolio bridge assertion
        assert summary.opening_portfolio_cents == 5_000_000
        assert summary.transfers_in_cents == 400_000
        assert summary.transfers_out_cents == 10_000
        assert summary.income_dividends_cents == 10_000
        assert summary.realized_gains_cents == 50_000
        assert summary.unrealized_gains_cents == -25_000
        assert summary.closing_portfolio_cents == 5_425_000

        # Verify identity: 5000000 + 400000 - 10000 + 10000 + 50000 + (-25000) = 5425000
        computed_closing = (
            summary.opening_portfolio_cents
            + summary.transfers_in_cents
            - summary.transfers_out_cents
            + summary.income_dividends_cents
            + summary.realized_gains_cents
            + summary.unrealized_gains_cents
        )
        assert computed_closing == summary.closing_portfolio_cents

    def test_cash_balance_mismatch_1_cent_fails(self) -> None:
        """1-cent discrepancy in cash closing balance must fail loudly."""
        extraction = load_fixture_as_raw_extraction("brokerage_cash_happy")
        # Change ending cash balance by 1 cent ($13,904.26 instead of $13,904.25)
        bad_text = extraction.pages[0].page_text.replace(
            "Ending Cash Balance: $13,904.25", "Ending Cash Balance: $13,904.26"
        )
        bad_extraction = create_raw_extraction_from_text(bad_text)

        with pytest.raises(
            InvariantError, match=r"Balance reconciliation failure|net_change_cents"
        ):
            self.adapter.parse_canonical(bad_extraction)

    def test_portfolio_bridge_mismatch_1_cent_fails(self) -> None:
        """1-cent discrepancy in stated ending portfolio value must fail loudly."""
        extraction = load_fixture_as_raw_extraction("brokerage_portfolio_happy")
        # Change ending portfolio value by 1 cent ($54,250.01 instead of $54,250.00)
        bad_text = extraction.pages[0].page_text.replace(
            "Ending Portfolio Value: $54,250.00", "Ending Portfolio Value: $54,250.01"
        )
        bad_extraction = create_raw_extraction_from_text(bad_text)

        with pytest.raises(InvariantError, match=r"Portfolio bridge mismatch"):
            self.adapter.parse_canonical(bad_extraction)

    def test_incomplete_portfolio_bridge_fails(self) -> None:
        """Missing printed component in portfolio bridge must fail loudly, not assume zero."""
        extraction = load_fixture_as_raw_extraction("brokerage_portfolio_happy")
        # Remove Realized Gain/Loss term
        bad_text = extraction.pages[0].page_text.replace(
            "Realized Gain/Loss: $500.00\n", ""
        )
        bad_extraction = create_raw_extraction_from_text(bad_text)

        with pytest.raises(
            MissingSectionError, match=r"Missing summary field matching pattern"
        ):
            self.adapter.parse_canonical(bad_extraction)

    def test_ambiguous_account_masks_fail(self) -> None:
        """Multiple distinct account masks on page 1 must raise AmbiguousAccountsError."""
        text = (
            "CHARLES SCHWAB\n"
            "Account Number: *7842\n"
            "Secondary Account: *9999\n"
            "Statement Period: 2025-08-01 to 2025-08-31\n"
            "ACCOUNT SUMMARY\n"
            "Starting Cash Balance: $1,000.00\n"
            "Ending Cash Balance: $1,000.00\n"
            "TRANSACTION ACTIVITY\n"
            "Date    Description    Amount\n"
            "END OF STATEMENT"
        )
        extraction = create_raw_extraction_from_text(text)
        with pytest.raises(
            AmbiguousAccountsError, match=r"Multiple distinct account masks"
        ):
            self.adapter.parse_canonical(extraction)

    def test_ambiguous_account_sections_fail(self) -> None:
        """Multiple account summary sections (Cash and Margin) must raise AmbiguousAccountsError."""
        text = (
            "CHARLES SCHWAB\n"
            "Account Number: *7842\n"
            "Statement Period: 2025-08-01 to 2025-08-31\n"
            "Brokerage Cash Summary\n"
            "Margin Account Summary\n"
            "ACCOUNT SUMMARY\n"
            "Starting Cash Balance: $1,000.00\n"
            "Ending Cash Balance: $1,000.00\n"
            "TRANSACTION ACTIVITY\n"
            "Date    Description    Amount\n"
            "END OF STATEMENT"
        )
        extraction = create_raw_extraction_from_text(text)
        with pytest.raises(
            AmbiguousAccountsError, match=r"Both Cash and Margin account sections"
        ):
            self.adapter.parse_canonical(extraction)

    def test_margin_account_type_detected(self) -> None:
        """Margin account statement assigns AccountType.BROKERAGE_MARGIN."""
        text = (
            "CHARLES SCHWAB\n"
            "Individual Margin Brokerage Account\n"
            "Account Number: *7842\n"
            "Statement Period: 2025-08-01 to 2025-08-31\n"
            "ACCOUNT SUMMARY\n"
            "Starting Cash Balance: $1,000.00\n"
            "Ending Cash Balance: $1,500.00\n"
            "TRANSACTION ACTIVITY\n"
            "Date    Description    Amount\n"
            "2025-08-10    Wire Transfer In    +$500.00\n"
            "END OF STATEMENT"
        )
        extraction = create_raw_extraction_from_text(text)
        account, stmt, _, _ = self.adapter.parse_canonical(extraction)
        assert account.account_type == AccountType.BROKERAGE_MARGIN
        assert stmt.closing_balance_cents == 150000

    def test_missing_mandatory_section_marker_fails(self) -> None:
        """Missing declared section marker raises MissingSectionError."""
        text = (
            "CHARLES SCHWAB\n"
            "Account Number: *7842\n"
            "Statement Period: 2025-08-01 to 2025-08-31\n"
            "ACCOUNT SUMMARY\n"
            "Starting Cash Balance: $1,000.00\n"
            "Ending Cash Balance: $1,000.00\n"
            "NO_RECORDS_HERE\n"
            "END OF STATEMENT"
        )
        extraction = create_raw_extraction_from_text(text)
        with pytest.raises(
            MissingSectionError,
            match=r"Mandatory section marker 'TRANSACTION ACTIVITY'",
        ):
            self.adapter.parse_canonical(extraction)

    def test_schema_drift_extra_column_fails(self) -> None:
        """Transaction table with extra column headers raises SchemaDriftError."""
        text = (
            "CHARLES SCHWAB\n"
            "Account Number: *7842\n"
            "Statement Period: 2025-08-01 to 2025-08-31\n"
            "ACCOUNT SUMMARY\n"
            "Starting Cash Balance: $1,000.00\n"
            "Ending Cash Balance: $1,000.00\n"
            "TRANSACTION ACTIVITY\n"
            "Date    Description    ExtraCol    Amount\n"
            "2025-08-10    Deposit    Extra    +$100.00\n"
            "END OF STATEMENT"
        )
        extraction = create_raw_extraction_from_text(text)
        with pytest.raises(SchemaDriftError, match=r"Header drift"):
            self.adapter.parse_canonical(extraction)

    def test_schema_drift_ragged_row_fails(self) -> None:
        """Transaction table with missing column in row raises SchemaDriftError."""
        text = (
            "CHARLES SCHWAB\n"
            "Account Number: *7842\n"
            "Statement Period: 2025-08-01 to 2025-08-31\n"
            "ACCOUNT SUMMARY\n"
            "Starting Cash Balance: $1,000.00\n"
            "Ending Cash Balance: $1,100.00\n"
            "TRANSACTION ACTIVITY\n"
            "Date    Description    Amount\n"
            "2025-08-10    +$100.00\n"
            "END OF STATEMENT"
        )
        extraction = create_raw_extraction_from_text(text)
        with pytest.raises(SchemaDriftError, match=r"Ragged row"):
            self.adapter.parse_canonical(extraction)

    def test_category_sign_violation_fee_positive_fails(self) -> None:
        """Fee with positive delta violates sign rules in brokerage domain."""
        text = (
            "CHARLES SCHWAB\n"
            "Account Number: *7842\n"
            "Statement Period: 2025-08-01 to 2025-08-31\n"
            "ACCOUNT SUMMARY\n"
            "Starting Cash Balance: $1,000.00\n"
            "Ending Cash Balance: $1,025.00\n"
            "TRANSACTION ACTIVITY\n"
            "Date    Description    Amount\n"
            "2025-08-10    Service Fee Charged    +$25.00\n"
            "END OF STATEMENT"
        )
        extraction = create_raw_extraction_from_text(text)
        with pytest.raises(TokenError, match=r"requires negative amount_cents"):
            self.adapter.parse_canonical(extraction)

    def test_category_sign_violation_dividend_negative_fails(self) -> None:
        """Dividend with negative delta violates sign rules in brokerage domain."""
        text = (
            "CHARLES SCHWAB\n"
            "Account Number: *7842\n"
            "Statement Period: 2025-08-01 to 2025-08-31\n"
            "ACCOUNT SUMMARY\n"
            "Starting Cash Balance: $1,000.00\n"
            "Ending Cash Balance: $950.00\n"
            "TRANSACTION ACTIVITY\n"
            "Date    Description    Amount\n"
            "2025-08-10    Qualifying Dividend    -$50.00\n"
            "END OF STATEMENT"
        )
        extraction = create_raw_extraction_from_text(text)
        with pytest.raises(TokenError, match=r"requires positive amount_cents"):
            self.adapter.parse_canonical(extraction)

    def test_trade_cash_both_signs_allowed(self) -> None:
        """TRADE_CASH allows both negative (purchase) and positive (sale) cash deltas."""
        text = (
            "CHARLES SCHWAB\n"
            "Account Number: *7842\n"
            "Statement Period: 2025-08-01 to 2025-08-31\n"
            "ACCOUNT SUMMARY\n"
            "Starting Cash Balance: $1,000.00\n"
            "Ending Cash Balance: $1,200.00\n"
            "TRANSACTION ACTIVITY\n"
            "Date    Description    Amount\n"
            "2025-08-05    Bought 10 Shares XYZ    -$500.00\n"
            "2025-08-15    Sold 10 Shares ABC    +$700.00\n"
            "END OF STATEMENT"
        )
        extraction = create_raw_extraction_from_text(text)
        _, stmt, _, txns = self.adapter.parse_canonical(extraction)

        assert stmt.closing_balance_cents == 120_000
        assert len(txns) == 2
        assert txns[0].transaction_category == TransactionCategory.TRADE_CASH
        assert txns[0].amount_cents == -50_000
        assert txns[1].transaction_category == TransactionCategory.TRADE_CASH
        assert txns[1].amount_cents == 70_000

    def test_out_of_cycle_transaction_fails(self) -> None:
        """Transaction post-date outside statement period fails validation."""
        text = (
            "CHARLES SCHWAB\n"
            "Account Number: *7842\n"
            "Statement Period: 2025-08-01 to 2025-08-31\n"
            "ACCOUNT SUMMARY\n"
            "Starting Cash Balance: $1,000.00\n"
            "Ending Cash Balance: $1,100.00\n"
            "TRANSACTION ACTIVITY\n"
            "Date    Description    Amount\n"
            "2025-09-05    Electronic Deposit Funds Received    +$100.00\n"
            "END OF STATEMENT"
        )
        extraction = create_raw_extraction_from_text(text)
        with pytest.raises(InvariantError, match=r"outside statement period"):
            self.adapter.parse_canonical(extraction)

    def test_invalid_currency_fails_loudly(self) -> None:
        """Currency without exactly 2 decimal places raises TokenError."""
        text = (
            "CHARLES SCHWAB\n"
            "Account Number: *7842\n"
            "Statement Period: 2025-08-01 to 2025-08-31\n"
            "ACCOUNT SUMMARY\n"
            "Starting Cash Balance: $1,000.00\n"
            "Ending Cash Balance: $1,100.00\n"
            "TRANSACTION ACTIVITY\n"
            "Date    Description    Amount\n"
            "2025-08-10    Electronic Deposit Funds Received    +$100.5\n"
            "END OF STATEMENT"
        )
        extraction = create_raw_extraction_from_text(text)
        with pytest.raises(
            TokenError, match=r"cents component must be exactly 2 digits"
        ):
            self.adapter.parse_canonical(extraction)
