"""Comprehensive test suite for Local Ingest FastAPI application (Step 9).

Validates:
- Healthcheck endpoint
- Depository round-trip statement ingestion
- Credit card round-trip statement ingestion
- Combined multi-account round-trip statement ingestion
- Fail-loud validation (non-PDF, empty, corrupt, unmatched adapter, invariant breaks)
- Zero PII egress in API responses and structured logs
- Static AST validation against dynamic metaprogramming in app/api
"""

from __future__ import annotations

import ast
import json
import logging
import re
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from app.api.app import create_app
from app.db.connection import init_db
from app.db.repository import get_extraction_run
from app.models.enums import AccountDomain, AccountType, RunStatus

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def render_pdf_from_fixture(fixture_name: str, target_path: Path) -> Path:
    """Render a synthetic PDF document from a test fixture JSON."""
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
    """Render custom lines of text into a PDF document."""
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


@pytest.fixture()
def client(db_conn: psycopg.Connection) -> TestClient:
    """FastAPI TestClient with initialized schema."""
    init_db(db_conn)
    app = create_app()
    return TestClient(app)


class TestHealthEndpoint:
    """Healthcheck endpoint verification."""

    def test_health_check_ok(self, client: TestClient) -> None:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["database"] == "connected"
        assert payload["registered_adapters_count"] >= 4
        assert payload["version"] == "0.1.0"


class TestDepositoryIngestRoundtrip:
    """Single-account depository round-trip upload and persistence."""

    def test_upload_checking_statement_success(
        self, client: TestClient, tmp_path: Path, db_conn: psycopg.Connection
    ) -> None:
        pdf_path = render_pdf_from_fixture("checking_happy", tmp_path / "checking.pdf")
        pdf_bytes = pdf_path.read_bytes()

        response = client.post(
            "/api/v1/ingest/upload",
            files={"file": ("checking_jan2025.pdf", pdf_bytes, "application/pdf")},
        )
        assert response.status_code == 201
        data = response.json()

        # Trace context header
        assert "X-Trace-ID" in response.headers
        assert len(response.headers["X-Trace-ID"]) > 0

        assert data["status"] == "canonical_persisted"
        assert data["adapter_id"] == "standard_depository"
        assert data["statements_count"] == 1
        assert data["transactions_count"] == 4
        assert len(data["accounts"]) == 1

        acc = data["accounts"][0]
        assert acc["account_mask"] == "*1234"
        assert acc["account_domain"] == AccountDomain.DEPOSITORY.value
        assert acc["account_type"] == AccountType.CHECKING.value
        assert acc["currency"] == "USD"

        # Verify run retrieval via GET /api/v1/ingest/runs/{run_id}
        run_id = data["run_id"]
        run_res = client.get(f"/api/v1/ingest/runs/{run_id}")
        assert run_res.status_code == 200
        run_data = run_res.json()
        assert run_data["run_id"] == run_id
        assert run_data["status"] == "canonical_persisted"
        assert run_data["statements_count"] == 1
        assert run_data["transactions_count"] == 4
        assert run_data["accounts"][0]["account_mask"] == "*1234"

        # Verify database extraction run status
        db_run = get_extraction_run(db_conn, UUID(run_id))
        assert db_run is not None
        assert db_run.status == RunStatus.CANONICAL_PERSISTED


class TestCreditCardIngestRoundtrip:
    """Single-account credit card round-trip upload and persistence."""

    def test_upload_card_statement_success(
        self, client: TestClient, tmp_path: Path, db_conn: psycopg.Connection
    ) -> None:
        pdf_path = render_pdf_from_fixture("card_happy", tmp_path / "card.pdf")
        pdf_bytes = pdf_path.read_bytes()

        response = client.post(
            "/api/v1/ingest/upload",
            files={"file": ("card_jan2025.pdf", pdf_bytes, "application/pdf")},
        )
        assert response.status_code == 201
        data = response.json()

        assert data["status"] == "canonical_persisted"
        assert data["adapter_id"] == "standard_credit_card"
        assert data["statements_count"] == 1
        assert data["transactions_count"] == 4
        assert len(data["accounts"]) == 1

        acc = data["accounts"][0]
        assert acc["account_mask"] == "*9876"
        assert acc["account_domain"] == AccountDomain.REVOLVING_CREDIT.value
        assert acc["account_type"] == AccountType.CREDIT_CARD.value

        # Inspect run endpoint
        run_res = client.get(f"/api/v1/ingest/runs/{data['run_id']}")
        assert run_res.status_code == 200
        run_data = run_res.json()
        assert run_data["statements_count"] == 1
        assert run_data["transactions_count"] == 4


