#!/usr/bin/env python3
"""QA verification script for Step 4: Credit Card Statement Adapter.

Validates revolving credit balance equation, transaction signs,
payment due dates, minimum payment fields, and fail-loud invariant checks.
"""

import json
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from app.adapters.credit_card import StandardCreditCardAdapter
from app.models.canonical import CreditCardSummary
from app.models.enums import AccountDomain, AccountType, RunStatus
from app.models.exceptions import InvariantError, MissingSectionError
from app.models.raw import ExtractionRun, RawExtraction, RawPage, RawPayload

FIXTURES_DIR = Path(__file__).parents[1] / "tests" / "fixtures"


def load_card_fixture(name: str) -> RawExtraction:
    path = FIXTURES_DIR / f"{name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    text = data["raw_text"]
    payload = RawPayload(
        content_sha256="4" * 64,
        byte_length=len(text.encode()),
        original_basename=f"{name}.pdf",
    )
    run = ExtractionRun(
        raw_payload_id=payload.raw_payload_id,
        adapter_id="standard_credit_card",
        adapter_version="1.0.0",
        status=RunStatus.EXTRACTED,
    )
    return RawExtraction(
        payload=payload,
        run=run,
        pages=[RawPage(run_id=run.run_id, page_number=1, page_text=text)],
    )


def test_credit_card_adapter_reconciliation() -> None:
    print(
        "Testing StandardCreditCardAdapter revolving balance reconciliation on fixture..."
    )
    adapter = StandardCreditCardAdapter()
    raw = load_card_fixture("card_happy")

    account, statement, summary, txns = adapter.parse_canonical(raw)
    assert account.account_domain == AccountDomain.REVOLVING_CREDIT
    assert account.account_type == AccountType.CREDIT_CARD
    assert statement.opening_balance_cents == 50000
    assert statement.closing_balance_cents == 28000
    assert statement.net_change_cents == -22000
    assert summary.previous_balance_cents == 50000
    assert summary.payments_credits_cents == 50000
    assert summary.purchases_cents == 25000
    assert summary.fees_charged_cents == 2500
    assert summary.interest_charged_cents == 500
    assert summary.new_balance_cents == 28000
    assert summary.minimum_payment_due_cents == 3500
    assert len(txns) == 4
    print("  ✓ Revolving credit balance equation reconciled perfectly.")


def test_credit_card_equation_mismatch_fails() -> None:
    print("Testing CreditCardAdapter fail-loud on balance equation violation...")
    adapter = StandardCreditCardAdapter()
    raw = load_card_fixture("card_happy")
    # Mutate New Balance Total by 1 cent
    corrupted_text = raw.pages[0].page_text.replace(
        "New Balance: $280.00", "New Balance: $280.01"
    )
    raw_corrupted = RawExtraction(
        payload=raw.payload,
        run=raw.run,
        pages=[RawPage(run_id=raw.run.run_id, page_number=1, page_text=corrupted_text)],
    )

    try:
        adapter.parse_canonical(raw_corrupted)
        raise AssertionError(
            "Expected InvariantError for credit card equation mismatch"
        )
    except InvariantError:
        pass
    print("  ✓ Balance equation mismatch rejected with InvariantError.")


def test_missing_due_date_fails() -> None:
    print("Testing CreditCardAdapter fail-loud on missing required payment due date...")
    adapter = StandardCreditCardAdapter()
    raw = load_card_fixture("card_missing_due_date")
    try:
        adapter.parse_canonical(raw)
        raise AssertionError("Expected MissingSectionError for missing due date")
    except MissingSectionError:
        pass
    print("  ✓ Missing payment due date rejected with MissingSectionError.")


def test_minimum_payment_required_on_model() -> None:
    print("Testing CreditCardSummary rejects a missing minimum payment...")
    try:
        CreditCardSummary(
            statement_id=uuid4(),
            previous_balance_cents=50000,
            payments_credits_cents=0,
            purchases_cents=0,
            cash_advances_cents=0,
            balance_transfers_cents=0,
            fees_charged_cents=0,
            interest_charged_cents=0,
            new_balance_cents=50000,
        )
        raise AssertionError("Expected ValidationError when minimum payment is omitted")
    except ValidationError:
        pass
    print("  ✓ Omitted minimum_payment_due_cents rejected at the model boundary.")


def test_negative_minimum_payment_fails() -> None:
    print("Testing CreditCardAdapter fail-loud on a negative minimum payment...")
    adapter = StandardCreditCardAdapter()
    raw = load_card_fixture("card_happy")
    corrupted_text = raw.pages[0].page_text.replace(
        "Minimum Payment Due: $35.00",
        "Minimum Payment Due: -$10.00",
    )
    raw_corrupted = RawExtraction(
        payload=raw.payload,
        run=raw.run,
        pages=[RawPage(run_id=raw.run.run_id, page_number=1, page_text=corrupted_text)],
    )
    try:
        adapter.parse_canonical(raw_corrupted)
        raise AssertionError("Expected InvariantError for negative minimum payment")
    except InvariantError as exc:
        if "minimum_payment_due_cents must be >= 0" not in str(exc):
            raise
    print("  ✓ Negative minimum payment rejected with InvariantError.")


def main() -> None:
    print("=" * 60)
    print("ASSASSIN Step 4 QA Validation: Credit Card Adapter")
    print("=" * 60)
    test_credit_card_adapter_reconciliation()
    test_credit_card_equation_mismatch_fails()
    test_missing_due_date_fails()
    test_minimum_payment_required_on_model()
    test_negative_minimum_payment_fails()
    print("=" * 60)
    print("All Step 4 Credit Card Adapter verifications PASSED successfully!")
    print("=" * 60)


if __name__ == "__main__":
    main()
