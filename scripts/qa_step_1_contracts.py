#!/usr/bin/env python3
"""QA verification script for Step 1: Contracts Freeze.

Validates that raw models, canonical models, and closed domain taxonomies
enforce all invariants and fail loudly on invalid payloads.
"""

import datetime
from uuid import uuid4

from pydantic import ValidationError

from app.models.canonical import (
    Account,
    CanonicalStatement,
)
from app.models.enums import (
    AccountDomain,
    AccountType,
    CurrencyCode,
    RunStatus,
)
from app.models.raw import ExtractionRun, RawToken


def test_token_geometry() -> None:
    print("Testing RawToken integer millipoint bounding box rules...")
    # Valid token
    t = RawToken(
        raw_page_id=uuid4(),
        token_kind="word",
        token_text="Deposit",
        x0_mp=1000,
        y0_mp=2000,
        x1_mp=5000,
        y1_mp=4000,
    )
    assert t.token_text == "Deposit"

    # Inverted coordinates must fail loudly
    try:
        RawToken(
            raw_page_id=uuid4(),
            token_kind="word",
            token_text="Invalid",
            x0_mp=5000,
            y0_mp=2000,
            x1_mp=1000,
            y1_mp=4000,
        )
        raise AssertionError("Expected ValidationError for x1 <= x0")
    except ValidationError:
        pass
    print("  ✓ RawToken geometry strictly validated.")


def test_extraction_run_status() -> None:
    print("Testing ExtractionRun status & error correlation...")
    run_ok = ExtractionRun(
        raw_payload_id=uuid4(),
        adapter_id="qa_adapter",
        adapter_version="1.0.0",
        status=RunStatus.RAW_STORED,
    )
    assert run_ok.status == RunStatus.RAW_STORED

    # FAILED status without error code must fail
    try:
        ExtractionRun(
            raw_payload_id=uuid4(),
            adapter_id="qa_adapter",
            adapter_version="1.0.0",
            status=RunStatus.FAILED,
            error_code=None,
        )
        raise AssertionError("Expected ValidationError for FAILED without error_code")
    except ValidationError:
        pass
    print("  ✓ ExtractionRun status invariant strictly enforced.")


def test_account_taxonomy() -> None:
    print("Testing Account taxonomy domain pairing...")
    acct = Account(
        institution="JPMorgan Chase",
        account_mask="*1234",
        account_domain=AccountDomain.DEPOSITORY,
        account_type=AccountType.CHECKING,
    )
    assert acct.currency == CurrencyCode.USD

    # Mismatched domain & account type must fail
    try:
        Account(
            institution="Chase",
            account_mask="*1234",
            account_domain=AccountDomain.DEPOSITORY,
            account_type=AccountType.CREDIT_CARD,
        )
        raise AssertionError("Expected ValidationError for mismatched domain and type")
    except ValidationError:
        pass
    print("  ✓ Account taxonomy pairings strictly enforced.")


def test_canonical_statement_invariants() -> None:
    print("Testing CanonicalStatement net change invariant...")
    stmt = CanonicalStatement(
        account_id=uuid4(),
        run_id=uuid4(),
        raw_payload_id=uuid4(),
        statement_start_date=datetime.date(2025, 1, 1),
        statement_end_date=datetime.date(2025, 1, 31),
        opening_balance_cents=100000,
        closing_balance_cents=125000,
        net_change_cents=25000,
    )
    assert stmt.net_change_cents == 25000

    # Net change mismatch must fail
    try:
        CanonicalStatement(
            account_id=uuid4(),
            run_id=uuid4(),
            raw_payload_id=uuid4(),
            statement_start_date=datetime.date(2025, 1, 1),
            statement_end_date=datetime.date(2025, 1, 31),
            opening_balance_cents=100000,
            closing_balance_cents=125000,
            net_change_cents=99999,
        )
        raise AssertionError("Expected ValidationError for net_change_cents mismatch")
    except ValidationError:
        pass
    print("  ✓ CanonicalStatement balance invariant strictly enforced.")


def main() -> None:
    print("=" * 60)
    print("ASSASSIN Step 1 QA Validation: Contracts Freeze")
    print("=" * 60)
    test_token_geometry()
    test_extraction_run_status()
    test_account_taxonomy()
    test_canonical_statement_invariants()
    print("=" * 60)
    print("All Step 1 Contract verifications PASSED successfully!")
    print("=" * 60)


if __name__ == "__main__":
    main()