class TestCombinedIngestRoundtrip:
    """Multi-account combined depository round-trip upload and persistence."""

    def test_upload_combined_statement_success(
        self, client: TestClient, tmp_path: Path, db_conn: psycopg.Connection
    ) -> None:
        pdf_path = render_pdf_from_fixture(
            "combined_checking_savings_happy", tmp_path / "combined.pdf"
        )
        pdf_bytes = pdf_path.read_bytes()

        response = client.post(
            "/api/v1/ingest/upload",
            files={"file": ("combined_jan2025.pdf", pdf_bytes, "application/pdf")},
        )
        assert response.status_code == 201
        data = response.json()

        assert data["status"] == "canonical_persisted"
        assert data["adapter_id"] == "standard_combined_depository"
        assert data["statements_count"] == 2
        assert data["transactions_count"] == 3
        assert len(data["accounts"]) == 2

        masks = {a["account_mask"] for a in data["accounts"]}
        assert masks == {"*1234", "*5678"}

        types = {a["account_type"] for a in data["accounts"]}
        assert types == {AccountType.CHECKING.value, AccountType.SAVINGS.value}

        # Verify run retrieval
        run_res = client.get(f"/api/v1/ingest/runs/{data['run_id']}")
        assert run_res.status_code == 200
        run_data = run_res.json()
        assert run_data["statements_count"] == 2
        assert run_data["transactions_count"] == 3


class TestFailLoudValidation:
    """Fail-loud contract validation and structured error response."""

    def test_reject_non_pdf_extension(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/ingest/upload",
            files={
                "file": ("statement.csv", b"date,amount\n2025-01-01,100", "text/csv")
            },
        )
        assert response.status_code == 400
        assert "Only PDF documents are supported" in response.json()["detail"]

    def test_reject_empty_payload(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/ingest/upload",
            files={"file": ("statement.pdf", b"", "application/pdf")},
        )
        assert response.status_code == 400
        assert "empty" in response.json()["detail"]

    def test_reject_invalid_pdf_bytes(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/ingest/upload",
            files={
                "file": ("statement.pdf", b"NOT A VALID PDF HEADER", "application/pdf")
            },
        )
        assert response.status_code == 400
        assert "not a valid PDF" in response.json()["detail"]

    def test_reject_unmatched_bank_statement(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        pdf_path = render_custom_pdf(
            ["UNKNOWN FINANCIAL INSTITUTION", "STATEMENT FOR 2025"],
            tmp_path / "unknown.pdf",
        )
        response = client.post(
            "/api/v1/ingest/upload",
            files={"file": ("unknown.pdf", pdf_path.read_bytes(), "application/pdf")},
        )
        assert response.status_code == 422
        data = response.json()
        assert data["status"] == "failed"
        assert data["error_code"] == "ADAPTER_MATCH_FAILED"

    def test_reject_invariant_violation_and_record_run_failure(
        self, client: TestClient, tmp_path: Path, db_conn: psycopg.Connection
    ) -> None:
        # Create a statement with broken reconciliation (ending balance off by $0.01)
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
            "Ending Balance: $1,295.01",  # Off by 1 cent! Stated 1,295.01 vs actual 1,295.00
            "TRANSACTION DETAILS",
            "Date | Description | Amount | Balance",
            "2025-01-05 | Payroll Deposit | +$500.00 | $1,500.00",
            "2025-01-10 | ATM Withdrawal | -$200.00 | $1,300.00",
            "2025-01-15 | Monthly Service Fee | -$10.00 | $1,290.00",
            "2025-01-31 | Interest Paid | +$5.00 | $1,295.00",
            "END OF STATEMENT",
        ]
        pdf_path = render_custom_pdf(lines, tmp_path / "broken_reconciliation.pdf")
        response = client.post(
            "/api/v1/ingest/upload",
            files={"file": ("broken.pdf", pdf_path.read_bytes(), "application/pdf")},
        )
        assert response.status_code == 422
        data = response.json()
        assert data["status"] == "failed"
        assert data["error_code"] == "InvariantError"
        assert data["run_id"] is not None

        # Verify run status in database is FAILED
        run = get_extraction_run(db_conn, UUID(data["run_id"]))
        assert run is not None
        assert run.status == RunStatus.FAILED
        assert run.error_code == "InvariantError"
        assert "reconciliation" in run.error_message.lower()

    def test_reject_missing_mandatory_section(
        self, client: TestClient, tmp_path: Path, db_conn: psycopg.Connection
    ) -> None:
        # Create a statement missing TRANSACTION DETAILS
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
            "Ending Balance: $1,295.00",
            # Missing TRANSACTION DETAILS
            "END OF STATEMENT",
        ]
        pdf_path = render_custom_pdf(lines, tmp_path / "missing_section.pdf")
        response = client.post(
            "/api/v1/ingest/upload",
            files={
                "file": (
                    "missing_section.pdf",
                    pdf_path.read_bytes(),
                    "application/pdf",
                )
            },
        )
        assert response.status_code == 422
        data = response.json()
        assert data["status"] == "failed"
        assert data["error_code"] == "MissingSectionError"

    def test_run_not_found_returns_404(self, client: TestClient) -> None:
        non_existent_id = uuid4()
        response = client.get(f"/api/v1/ingest/runs/{non_existent_id}")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()


