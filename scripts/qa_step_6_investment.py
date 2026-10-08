#!/usr/bin/env python3
"""QA verification script for Step 6: Investment Statement Adapter.

Validates:
1. Universal cash identity: opening_cash + sum(cash deltas) == closing_cash.
2. Optional portfolio bridge: opening_portfolio + transfers_in - transfers_out +
   income_dividends + realized_gains + unrealized_gains == closing_portfolio.
3. Fail-loud invariant rejection on 1-cent cash balance mismatch.
4. Fail-loud invariant rejection on 1-cent portfolio bridge mismatch.
5. Fail-loud contract rejection on missing portfolio bridge printed components.
6. Fail-loud rejection on ambiguous multiple accounts detected on statement.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.adapters.schwab import SchwabAdapter
from app.models.enums import AccountDomain, AccountType, RunStatus
from app.models.exceptions import (
    AmbiguousAccountsError,
    InvariantError,
    MissingSectionError,
)
from app.models.raw import ExtractionRun, RawExtraction, RawPage, RawPayload

FIXTURES_DIR = Path(__file__).parents[1] / "tests" / "fixtures"


def load_fixture(name: str) -> RawExtraction:
    path = FIXTURES_DIR / f"{name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    text = data["raw_text"]
    payload = RawPayload(
        content_sha256="6" * 64,
        byte_length=len(text.encode()),
        original_basename=f"{name}.pdf",
    )
    run = ExtractionRun(
        raw_payload_id=payload.raw_payload_id,
        adapter_id="schwab_brokerage",
        adapter_version="1.0.0",
        status=RunStatus.EXTRACTED,
    )
    return RawExtraction(
        payload=payload,
        run=run,
        pages=[RawPage(run_id=run.run_id, page_number=1, page_text=text)],
    )


def test_brokerage_cash_reconciliation() -> None:
    print("Testing SchwabAdapter cash identity on fixture...")
    adapter = SchwabAdapter()
    raw = load_fixture("brokerage_cash_happy")

    account, statement, summary, txns, _holdings = adapter.parse_canonical(raw)
    assert account.account_domain == AccountDomain.CUSTODIAL_BROKERAGE
    assert account.account_type == AccountType.BROKERAGE_CASH
    assert account.account_mask == "****7842"
    assert statement.opening_balance_cents == 1_000_000
    assert statement.closing_balance_cents == 1_390_425
    assert statement.net_change_cents == 390_425
    assert summary.opening_cash_cents == 1_000_000
    assert summary.closing_cash_cents == 1_390_425
    assert summary.opening_portfolio_cents is None
    assert len(txns) == 5
    print("  ✓ Brokerage cash statement parsed and universal cash identity verified.")


def test_brokerage_portfolio_bridge() -> None:
    print("Testing SchwabAdapter portfolio bridge on fixture...")
    adapter = SchwabAdapter()
    raw = load_fixture("brokerage_portfolio_happy")

    account, statement, summary, _, _holdings = adapter.parse_canonical(raw)
    assert account.account_domain == AccountDomain.CUSTODIAL_BROKERAGE
    assert statement.opening_balance_cents == 1_000_000
    assert statement.closing_balance_cents == 1_390_425

    # Check portfolio bridge components
    assert summary.opening_portfolio_cents == 5_000_000
    assert summary.transfers_in_cents == 400_000
    assert summary.transfers_out_cents == 10_000
    assert summary.income_dividends_cents == 10_000
    assert summary.realized_gains_cents == 50_000
    assert summary.unrealized_gains_cents == -25_000
    assert summary.closing_portfolio_cents == 5_425_000
    print("  ✓ Brokerage portfolio bridge reconciled perfectly ($54,250.00).")


def test_cash_balance_mismatch_fails() -> None:
    print("Testing SchwabAdapter fail-loud on 1-cent cash discrepancy...")
    adapter = SchwabAdapter()
    raw = load_fixture("brokerage_cash_happy")
    corrupted_text = raw.pages[0].page_text.replace(
        "Ending Cash Balance: $13,904.25", "Ending Cash Balance: $13,904.26"
    )
    raw_corrupted = RawExtraction(
        payload=raw.payload,
        run=raw.run,
        pages=[RawPage(run_id=raw.run.run_id, page_number=1, page_text=corrupted_text)],
    )

    try:
        adapter.parse_canonical(raw_corrupted)
        raise AssertionError("Expected InvariantError for 1-cent cash mismatch")
    except InvariantError:
        pass
    print("  ✓ 1-cent cash mismatch rejected with InvariantError.")


def test_portfolio_bridge_mismatch_fails() -> None:
    print("Testing SchwabAdapter fail-loud on 1-cent portfolio bridge discrepancy...")
    adapter = SchwabAdapter()
    raw = load_fixture("brokerage_portfolio_happy")
    corrupted_text = raw.pages[0].page_text.replace(
        "Ending Portfolio Value: $54,250.00", "Ending Portfolio Value: $54,250.01"
    )
    raw_corrupted = RawExtraction(
        payload=raw.payload,
        run=raw.run,
        pages=[RawPage(run_id=raw.run.run_id, page_number=1, page_text=corrupted_text)],
    )

    try:
        adapter.parse_canonical(raw_corrupted)
        raise AssertionError(
            "Expected InvariantError for 1-cent portfolio bridge mismatch"
        )
    except InvariantError:
        pass
    print("  ✓ 1-cent portfolio bridge mismatch rejected with InvariantError.")


def test_incomplete_portfolio_bridge_fails() -> None:
    print("Testing SchwabAdapter fail-loud on missing portfolio bridge component...")
    adapter = SchwabAdapter()
    raw = load_fixture("brokerage_portfolio_happy")
    corrupted_text = raw.pages[0].page_text.replace("Realized Gain/Loss: $500.00\n", "")
    raw_corrupted = RawExtraction(
        payload=raw.payload,
        run=raw.run,
        pages=[RawPage(run_id=raw.run.run_id, page_number=1, page_text=corrupted_text)],
    )

    try:
        adapter.parse_canonical(raw_corrupted)
        raise AssertionError(
            "Expected MissingSectionError for missing portfolio bridge term"
        )
    except MissingSectionError:
        pass
    print("  ✓ Missing portfolio bridge term rejected with MissingSectionError.")


def test_ambiguous_accounts_fails() -> None:
    print("Testing SchwabAdapter fail-loud on multiple accounts detected...")
    adapter = SchwabAdapter()
    raw = load_fixture("brokerage_cash_happy")
    corrupted_text = raw.pages[0].page_text.replace(
        "Account Number: ****7842",
        "Account Number: ****7842\nSecondary Account: ****9999",
    )
    raw_corrupted = RawExtraction(
        payload=raw.payload,
        run=raw.run,
        pages=[RawPage(run_id=raw.run.run_id, page_number=1, page_text=corrupted_text)],
    )

    try:
        adapter.parse_canonical(raw_corrupted)
        raise AssertionError(
            "Expected AmbiguousAccountsError for multi-account statement"
        )
    except AmbiguousAccountsError:
        pass
    print("  ✓ Multi-account statement rejected with AmbiguousAccountsError.")


def main() -> None:
    print("=" * 60)
    print("ASSASSIN Step 6 QA Validation: Investment Statement Adapter")
    print("=" * 60)
    test_brokerage_cash_reconciliation()
    test_brokerage_portfolio_bridge()
    test_cash_balance_mismatch_fails()
    test_portfolio_bridge_mismatch_fails()
    test_incomplete_portfolio_bridge_fails()
    test_ambiguous_accounts_fails()
    print("=" * 60)
    print("All Step 6 Investment Adapter verifications PASSED successfully!")
    print("=" * 60)


if __name__ == "__main__":
    main()
