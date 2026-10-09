#!/usr/bin/env python3
"""QA: Vanguard brokerage adapter parses cash, holdings, and a signed bridge.

Run with: uv run python scripts/qa_vanguard_adapter.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.adapters.vanguard import VanguardAdapter
from app.models.enums import RunStatus
from app.models.exceptions import InvariantError, MissingSectionError
from app.models.raw import ExtractionRun, RawExtraction, RawPage, RawPayload


class QAReport:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[str] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed.append(name)
            print(f"  [PASS] {name}" + (f": {detail}" if detail else ""))
        else:
            self.failed.append(name)
            print(f"  [FAIL] {name}" + (f": {detail}" if detail else ""))

    def summary(self) -> bool:
        total = len(self.passed) + len(self.failed)
        print(f"\n{'=' * 60}")
        print(f"QA Summary: {len(self.passed)}/{total} passed")
        if self.failed:
            for name in self.failed:
                print(f"  - {name}")
            return False
        print("All tests passed!")
        return True


def extraction_from(text: str) -> RawExtraction:
    payload = RawPayload(
        content_sha256="b" * 64,
        byte_length=len(text.encode("utf-8")),
        original_basename="synthetic_vanguard.pdf",
    )
    run = ExtractionRun(
        raw_payload_id=payload.raw_payload_id,
        adapter_id="vanguard_brokerage",
        adapter_version="1.0.0",
        status=RunStatus.EXTRACTED,
    )
    page = RawPage(run_id=run.run_id, page_number=1, page_text=text, tokens=[])
    return RawExtraction(payload=payload, run=run, pages=[page])


HAPPY = """VANGUARD BROKERAGE SERVICES
Vanguard Account: *0000
Registration: Brokerage Cash
Period: 2025-08-01 through 2025-08-31

CASH SUMMARY
Opening Cash: $1,000.00
Closing Cash: $1,100.00

ACTIVITY
Date | Activity | Amount
2025-08-15 | SYNTHETIC Dividend | +$100.00

FUND POSITIONS
Symbol | Description | Shares | Value
SYN | SYNTHETIC Equity | 10 | $1,000.00
Positions Total: $1,000.00
"""


def main() -> int:
    report = QAReport()
    adapter = VanguardAdapter()

    print("\n" + "=" * 60)
    print("Feature: Vanguard cash ledger and holdings")
    print("=" * 60)
    account, statement, _summary, txns, holdings = adapter.parse_canonical(
        extraction_from(HAPPY)
    )
    report.record(
        "Cash identity and one dividend",
        statement.opening_balance_cents == 100_000
        and statement.closing_balance_cents == 110_000
        and len(txns) == 1
        and txns[0].amount_cents == 10_000
        and account.account_mask == "*0000",
    )
    report.record(
        "Holdings use integer nanos and cents",
        len(holdings) == 1
        and holdings[0].quantity_nanos == 10_000_000_000
        and holdings[0].market_value_cents == 100_000
        and holdings[0].symbol == "SYN",
    )

    print("\n" + "=" * 60)
    print("Feature: Loud rejection")
    print("=" * 60)
    broken = HAPPY.replace("Closing Cash: $1,100.00", "Closing Cash: $1,100.01")
    try:
        adapter.parse_canonical(extraction_from(broken))
        report.record("One-cent cash mismatch is rejected", False)
    except InvariantError:
        report.record("One-cent cash mismatch is rejected", True)

    flipped = HAPPY.replace("Positions Total: $1,000.00", "Positions Total: $999.99")
    try:
        adapter.parse_canonical(extraction_from(flipped))
        report.record("Holdings total mismatch is rejected", False)
    except InvariantError:
        report.record("Holdings total mismatch is rejected", True)

    schwab = (PROJECT_ROOT / "tests/fixtures/brokerage_cash_happy.json").read_text()
    try:
        adapter.parse_canonical(extraction_from(schwab))
        report.record("Schwab text is not accepted as Vanguard", False)
    except MissingSectionError:
        report.record("Schwab text is not accepted as Vanguard", True)

    print("\n" + "=" * 60)
    return 0 if report.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
