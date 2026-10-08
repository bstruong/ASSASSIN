"""Unit tests for Raw and Canonical models under Step 1."""

from __future__ import annotations

import datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.canonical import (
    Account,
    BrokerageSummary,
    CanonicalStatement,
    CanonicalTransaction,
    DepositorySummary,
    Holding,
)
from app.models.enums import (
    AccountDomain,
    AccountType,
    CurrencyCode,
    RunStatus,
    TransactionCategory,
)
from app.models.raw import (
    ExtractionRun,
    RawExtraction,
    RawPage,
    RawPayload,
    RawToken,
)
from app.models.schema import TableSchema


class TestRawTokenGeometry:
    """RawToken asserts integer millipoints and geometry invariants."""

    def test_valid_token(self) -> None:
        token = RawToken(
            raw_page_id=uuid4(),
            token_kind="word",
            token_text="Deposit",
            x0_mp=10000,
            y0_mp=20000,
            x1_mp=50000,
            y1_mp=30000,
        )
        assert token.x0_mp == 10000
        assert token.y0_mp == 20000
        assert token.x1_mp == 50000
        assert token.y1_mp == 30000
        assert token.token_kind == "word"

    def test_x1_le_x0_raises(self) -> None:
        with pytest.raises(ValidationError):
            RawToken(
                raw_page_id=uuid4(),
                token_kind="word",
                token_text="Bad",
                x0_mp=50000,
                y0_mp=10000,
                x1_mp=50000,
                y1_mp=20000,
            )

    def test_y1_le_y0_raises(self) -> None:
        with pytest.raises(ValidationError):
            RawToken(
                raw_page_id=uuid4(),
                token_kind="cell",
                token_text="Bad",
                x0_mp=10000,
                y0_mp=30000,
                x1_mp=20000,
                y1_mp=25000,
            )

    def test_negative_x0_raises(self) -> None:
        with pytest.raises(ValidationError):
            RawToken(
                raw_page_id=uuid4(),
                token_kind="word",
                token_text="Bad",
                x0_mp=-1,
                y0_mp=10000,
                x1_mp=20000,
                y1_mp=20000,
            )


class TestExtractionRunStatus:
    """ExtractionRun enforces error_code correlation with failed status."""

    def test_failed_status_with_error_code(self) -> None:
        run = ExtractionRun(
            raw_payload_id=uuid4(),
            adapter_id="test_adapter",
            adapter_version="1.0.0",
            status=RunStatus.FAILED,
            error_code="SchemaDriftError",
            error_message="Column count mismatch",
        )
        assert run.status == RunStatus.FAILED
        assert run.error_code == "SchemaDriftError"

    def test_failed_status_without_error_code_raises(self) -> None:
        with pytest.raises(ValidationError):
            ExtractionRun(
                raw_payload_id=uuid4(),
                adapter_id="test_adapter",
                adapter_version="1.0.0",
                status=RunStatus.FAILED,
                error_code=None,
            )

    def test_extracted_status_with_error_code_raises(self) -> None:
        with pytest.raises(ValidationError):
            ExtractionRun(
                raw_payload_id=uuid4(),
                adapter_id="test_adapter",
                adapter_version="1.0.0",
                status=RunStatus.EXTRACTED,
                error_code="SomeError",
            )


class TestRawExtractionEnvelope:
    """RawExtraction groups payload, run, and pages."""

    def test_valid_raw_extraction(self) -> None:
        payload = RawPayload(
            content_sha256="a" * 64,
            byte_length=12345,
            original_basename="stmt.pdf",
        )
        run = ExtractionRun(
            raw_payload_id=payload.raw_payload_id,
            adapter_id="test_adapter",
            adapter_version="1.0.0",
            status=RunStatus.EXTRACTED,
        )
        page = RawPage(
            run_id=run.run_id,
            page_number=1,
            page_text="Page 1 text",
            tokens=[],
        )
        extraction = RawExtraction(
            payload=payload,
            run=run,
            pages=[page],
        )
        assert extraction.payload.original_basename == "stmt.pdf"
        assert len(extraction.pages) == 1


