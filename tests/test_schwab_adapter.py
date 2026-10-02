"""Tests for the Schwab adapter and canonical model conformance.

These tests verify that:

1. The sample fixture deserialises into the canonical Pydantic models
   without error.
2. The parsed statement satisfies the cash-balance invariant and
   date-continuity rules enforced by the pipeline validators.
3. The ``SchwabAdapter`` skeleton is properly wired (``parse()`` raises
   ``NotImplementedError`` until extraction logic is implemented).
4. Edge cases in validation (out-of-range dates, misordered
   transactions, arithmetic mismatches) produce explicit ``ValueError``
   exceptions.
"""

from __future__ import annotations

import datetime

import pytest
from pydantic import ValidationError

from app.adapters.schwab import SchwabAdapter
from app.models.canonical import (
    CashTransaction,
    RawStatement,
    StatementSummary,
    TransactionType,
)
from app.pipeline.validator import validate_cash_balance, validate_date_continuity

# ---------------------------------------------------------------------------
# Model deserialisation
# ---------------------------------------------------------------------------


class TestCanonicalModelParsing:
    """The sample fixture must round-trip through Pydantic without error."""

    def test_fixture_loads_as_raw_statement(
        self, schwab_raw_statement: RawStatement
    ) -> None:
        """RawStatement is returned with correct broker and source file."""
        assert schwab_raw_statement.broker == "Charles Schwab"
        assert schwab_raw_statement.source_file == "schwab-stmt-2025-08.pdf"

    def test_summary_fields(self, schwab_raw_statement: RawStatement) -> None:
        """StatementSummary has expected period and balance values."""
        summary: StatementSummary = schwab_raw_statement.summary
        assert summary.account_number_masked == "****7842"
        assert summary.period_start == datetime.date(2025, 8, 1)
        assert summary.period_end == datetime.date(2025, 8, 31)
        assert summary.start_balance_cents == 1_000_000
        assert summary.end_balance_cents == 1_390_425

    def test_transaction_count(self, schwab_raw_statement: RawStatement) -> None:
        """Fixture contains exactly five transactions."""
        assert len(schwab_raw_statement.transactions) == 5

    def test_all_amounts_are_positive_integers(
        self, schwab_raw_statement: RawStatement
    ) -> None:
        """Every amount_cents is a positive int (no floats, no zeros)."""
        for txn in schwab_raw_statement.transactions:
            assert isinstance(txn.amount_cents, int)
            assert txn.amount_cents > 0

    def test_transaction_types_are_valid(
        self, schwab_raw_statement: RawStatement
    ) -> None:
        """Every transaction_type is a known TransactionType member."""
        for txn in schwab_raw_statement.transactions:
            assert txn.transaction_type in (
                TransactionType.CREDIT,
                TransactionType.DEBIT,
            )

    def test_model_is_frozen(self, schwab_raw_statement: RawStatement) -> None:
        """Canonical models are immutable after construction."""
        with pytest.raises(ValidationError):
            schwab_raw_statement.broker = "Fidelity"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Validation - happy path
# ---------------------------------------------------------------------------


class TestValidation:
    """Validators must pass on a well-formed statement."""

    def test_date_continuity_passes(
        self, schwab_raw_statement: RawStatement
    ) -> None:
        """No exception when dates are ordered and within the period."""
        validate_date_continuity(schwab_raw_statement)

    def test_cash_balance_passes(
        self, schwab_raw_statement: RawStatement
    ) -> None:
        """No exception when the balance equation holds."""
        validate_cash_balance(schwab_raw_statement)


# ---------------------------------------------------------------------------
# Validation - failure cases
# ---------------------------------------------------------------------------


