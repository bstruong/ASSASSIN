#!/usr/bin/env python3
"""QA: sidecar magnitudes reject a printed minus; balances stay signed.

Run with: uv run python scripts/qa_sidecar_signs.py

Manual UI QA: not applicable (no user-visible UI).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.adapters.credit_card import StandardCreditCardAdapter
from app.adapters.depository import StandardDepositoryAdapter
from app.adapters.schwab import SchwabAdapter
from app.models.enums import RunStatus
from app.models.exceptions import InvariantError
from app.models.raw import ExtractionRun, RawExtraction, RawPage, RawPayload

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


class QAReport:
    """Collect [PASS]/[FAIL] lines for the operator."""

    def __init__(self) -> None:
        self.failed = 0

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        mark = "PASS" if ok else "FAIL"
        suffix = f": {detail}" if detail else ""
        print(f"[{mark}] {name}{suffix}")
        if not ok:
            self.failed += 1


def extraction_from(text: str, adapter_id: str) -> RawExtraction:
    payload = RawPayload(
        content_sha256="a" * 64,
        byte_length=len(text.encode("utf-8")),
        original_basename="synthetic_statement.pdf",
    )
    run = ExtractionRun(
        raw_payload_id=payload.raw_payload_id,
        adapter_id=adapter_id,
        adapter_version="1.0.0",
        status=RunStatus.EXTRACTED,
    )
    page = RawPage(run_id=run.run_id, page_number=1, page_text=text, tokens=[])
    return RawExtraction(payload=payload, run=run, pages=[page])


def fixture_text(name: str) -> str:
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    text = data["raw_text"]
    if not isinstance(text, str):
        raise TypeError(f"Fixture {name} raw_text must be a string")
    return text


def expect_invariant(report: QAReport, name: str, action) -> None:
    try:
        action()
    except InvariantError:
        report.record(name, True, "InvariantError")
    except Exception as exc:  # noqa: BLE001 — QA must show the unexpected type
        report.record(name, False, type(exc).__name__)
    else:
        report.record(name, False, "accepted a printed negative magnitude")


def main() -> None:
    print("Feature: sidecar magnitudes stay non-negative without abs().")
    print("Balances and realized/unrealized gains stay signed integer cents.")
    print("Manual UI QA: not applicable (no user-visible UI).")
    print()
    report = QAReport()

    depository = StandardDepositoryAdapter()
    checking = extraction_from(fixture_text("checking_happy"), "standard_depository")
    _opening, _closing, dep_summary = depository.parse_summary(checking)
    report.record(
        "depository happy path",
        dep_summary.deposits_cents > 0 and dep_summary.fees_cents >= 0,
        "positive deposit and fee buckets accepted",
    )
    signed_opening = extraction_from(
        fixture_text("checking_happy").replace(
            "Starting Balance: $1,000.00",
            "Starting Balance: $-10.00",
        ),
        "standard_depository",
    )
    opening_cents, _, _summary = depository.parse_summary(signed_opening)
    report.record(
        "depository signed opening balance",
        opening_cents < 0,
        "printed minus kept on the balance",
    )
    expect_invariant(
        report,
        "depository negative deposits rejected",
        lambda: depository.parse_summary(
            extraction_from(
                fixture_text("checking_happy").replace(
                    "Deposits and Additions: $500.00",
                    "Deposits and Additions: ($500.00)",
                ),
                "standard_depository",
            )
        ),
    )

    card = StandardCreditCardAdapter()
    card_happy = extraction_from(fixture_text("card_happy"), "standard_credit_card")
    _prev, new_balance, card_summary = card.parse_summary(card_happy)
    report.record(
        "credit card happy path",
        card_summary.purchases_cents > 0 and new_balance > 0,
        "purchase bucket and new balance accepted",
    )
    expect_invariant(
        report,
        "credit card negative purchases rejected",
        lambda: card.parse_summary(
            extraction_from(
                fixture_text("card_happy").replace(
                    "Purchases: $250.00",
                    "Purchases: -$250.00",
                ),
                "standard_credit_card",
            )
        ),
    )

    schwab = SchwabAdapter()
    portfolio = extraction_from(
        fixture_text("brokerage_portfolio_happy"), "schwab_brokerage"
    )
    _cash_open, _cash_close, brokerage = schwab.parse_summary(portfolio)
    report.record(
        "schwab portfolio happy path",
        brokerage.transfers_in_cents is not None
        and brokerage.transfers_in_cents > 0
        and brokerage.unrealized_gains_cents is not None
        and brokerage.unrealized_gains_cents < 0,
        "transfers in accepted and unrealized loss stays signed",
    )
    expect_invariant(
        report,
        "schwab negative transfer in rejected",
        lambda: schwab.parse_summary(
            extraction_from(
                fixture_text("brokerage_portfolio_happy").replace(
                    "Transfers In: $4,000.00",
                    "Transfers In: ($4,000.00)",
                ),
                "schwab_brokerage",
            )
        ),
    )

    print()
    if report.failed:
        print(f"[FAIL] {report.failed} check(s) failed")
        sys.exit(1)
    print("[PASS] sidecar sign checks")
    sys.exit(0)


if __name__ == "__main__":
    main()