class TestZeroPIIEgress:
    """Verify zero PII egress in API responses and structured logs."""

    def test_response_contains_zero_pii(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        pdf_path = render_pdf_from_fixture("checking_happy", tmp_path / "checking.pdf")
        response = client.post(
            "/api/v1/ingest/upload",
            files={"file": ("checking.pdf", pdf_path.read_bytes(), "application/pdf")},
        )
        assert response.status_code == 201
        body_text = response.text

        # 1. No SSN format (\d{3}-\d{2}-\d{4})
        assert not re.search(r"\b\d{3}-\d{2}-\d{4}\b", body_text)

        # 2. No unmasked 8-16 digit account numbers (excluding UUID segments)
        assert not re.search(r"(?<![-a-fA-F0-9])\d{8,16}(?![-a-fA-F0-9])", body_text)

        # 3. Only masked account representation (*1234)
        assert "*1234" in body_text

    def test_logs_contain_zero_monetary_or_pii_leaks(
        self, client: TestClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO)
        pdf_path = render_pdf_from_fixture("checking_happy", tmp_path / "checking.pdf")

        response = client.post(
            "/api/v1/ingest/upload",
            files={"file": ("checking.pdf", pdf_path.read_bytes(), "application/pdf")},
        )
        assert response.status_code == 201

        # Audit all log messages emitted during the request
        for record in caplog.records:
            msg = record.getMessage()
            # No dollar amounts printed in log text ($1000.00, etc.)
            assert "$" not in msg, f"Leaked monetary dollar sign in log: {msg}"
            # No SSN patterns
            assert not re.search(r"\b\d{3}-\d{2}-\d{4}\b", msg)
            # No raw account numbers (must be UUID or masked)
            assert not re.search(r"(?<![-a-fA-F0-9])\d{8,16}(?![-a-fA-F0-9])", msg)


class TestASTInvariants:
    """Verify that app/api adheres to AST static analysis invariants."""

    def test_no_dynamic_metaprogramming_in_api(self) -> None:
        """Verify app/api contains no eval, exec, or dynamic reflection."""
        api_dir = Path(__file__).parents[1] / "app" / "api"
        for py_file in api_dir.glob("*.py"):
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))

            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name):
                        assert node.func.id not in {"eval", "exec"}, (
                            f"Forbidden dynamic call '{node.func.id}' in {py_file.name}:{node.lineno}"
                        )
                    elif isinstance(node.func, ast.Attribute) and node.func.attr in {
                        "eval",
                        "exec",
                    }:
                        raise AssertionError(
                            f"Forbidden dynamic method '{node.func.attr}' in {py_file.name}:{node.lineno}"
                        )
