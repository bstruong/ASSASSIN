#!/usr/bin/env python3
"""QA: brokerage holdings parse with integer nanos and a printed total.

Run with: uv run python scripts/qa_holdings.py

Happy path uses obviously fake symbols and SYNTHETIC descriptions.
Negative cases fail loudly: missing total, 1-cent valuation mismatch,
and a share quantity with more than 9 decimal places.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.adapters.schwab import SchwabAdapter
from app.models.enums import RunStatus
from app.models.exceptions import InvariantError, MissingSectionError, TokenError
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


def _extraction(text: str) -> RawExtraction:
    payload = RawPayload(
        content_sha256="a" * 64,
        byte_length=len(text.encode()),
        original_basename="synthetic_holdings.pdf",
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


def _statement(positions: str) -> str:
    return (
        "CHARLES SCHWAB\n"
        "Individual Brokerage Account\n"
        "Account Number: *0000\n"
        "Statement Period: 2025-08-01 to 2025-08-31\n"
        "\n"
        "ACCOUNT SUMMARY\n"
        "Starting Cash Balance: $10,000.00\n"
        "Ending Cash Balance: $13,904.25\n"
        "\n"
        "TRANSACTION ACTIVITY\n"
        "Date    Description    Amount\n"
        "2025-08-05    Electronic Deposit Funds Received    +$2,000.00\n"
        "2025-08-10    Wire Transfer In    +$2,000.00\n"
        "2025-08-15    Funds Withdrawal Transfer Out    -$100.00\n"
        "2025-08-20    Qualifying Dividend Payment    +$100.00\n"
        "2025-08-25    Account Service Fee Charged    -$95.75\n"
        f"{positions}"
        "END OF STATEMENT"
    )


_HAPPY = (
    "POSITIONS\n"
    "Symbol    Description    Quantity    Market Value\n"
    "SYN    SYNTHETIC Equity    10    $1,000.00\n"
    "BND    SYNTHETIC Bond    2.5    $250.50\n"
    "Holdings Total: $1,250.50\n"
)


def main() -> int:
    report = QAReport()
    adapter = SchwabAdapter()

    print("\n" + "=" * 60)
    print("Feature: Holdings happy path (integer nanos, integer cents)")
    print("=" * 60)
    try:
        _account, _stmt, _summary, txns, holdings = adapter.parse_canonical(
            _extraction(_statement(_HAPPY))
        )
        ok = (
            len(txns) == 5
            and len(holdings) == 2
            and holdings[0].quantity_nanos == 10_000_000_000
            and holdings[0].market_value_cents == 100_000
            and holdings[1].quantity_nanos == 2_500_000_000
            and holdings[1].market_value_cents == 25_050
            and holdings[0].description == "SYNTHETIC Equity"
        )
        report.record(
            "Positions sum to the printed Holdings Total",
            ok,
            f"rows={len(holdings)}",
        )
    except Exception as exc:  # noqa: BLE001
        report.record("Positions sum to the printed Holdings Total", False, str(exc))

    print("\n" + "=" * 60)
    print("Feature: Loud failures")
    print("=" * 60)

    try:
        adapter.parse_canonical(
            _extraction(
                _statement(
                    _HAPPY.replace(
                        "Holdings Total: $1,250.50", "Holdings Total: $1,250.51"
                    )
                )
            )
        )
        report.record("1-cent holdings total mismatch fails loud", False)
    except InvariantError as exc:
        report.record(
            "1-cent holdings total mismatch fails loud",
            "Holdings valuation mismatch" in str(exc),
            str(exc)[:80],
        )

    try:
        adapter.parse_canonical(
            _extraction(_statement(_HAPPY.replace("Holdings Total: $1,250.50\n", "")))
        )
        report.record("Missing Holdings Total fails loud", False)
    except MissingSectionError as exc:
        report.record(
            "Missing Holdings Total fails loud",
            "Holdings Total is required" in str(exc),
        )

    try:
        adapter.parse_canonical(
            _extraction(
                _statement(
                    _HAPPY.replace("10    $1,000.00", "10.1234567891    $1,000.00")
                )
            )
        )
        report.record("Over-precise share quantity fails loud", False)
    except TokenError as exc:
        report.record(
            "Over-precise share quantity fails loud",
            "more than 9 decimal places" in str(exc),
        )

    print("\n" + "=" * 60)
    return 0 if report.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
