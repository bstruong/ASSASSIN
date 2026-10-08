#!/usr/bin/env python3
"""QA Verification Script: Integrity Debt Remediation.

Verifies fail-loud parsing, signed-delta contracts, cloud aggregate-only SQL,
and frontier Tier-1 quarantine — without requiring the operator to read diffs.

Run with: uv run python scripts/qa_integrity_debt.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.adapters.combined import StandardCombinedDepositoryAdapter
from app.adapters.credit_card import StandardCreditCardAdapter
from app.adapters.depository import StandardDepositoryAdapter
from app.api.app import create_app
from app.mcp.cloud_tools import CloudToolError, _validate_cloud_sql
from app.models.enums import (
    AccountDomain,
    AccountType,
    RunStatus,
    TransactionCategory,
    validate_category_for_domain,
)
from app.models.exceptions import TokenError
from app.models.raw import ExtractionRun, RawExtraction, RawPage, RawPayload

FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


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
            print(f"Failed ({len(self.failed)}):")
            for name in self.failed:
                print(f"  - {name}")
            return False
        print("All integrity negatives verified.")
        return True


def load_fixture(name: str) -> RawExtraction:
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    text = data["raw_text"]
    payload = RawPayload(
        content_sha256="a" * 64,
        byte_length=len(text.encode()),
        original_basename=f"{name}.pdf",
    )
    run = ExtractionRun(
        raw_payload_id=payload.raw_payload_id,
        adapter_id="qa",
        adapter_version="1.0.0",
        status=RunStatus.EXTRACTED,
    )
    return RawExtraction(
        payload=payload,
        run=run,
        pages=[RawPage(run_id=run.run_id, page_number=1, page_text=text)],
    )


def main() -> int:
    report = QAReport()
    print("=" * 60)
    print("Integrity Debt Remediation QA")
    print("=" * 60)

    # 1. Combined malformed row → loud fail
    print("\n-- Combined malformed row --")
    combined = StandardCombinedDepositoryAdapter()
    try:
        combined._parse_account_transactions(
            "CHECKING TRANSACTION DETAILS\n"
            "Date    Description    Amount    Balance\n"
            "Invalid Row\n"
            "END\n",
            start_marker="CHECKING TRANSACTION DETAILS",
            end_marker="END",
            statement_id=uuid4(),
            account_type=AccountType.CHECKING,
        )
        report.record("Combined malformed row fails loud", False, "no exception")
    except TokenError as exc:
        report.record("Combined malformed row fails loud", True, str(exc)[:80])

    # 2. Unsigned ambiguous amount → loud fail (no keyword)
    print("\n-- Unsigned ambiguous amount --")
    depository = StandardDepositoryAdapter()
    try:
        depository.parse_canonical(load_fixture("ambiguous_sign"))
        report.record("Unsigned ambiguous amount fails loud", False, "no exception")
    except TokenError as exc:
        report.record(
            "Unsigned ambiguous amount fails loud",
            "Ambiguous amount sign" in str(exc),
            str(exc)[:80],
        )

    # 3. Wrong-domain category → loud fail
    print("\n-- Wrong-domain category --")
    try:
        validate_category_for_domain(
            AccountDomain.REVOLVING_CREDIT, TransactionCategory.DEPOSIT
        )
        report.record("Wrong-domain category fails loud", False, "no exception")
    except ValueError as exc:
        report.record("Wrong-domain category fails loud", True, str(exc)[:80])

    # 4. Card glyph/category disagreement → loud fail
    print("\n-- Card glyph/category disagreement --")
    card = StandardCreditCardAdapter()
    extraction = load_fixture("card_happy")
    bad_text = extraction.pages[0].page_text.replace(
        "Payment Received - Thank You    -$500.00",
        "Payment Received - Thank You    +$500.00",
    )
    extraction.pages[0] = RawPage(
        run_id=extraction.run.run_id,
        page_number=1,
        page_text=bad_text,
    )
    try:
        card.parse_canonical(extraction)
        report.record("Card glyph/category disagreement fails loud", False)
    except TokenError as exc:
        report.record(
            "Card glyph/category disagreement fails loud",
            "disagrees" in str(exc),
            str(exc)[:80],
        )

    # 5. Cloud SELECT * / raw JOIN → loud fail
    print("\n-- Cloud SQL aggregate-only policy --")
    try:
        _validate_cloud_sql("SELECT * FROM transactions;")
        report.record("Cloud SELECT * fails loud", False)
    except CloudToolError as exc:
        report.record("Cloud SELECT * fails loud", True, str(exc)[:80])

    try:
        _validate_cloud_sql(
            "SELECT COUNT(*) FROM transactions t JOIN raw_pages r ON true;"
        )
        report.record("Cloud raw_* JOIN fails loud", False)
    except CloudToolError as exc:
        report.record("Cloud raw_* JOIN fails loud", True, str(exc)[:80])

    try:
        _validate_cloud_sql("SELECT account_id FROM accounts;")
        report.record("Cloud non-aggregate SELECT fails loud", False)
    except CloudToolError as exc:
        report.record("Cloud non-aggregate SELECT fails loud", True, str(exc)[:80])

    # 6. Frontier quarantine + aggregate-only handoff
    print("\n-- Frontier quarantine + aggregate handoff --")
    app = create_app()
    client = TestClient(app)
    resp = client.post("/v1/orchestrator/execute_sql", json={"sql": "SELECT 1;"})
    detail = resp.json().get("detail", {})
    report.record(
        "Tier-1 execute_sql remains quarantined (no row sets)",
        resp.status_code == 501
        and detail.get("error_code") == "FRONTIER_QUARANTINED"
        and "results" not in detail,
        f"status={resp.status_code} code={detail.get('error_code')}",
    )

    prod_paths = set(create_app().openapi()["paths"])
    report.record(
        "Production app mounts aggregate-only /chat/frontier/audit",
        "/chat/frontier/audit" in prod_paths,
    )
    report.record(
        "Production app mounts quarantined /v1/orchestrator/execute_sql",
        "/v1/orchestrator/execute_sql" in prod_paths,
    )

    resp = client.post(
        "/chat/frontier/audit",
        json={"aggregate_sql": "SELECT description FROM transactions;"},
    )
    detail = resp.json().get("detail", {})
    report.record(
        "Frontier audit rejects row-level description SQL",
        resp.status_code == 400
        and detail.get("error_code") == "CLOUD_TOOL_VALIDATION_ERROR",
        f"status={resp.status_code} code={detail.get('error_code')}",
    )

    return 0 if report.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
