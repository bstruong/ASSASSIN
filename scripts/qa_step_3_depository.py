#!/usr/bin/env python3
"""QA verification script for Step 3: Depository Statement Adapter.

Validates checking/savings extraction, depository bucket reconciliation,
running-balance continuity, and fail-loud multi-account checks.
"""

import json
from pathlib import Path

from app.adapters.depository import StandardDepositoryAdapter
from app.models.enums import AccountDomain, AccountType, RunStatus
from app.models.exceptions import AmbiguousAccountsError, InvariantError
from app.models.raw import ExtractionRun, RawExtraction, RawPage, RawPayload

FIXTURES_DIR = Path(__file__).parents[1] / "tests" / "fixtures"


def load_fixture(name: str) -> RawExtraction:
    path = FIXTURES_DIR / f"{name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    text = data["raw_text"]
    payload = RawPayload(
        content_sha256="1" * 64,
        byte_length=len(text.encode()),
        original_basename=f"{name}.pdf",
    )
    run = ExtractionRun(
        raw_payload_id=payload.raw_payload_id,
        adapter_id="standard_depository",
        adapter_version="1.0.0",
        status=RunStatus.EXTRACTED,
    )
    return RawExtraction(
        payload=payload,
        run=run,
        pages=[RawPage(run_id=run.run_id, page_number=1, page_text=text)],
    )


def test_depository_adapter_reconciliation() -> None:
    print("Testing StandardDepositoryAdapter balance reconciliation on fixture...")
    adapter = StandardDepositoryAdapter()
    raw = load_fixture("checking_happy")

    account, statement, summary, txns = adapter.parse_canonical(raw)
    assert account.account_domain == AccountDomain.DEPOSITORY
    assert account.account_type == AccountType.CHECKING
    assert statement.opening_balance_cents == 100000
    assert statement.closing_balance_cents == 129500
    assert statement.net_change_cents == 29500
    assert summary.deposits_cents == 50000
    assert summary.withdrawals_cents == 20000
    assert summary.fees_cents == 1000
    assert summary.interest_paid_cents == 500
    assert len(txns) == 4
    print("  ✓ Depository checking statement parsed and reconciled perfectly.")


def test_balance_mismatch_fails() -> None:
    print("Testing DepositoryAdapter fail-loud on 1-cent discrepancy...")
    adapter = StandardDepositoryAdapter()
    raw = load_fixture("checking_happy")
    # Mutate ending balance by 1 cent
    corrupted_text = raw.pages[0].page_text.replace(
        "Ending Balance: $1,295.00", "Ending Balance: $1,295.01"
    )
    raw_corrupted = RawExtraction(
        payload=raw.payload,
        run=raw.run,
        pages=[RawPage(run_id=raw.run.run_id, page_number=1, page_text=corrupted_text)],
    )

    try:
        adapter.parse_canonical(raw_corrupted)
        raise AssertionError("Expected InvariantError for 1-cent mismatch")
    except InvariantError:
        pass
    print("  ✓ 1-cent balance mismatch rejected with InvariantError.")


def test_ambiguous_accounts_fails() -> None:
    print("Testing DepositoryAdapter fail-loud on multiple accounts detected...")
    adapter = StandardDepositoryAdapter()
    raw = load_fixture("checking_plus_savings_page1")
    try:
        adapter.parse_canonical(raw)
        raise AssertionError(
            "Expected AmbiguousAccountsError for multi-account statement"
        )
    except AmbiguousAccountsError:
        pass
    print("  ✓ Multi-account statement rejected with AmbiguousAccountsError.")


def main() -> None:
    print("=" * 60)
    print("ASSASSIN Step 3 QA Validation: Depository Adapter")
    print("=" * 60)
    test_depository_adapter_reconciliation()
    test_balance_mismatch_fails()
    test_ambiguous_accounts_fails()
    print("=" * 60)
    print("All Step 3 Depository Adapter verifications PASSED successfully!")
    print("=" * 60)


if __name__ == "__main__":
    main()
