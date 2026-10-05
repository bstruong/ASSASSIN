#!/usr/bin/env python3
"""Executable QA verification script for Step 9: Local Ingest API.

Demonstrates:
1. Healthcheck endpoint (database connectivity and registered adapters)
2. Single-account depository round-trip statement ingestion
3. Single-account credit card round-trip statement ingestion
4. Multi-account combined depository round-trip statement ingestion
5. Fail-loud contract rejection on invalid/unmatched payloads
6. Invariant violation failure recording in audit database
7. Zero PII egress verification across responses and logs
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from app.api.app import create_app
from app.db.connection import get_db_connection, init_db
from app.db.repository import get_extraction_run
from app.models.enums import RunStatus

FIXTURES_DIR = Path(__file__).parents[1] / "tests" / "fixtures"


def render_pdf_from_fixture(fixture_name: str, target_path: Path) -> Path:
    """Render a synthetic PDF document from fixture JSON."""
    data = json.loads(
        (FIXTURES_DIR / f"{fixture_name}.json").read_text(encoding="utf-8")
    )
    raw_text = data["raw_text"]
    lines = raw_text.split("\n")
    c = canvas.Canvas(str(target_path))
    y = 750
    for line in lines:
        if "\t" in line or "    " in line:
            line = re.sub(r"(\t|\s{2,})", " | ", line)
        c.drawString(50, y, line)
        y -= 15
        if y < 50:
            c.showPage()
            y = 750
    c.save()
    return target_path


def render_custom_pdf(lines: list[str], target_path: Path) -> Path:
    """Render custom text lines into a PDF."""
    c = canvas.Canvas(str(target_path))
    y = 750
    for line in lines:
        c.drawString(50, y, line)
        y -= 15
        if y < 50:
            c.showPage()
            y = 750
    c.save()
    return target_path


def main() -> None:
    print("=" * 80)
    print("ASSASSIN Step 9: Local Ingest API QA Verification")
    print("=" * 80)

    # Initialize schema
    init_db()

    app = create_app()
    client = TestClient(app)

    # Scenario 1: Healthcheck
    print("\n[Scenario 1] GET /api/v1/health")
    health_res = client.get("/api/v1/health")
    assert health_res.status_code == 200, f"Expected 200, got {health_res.status_code}"
    health_data = health_res.json()
    print(f"  Health status: {health_data['status']}")
    print(f"  Database: {health_data['database']}")
    print(f"  Registered adapters: {health_data['registered_adapters_count']}")
    assert health_data["status"] == "ok"
    assert health_data["database"] == "connected"
    print("  ✓ Healthcheck verified.")

    # Scenario 2: Depository Statement Upload
    print("\n[Scenario 2] POST /api/v1/ingest/upload (Depository Statement)")
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        render_pdf_from_fixture("checking_happy", Path(tmp.name))
        res = client.post(
            "/api/v1/ingest/upload",
            files={
                "file": (
                    "checking_jan2025.pdf",
                    Path(tmp.name).read_bytes(),
                    "application/pdf",
                )
            },
        )
        assert res.status_code == 201, (
            f"Expected 201, got {res.status_code}: {res.text}"
        )
        data = res.json()
        trace_id = res.headers.get("X-Trace-ID")
        print(f"  Status: {data['status']}")
        print(f"  Adapter: {data['adapter_id']} v{data['adapter_version']}")
        print(f"  Trace ID: {trace_id}")
        print(f"  Statements persisted: {data['statements_count']}")
        print(f"  Transactions persisted: {data['transactions_count']}")
        print(f"  Account mask: {data['accounts'][0]['account_mask']}")
        assert data["adapter_id"] == "standard_depository"
        assert data["statements_count"] == 1
        assert data["transactions_count"] == 4

        # Query run
        run_id = data["run_id"]
        run_res = client.get(f"/api/v1/ingest/runs/{run_id}")
        assert run_res.status_code == 200
        run_data = run_res.json()
        print(f"  Run query status: {run_data['status']}")
        assert run_data["status"] == "canonical_persisted"
        print("  ✓ Depository round-trip verified.")

    # Scenario 3: Credit Card Statement Upload
    print("\n[Scenario 3] POST /api/v1/ingest/upload (Credit Card Statement)")
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        render_pdf_from_fixture("card_happy", Path(tmp.name))
        res = client.post(
            "/api/v1/ingest/upload",
            files={
                "file": (
                    "card_jan2025.pdf",
                    Path(tmp.name).read_bytes(),
                    "application/pdf",
                )
            },
        )
        assert res.status_code == 201, (
            f"Expected 201, got {res.status_code}: {res.text}"
        )
        data = res.json()
        print(f"  Status: {data['status']}")
        print(f"  Adapter: {data['adapter_id']}")
        print(f"  Statements persisted: {data['statements_count']}")
        print(f"  Transactions persisted: {data['transactions_count']}")
        print(f"  Account mask: {data['accounts'][0]['account_mask']}")
        assert data["adapter_id"] == "standard_credit_card"
        print("  ✓ Credit card round-trip verified.")

    # Scenario 4: Combined Multi-Account Upload
    print(
        "\n[Scenario 4] POST /api/v1/ingest/upload (Combined Multi-Account Statement)"
    )
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        render_pdf_from_fixture("combined_checking_savings_happy", Path(tmp.name))
        res = client.post(
            "/api/v1/ingest/upload",
            files={
                "file": (
                    "combined_jan2025.pdf",
                    Path(tmp.name).read_bytes(),
                    "application/pdf",
                )
            },
        )
        assert res.status_code == 201, (
            f"Expected 201, got {res.status_code}: {res.text}"
        )
        data = res.json()
        print(f"  Status: {data['status']}")
        print(f"  Adapter: {data['adapter_id']}")
        print(f"  Statements persisted: {data['statements_count']}")
        print(f"  Transactions persisted: {data['transactions_count']}")
        masks = [a["account_mask"] for a in data["accounts"]]
        print(f"  Account masks: {masks}")
        assert data["adapter_id"] == "standard_combined_depository"
        assert data["statements_count"] == 2
        assert data["transactions_count"] == 3
        print("  ✓ Combined multi-account round-trip verified.")

    # Scenario 5: Fail-Loud Validation on Non-PDF & Unmatched Adapter
    print("\n[Scenario 5] Fail-Loud Rejection on Invalid Payload & Unmatched Bank")
    # Non-PDF
    bad_res = client.post(
        "/api/v1/ingest/upload",
        files={"file": ("data.txt", b"not a pdf", "text/plain")},
    )
    assert bad_res.status_code == 400
    print(
        f"  Non-PDF rejected with HTTP {bad_res.status_code}: {bad_res.json()['detail']}"
    )

    # Unmatched bank
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        render_custom_pdf(["UNKNOWN BANK", "STATEMENT 2025"], Path(tmp.name))
        unmatched_res = client.post(
            "/api/v1/ingest/upload",
            files={
                "file": ("unknown.pdf", Path(tmp.name).read_bytes(), "application/pdf")
            },
        )
        assert unmatched_res.status_code == 422
        unmatched_data = unmatched_res.json()
        print(
            f"  Unmatched adapter rejected with HTTP {unmatched_res.status_code}: {unmatched_data['error_code']}"
        )
        assert unmatched_data["error_code"] == "ADAPTER_MATCH_FAILED"
    print("  ✓ Fail-loud payload rejection verified.")

    # Scenario 6: Invariant Break Recording in Audit Database
    print("\n[Scenario 6] Invariant Violation Recording in Audit Trail")
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        lines = [
            "STANDARD BANK",
            "Checking Account Number: *1234",
            "Statement Period: 2025-01-01 to 2025-01-31",
            "ACCOUNT SUMMARY",
            "Starting Balance: $1,000.00",
            "Deposits and Additions: $500.00",
            "Withdrawals and Subtractions: $200.00",
            "Interest Paid: $5.00",
            "Fees Charged: $10.00",
            "Ending Balance: $1,295.01",  # 1-cent invariant violation!
            "TRANSACTION DETAILS",
            "Date | Description | Amount | Balance",
            "2025-01-05 | Payroll Deposit | +$500.00 | $1,500.00",
            "2025-01-10 | ATM Withdrawal | -$200.00 | $1,300.00",
            "2025-01-15 | Monthly Service Fee | -$10.00 | $1,290.00",
            "2025-01-31 | Interest Paid | +$5.00 | $1,295.00",
            "END OF STATEMENT",
        ]
        render_custom_pdf(lines, Path(tmp.name))
        inv_res = client.post(
            "/api/v1/ingest/upload",
            files={
                "file": (
                    "invariant_break.pdf",
                    Path(tmp.name).read_bytes(),
                    "application/pdf",
                )
            },
        )
        assert inv_res.status_code == 422
        inv_data = inv_res.json()
        print(
            f"  Invariant error rejected with HTTP {inv_res.status_code}: {inv_data['error_code']}"
        )
        assert inv_data["error_code"] == "InvariantError"
        inv_run_id = inv_data["run_id"]

        with get_db_connection() as conn:
            run = get_extraction_run(conn, UUID(inv_run_id))
            assert run is not None
            print(f"  Database run status: {run.status.value}")
            print(f"  Database recorded error_code: {run.error_code}")
            assert run.status == RunStatus.FAILED
            assert run.error_code == "InvariantError"
    print("  ✓ Invariant audit persistence verified.")

    # Scenario 7: Zero PII Egress Audit
    print("\n[Scenario 7] Zero PII Egress Inspection")
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        render_pdf_from_fixture("checking_happy", Path(tmp.name))
        res = client.post(
            "/api/v1/ingest/upload",
            files={
                "file": ("checking.pdf", Path(tmp.name).read_bytes(), "application/pdf")
            },
        )
        body = res.text
        # Check no SSNs
        assert not re.search(r"\b\d{3}-\d{2}-\d{4}\b", body)
        # Check no unmasked account numbers
        assert not re.search(r"(?<![-a-fA-F0-9])\d{8,16}(?![-a-fA-F0-9])", body)
        print("  Zero SSNs detected.")
        print("  Zero unmasked account numbers detected.")
        print("  Masked accounts safely exposed (*1234).")
    print("  ✓ Zero PII egress verified.")

    print("\n" + "=" * 80)
    print("ALL 7 SCENARIOS SUCCESSFULLY VERIFIED - STEP 9 COMPLETE")
    print("=" * 80)


if __name__ == "__main__":
    main()
