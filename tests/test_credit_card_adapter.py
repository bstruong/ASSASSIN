"""Unit and fixture tests for CreditCardStatementAdapter (Step 4)."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from app.adapters.credit_card import StandardCreditCardAdapter
from app.models.enums import (
    AccountDomain,
    AccountType,
    RunStatus,
    TransactionCategory,
    validate_category_for_domain,
)
from app.models.exceptions import (
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


def load_card_fixture(fixture_name: str) -> RawExtraction:
    """Helper to convert card JSON fixture text into RawExtraction."""
    fixture_path = FIXTURES_DIR / f"{fixture_name}.json"
    data = json.loads(fixture_path.read_text())
    text = data["raw_text"]

    payload = RawPayload(
        content_sha256="4" * 64,
        byte_length=len(text.encode("utf-8")),
        original_basename=f"{fixture_name}.pdf",
    )
    run = ExtractionRun(
        raw_payload_id=payload.raw_payload_id,
        adapter_id="standard_credit_card",
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


class TestCreditCardAdapter:
    """Test suite for StandardCreditCardAdapter."""

    def setup_method(self) -> None:
        self.adapter = StandardCreditCardAdapter()

    def test_card_happy(self) -> None:
        extraction = load_card_fixture("card_happy")
        account, statement, summary, txns = self.adapter.parse_canonical(extraction)

        assert account.account_domain == AccountDomain.REVOLVING_CREDIT
        assert account.account_type == AccountType.CREDIT_CARD
        assert account.account_mask == "*9876"

        assert statement.opening_balance_cents == 50000
        assert statement.closing_balance_cents == 28000
        assert statement.net_change_cents == -22000

        assert summary.previous_balance_cents == 50000
        assert summary.new_balance_cents == 28000
        assert summary.payments_credits_cents == 50000
        assert summary.purchases_cents == 25000
        assert summary.fees_charged_cents == 2500
        assert summary.interest_charged_cents == 500
        assert summary.payment_due_date == datetime.date(2025, 2, 25)
        assert summary.minimum_payment_due_cents == 3500

        assert len(txns) == 4
        # Payment decreases balance owed (-50000)
        assert txns[0].amount_cents == -50000
        assert txns[0].transaction_category == TransactionCategory.PAYMENT

        # Purchase increases balance owed (+25000)
        assert txns[1].amount_cents == 25000
        assert txns[1].transaction_category == TransactionCategory.PURCHASE

        # Fee increases balance owed (+2500)
        assert txns[2].amount_cents == 2500
        assert txns[2].transaction_category == TransactionCategory.FEE

        # Interest increases balance owed (+500)
        assert txns[3].amount_cents == 500
        assert txns[3].transaction_category == TransactionCategory.INTEREST_CHARGED

    def test_card_missing_due_date_fails(self) -> None:
        extraction = load_card_fixture("card_missing_due_date")
        with pytest.raises(MissingSectionError, match="Payment due date"):
            self.adapter.parse_canonical(extraction)

    def test_card_missing_minimum_payment_fails(self) -> None:
        extraction = load_card_fixture("card_happy")
        tampered_text = extraction.pages[0].page_text.replace(
            "Minimum Payment Due: $35.00\n",
            "",
        )
        extraction.pages[0] = RawPage(
            run_id=extraction.run.run_id,
            page_number=1,
            page_text=tampered_text,
            tokens=[],
        )
        with pytest.raises(MissingSectionError, match="Minimum payment due"):
            self.adapter.parse_canonical(extraction)

    def test_card_midcycle_interest(self) -> None:
        extraction = load_card_fixture("card_midcycle_interest")
        _account, statement, summary, txns = self.adapter.parse_canonical(extraction)

        assert summary.interest_charged_cents == 1500
        assert len(txns) == 2

        # Mid-cycle interest
        assert txns[0].amount_cents == 1500
        assert txns[0].transaction_category == TransactionCategory.INTEREST_CHARGED

        # Purchase
        assert txns[1].amount_cents == 10000
        assert txns[1].transaction_category == TransactionCategory.PURCHASE

        assert statement.closing_balance_cents == 111500
        assert statement.net_change_cents == 11500

    def test_fee_reversal_card(self) -> None:
        extraction = load_card_fixture("fee_reversal_card")
        _account, statement, summary, txns = self.adapter.parse_canonical(extraction)

        assert summary.fees_charged_cents == 0
        assert len(txns) == 2

        # Fee increases balance owed (+2500)
        assert txns[0].amount_cents == 2500
        assert txns[0].transaction_category == TransactionCategory.FEE

        # Fee reversal decreases balance owed (-2500)
        assert txns[1].amount_cents == -2500
        assert txns[1].transaction_category == TransactionCategory.FEE_REVERSAL

        assert statement.net_change_cents == 0

    def test_negative_minimum_payment_fails_invariant(self) -> None:
        extraction = load_card_fixture("card_happy")
        tampered_text = extraction.pages[0].page_text.replace(
            "Minimum Payment Due: $35.00",
            "Minimum Payment Due: -$10.00",
        )
        extraction.pages[0] = RawPage(
            run_id=extraction.run.run_id,
            page_number=1,
            page_text=tampered_text,
            tokens=[],
        )
        with pytest.raises(
            InvariantError, match=r"^minimum_payment_due_cents must be >= 0$"
        ):
            self.adapter.parse_canonical(extraction)

    def test_revolving_equation_mismatch_fails_invariant(self) -> None:
        extraction = load_card_fixture("card_happy")
        tampered_text = extraction.pages[0].page_text.replace(
            "Purchases: $250.00",
            "Purchases: $251.00",
        )
        extraction.pages[0] = RawPage(
            run_id=extraction.run.run_id,
            page_number=1,
            page_text=tampered_text,
            tokens=[],
        )
        with pytest.raises(InvariantError, match="Credit card balance equation failed"):
            self.adapter.parse_canonical(extraction)

    def test_glyph_category_disagreement_fails_loud(self) -> None:
        extraction = load_card_fixture("card_happy")
        tampered_text = extraction.pages[0].page_text.replace(
            "Payment Received - Thank You    -$500.00",
            "Payment Received - Thank You    +$500.00",
        )
        extraction.pages[0] = RawPage(
            run_id=extraction.run.run_id,
            page_number=1,
            page_text=tampered_text,
            tokens=[],
        )
        with pytest.raises(TokenError, match="disagrees with printed amount"):
            self.adapter.parse_canonical(extraction)

    def test_unsigned_card_amount_fails_loud(self) -> None:
        extraction = load_card_fixture("card_happy")
        tampered_text = extraction.pages[0].page_text.replace(
            "Grocery Store Purchase    +$250.00",
            "Grocery Store Purchase    $250.00",
        )
        extraction.pages[0] = RawPage(
            run_id=extraction.run.run_id,
            page_number=1,
            page_text=tampered_text,
            tokens=[],
        )
        with pytest.raises(TokenError, match="Ambiguous amount sign"):
            self.adapter.parse_canonical(extraction)

    def test_deposit_category_invalid_for_revolving_domain(self) -> None:
        with pytest.raises(ValueError, match="not valid for domain"):
            validate_category_for_domain(
                AccountDomain.REVOLVING_CREDIT, TransactionCategory.DEPOSIT
            )

    def test_domain_category_validation_invoked_on_parse(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(domain: AccountDomain, category: TransactionCategory) -> None:
            raise ValueError(
                f"Category {category!r} is not valid for domain {domain!r}."
            )

        monkeypatch.setattr(
            "app.adapters.credit_card.validate_category_for_domain", boom
        )
        with pytest.raises(ValueError, match="not valid for domain"):
            self.adapter.parse_canonical(load_card_fixture("card_happy"))