class TestValidationFailures:
    """Validators must raise ``ValueError`` on bad data."""

    def test_balance_mismatch_raises(
        self, schwab_raw_statement: RawStatement
    ) -> None:
        """A one-cent discrepancy in end_balance triggers a ValueError."""
        bad_summary = StatementSummary(
            account_number_masked=schwab_raw_statement.summary.account_number_masked,
            period_start=schwab_raw_statement.summary.period_start,
            period_end=schwab_raw_statement.summary.period_end,
            start_balance_cents=schwab_raw_statement.summary.start_balance_cents,
            end_balance_cents=schwab_raw_statement.summary.end_balance_cents + 1,
        )
        bad_statement = RawStatement(
            source_file=schwab_raw_statement.source_file,
            broker=schwab_raw_statement.broker,
            summary=bad_summary,
            transactions=list(schwab_raw_statement.transactions),
        )
        with pytest.raises(ValueError, match="Cash balance mismatch"):
            validate_cash_balance(bad_statement)

    def test_out_of_range_date_raises(
        self, schwab_raw_statement: RawStatement
    ) -> None:
        """A transaction dated outside the period triggers a ValueError."""
        out_of_range_txn = CashTransaction(
            date=datetime.date(2025, 9, 5),
            description="LATE TRANSACTION",
            transaction_type=TransactionType.CREDIT,
            amount_cents=100,
            category=None,
        )
        bad_statement = RawStatement(
            source_file=schwab_raw_statement.source_file,
            broker=schwab_raw_statement.broker,
            summary=schwab_raw_statement.summary,
            transactions=[
                *schwab_raw_statement.transactions,
                out_of_range_txn,
            ],
        )
        with pytest.raises(ValueError, match="outside the statement period"):
            validate_date_continuity(bad_statement)

    def test_misordered_dates_raise(
        self, schwab_raw_statement: RawStatement
    ) -> None:
        """Transactions out of chronological order trigger a ValueError."""
        txns = list(schwab_raw_statement.transactions)
        reversed_txns = list(reversed(txns))
        bad_statement = RawStatement(
            source_file=schwab_raw_statement.source_file,
            broker=schwab_raw_statement.broker,
            summary=schwab_raw_statement.summary,
            transactions=reversed_txns,
        )
        with pytest.raises(ValueError, match="not in chronological order"):
            validate_date_continuity(bad_statement)

    def test_inverted_period_raises(self) -> None:
        """A statement where period_start > period_end triggers a ValueError."""
        summary = StatementSummary(
            account_number_masked="****0000",
            period_start=datetime.date(2025, 8, 31),
            period_end=datetime.date(2025, 8, 1),
            start_balance_cents=0,
            end_balance_cents=0,
        )
        statement = RawStatement(
            source_file="bad.pdf",
            broker="Test",
            summary=summary,
            transactions=[],
        )
        with pytest.raises(ValueError, match="period is inverted"):
            validate_date_continuity(statement)


# ---------------------------------------------------------------------------
# Model rejection of invalid data
# ---------------------------------------------------------------------------


class TestModelStrictness:
    """Pydantic strict mode must reject invalid inputs at construction time."""

    def test_float_amount_rejected(self) -> None:
        """Floating-point amounts must not be silently coerced to int."""
        with pytest.raises(ValidationError):
            CashTransaction(
                date=datetime.date(2025, 8, 1),
                description="BAD FLOAT",
                transaction_type=TransactionType.CREDIT,
                amount_cents=12.50,  # type: ignore[arg-type]
            )

    def test_zero_amount_rejected(self) -> None:
        """Zero-cent transactions are not valid."""
        with pytest.raises(ValidationError):
            CashTransaction(
                date=datetime.date(2025, 8, 1),
                description="ZERO",
                transaction_type=TransactionType.CREDIT,
                amount_cents=0,
            )

    def test_negative_amount_rejected(self) -> None:
        """Negative amounts must be rejected (use transaction_type instead)."""
        with pytest.raises(ValidationError):
            CashTransaction(
                date=datetime.date(2025, 8, 1),
                description="NEGATIVE",
                transaction_type=TransactionType.DEBIT,
                amount_cents=-500,
            )

    def test_empty_description_rejected(self) -> None:
        """Empty strings are not valid descriptions."""
        with pytest.raises(ValidationError):
            CashTransaction(
                date=datetime.date(2025, 8, 1),
                description="",
                transaction_type=TransactionType.CREDIT,
                amount_cents=100,
            )

    def test_invalid_transaction_type_rejected(self) -> None:
        """Unknown transaction types must be rejected."""
        with pytest.raises(ValidationError):
            CashTransaction(
                date=datetime.date(2025, 8, 1),
                description="UNKNOWN TYPE",
                transaction_type="refund",  # type: ignore[arg-type]
                amount_cents=100,
            )