class TestCanonicalAccount:
    """Account enforces domain and type consistency."""

    def test_valid_checking_account(self) -> None:
        acc = Account(
            institution="Chase",
            account_mask="1234",
            account_domain=AccountDomain.DEPOSITORY,
            account_type=AccountType.CHECKING,
            currency=CurrencyCode.USD,
        )
        assert acc.account_domain == AccountDomain.DEPOSITORY

    def test_mismatched_domain_type_raises(self) -> None:
        with pytest.raises(ValidationError):
            Account(
                institution="Chase",
                account_mask="1234",
                account_domain=AccountDomain.DEPOSITORY,
                account_type=AccountType.CREDIT_CARD,
            )


class TestCanonicalStatementInvariants:
    """CanonicalStatement enforces date and net change invariants."""

    def test_valid_statement(self) -> None:
        stmt = CanonicalStatement(
            account_id=uuid4(),
            run_id=uuid4(),
            raw_payload_id=uuid4(),
            statement_start_date=datetime.date(2025, 1, 1),
            statement_end_date=datetime.date(2025, 1, 31),
            opening_balance_cents=100000,
            closing_balance_cents=150000,
            net_change_cents=50000,
        )
        assert stmt.net_change_cents == 50000

    def test_net_change_mismatch_raises(self) -> None:
        with pytest.raises(ValidationError):
            CanonicalStatement(
                account_id=uuid4(),
                run_id=uuid4(),
                raw_payload_id=uuid4(),
                statement_start_date=datetime.date(2025, 1, 1),
                statement_end_date=datetime.date(2025, 1, 31),
                opening_balance_cents=100000,
                closing_balance_cents=150000,
                net_change_cents=49999,
            )

    def test_inverted_dates_raises(self) -> None:
        with pytest.raises(ValidationError):
            CanonicalStatement(
                account_id=uuid4(),
                run_id=uuid4(),
                raw_payload_id=uuid4(),
                statement_start_date=datetime.date(2025, 2, 1),
                statement_end_date=datetime.date(2025, 1, 31),
                opening_balance_cents=100000,
                closing_balance_cents=150000,
                net_change_cents=50000,
            )


class TestCanonicalTransactionInvariants:
    """CanonicalTransaction enforces non-zero signed amount_cents."""

    def test_valid_transaction(self) -> None:
        txn = CanonicalTransaction(
            statement_id=uuid4(),
            post_date=datetime.date(2025, 1, 15),
            amount_cents=2500,
            description="Payroll Deposit",
            transaction_category=TransactionCategory.DEPOSIT,
        )
        assert txn.amount_cents == 2500

    def test_zero_amount_raises(self) -> None:
        with pytest.raises(ValidationError):
            CanonicalTransaction(
                statement_id=uuid4(),
                post_date=datetime.date(2025, 1, 15),
                amount_cents=0,
                description="Zero Fee",
                transaction_category=TransactionCategory.FEE,
            )


class TestSidecarSummaries:
    """Sidecar summaries enforce schema constraints."""

    def test_depository_summary_negative_cents_raises(self) -> None:
        with pytest.raises(ValidationError):
            DepositorySummary(
                statement_id=uuid4(),
                deposits_cents=-100,
                withdrawals_cents=200,
                interest_paid_cents=0,
                fees_cents=0,
            )

    def test_brokerage_summary_partial_portfolio_raises(self) -> None:
        with pytest.raises(ValidationError):
            BrokerageSummary(
                statement_id=uuid4(),
                opening_cash_cents=10000,
                closing_cash_cents=12000,
                opening_portfolio_cents=50000,
                closing_portfolio_cents=None,
            )


class TestHoldingModel:
    def test_zero_quantity_rejected(self) -> None:
        with pytest.raises(ValidationError, match="quantity_nanos must be non-zero"):
            Holding(
                statement_id=uuid4(),
                as_of_date=datetime.date(2025, 8, 31),
                symbol="SYN",
                description="SYNTHETIC Equity",
                quantity_nanos=0,
                market_value_cents=100,
            )

    def test_short_market_value_allowed(self) -> None:
        holding = Holding(
            statement_id=uuid4(),
            as_of_date=datetime.date(2025, 8, 31),
            symbol="SHRT",
            description="SYNTHETIC Short",
            quantity_nanos=-1_000_000_000,
            market_value_cents=-5000,
        )
        assert holding.market_value_cents == -5000


class TestTableSchemaModel:
    """TableSchema is frozen and strictly defined."""

    def test_table_schema_creation(self) -> None:
        schema = TableSchema(
            name="transactions",
            headers=("date", "description", "amount"),
            section_markers=("Activity Summary",),
        )
        assert schema.name == "transactions"
        assert schema.headers == ("date", "description", "amount")