# ---------------------------------------------------------------------------
# Adapter wiring
# ---------------------------------------------------------------------------


class TestSchwabAdapterWiring:
    """The adapter skeleton must be importable and structurally correct."""

    def test_adapter_is_subclass_of_base(self) -> None:
        """SchwabAdapter inherits from StatementAdapter."""
        from app.adapters.base import StatementAdapter

        assert issubclass(SchwabAdapter, StatementAdapter)

    def test_parse_raises_on_missing_file(self, tmp_path: pytest.TempPathFactory) -> None:
        """parse() raises FileNotFoundError for non-existent paths."""
        adapter = SchwabAdapter()
        with pytest.raises(FileNotFoundError):
            adapter.parse(tmp_path / "nonexistent.pdf")  # type: ignore[arg-type]

    def test_extract_summary(self) -> None:
        """_extract_summary() parses the account summary from page 1."""
        import datetime
        from unittest.mock import MagicMock

        adapter = SchwabAdapter()

        # 1. Create a mock page that returns the text our regexes expected
        mock_page = MagicMock()
        mock_page.extract_text.return_value = (
            "Account Number: ****1234\n"
            "Statement Period 08/01/25 to 08/31/25\n"
            "Starting Cash Balance $10,000.00\n"
            "Ending Cash Balance $13,904.25\n"
        )

        # 2. Create a mock PDF containing our page
        mock_pdf = MagicMock()
        mock_pdf.pages = [mock_page]

        # 3. Execute the extraction
        summary = adapter._extract_summary(mock_pdf)

        # 4. Assert the results match our canonical model expectations
        assert summary.account_number_masked == "****1234"
        assert summary.period_start == datetime.date(2025, 8, 1)
        assert summary.period_end == datetime.date(2025, 8, 31)
        assert summary.start_balance_cents == 1000000
        assert summary.end_balance_cents == 1390425

    def test_extract_transactions(self) -> None:
        """_extract_transactions() parses a mock transaction table."""
        from unittest.mock import MagicMock

        adapter = SchwabAdapter()

        # Build a mock page with a transaction table
        mock_page = MagicMock()
        mock_page.extract_text.return_value = "Cash Transaction Activity"
        mock_page.extract_tables.return_value = [
            [
                ["Date", "Description", "Amount"],
                ["08/04/2025", "SCHWAB BANK INTEREST", "$3.75"],
                ["08/20/2025", "WIRE FEE", "-$25.00"],
            ]
        ]

        mock_pdf = MagicMock()
        mock_pdf.pages = [mock_page]

        txns = adapter._extract_transactions(
            mock_pdf,
            period_start=datetime.date(2025, 8, 1),
            period_end=datetime.date(2025, 8, 31),
        )

        assert len(txns) == 2
        # Credit
        assert txns[0].description == "SCHWAB BANK INTEREST"
        assert txns[0].amount_cents == 375
        assert txns[0].transaction_type == TransactionType.CREDIT
        # Debit
        assert txns[1].description == "WIRE FEE"
        assert txns[1].amount_cents == 2500
        assert txns[1].transaction_type == TransactionType.DEBIT

    def test_parse_cents(self) -> None:
        """_parse_cents() converts US dollar string to postive integer cents."""
        adapter = SchwabAdapter()

        # Test standard formatting with dollar sign and commas
        assert adapter._parse_cents("$1,234.56") == 123456

        # Test without dollar sign or commas
        assert adapter._parse_cents("500.00") == 50000

    def test_matches_raises_on_missing_file(self, tmp_path: pytest.TempPathFactory) -> None:
        """matches() raises FileNotFoundError for non-existent paths."""
        adapter = SchwabAdapter()
        with pytest.raises(FileNotFoundError):
            adapter.matches(tmp_path / "nonexistent.pdf")  # type: ignore[arg-type]

