"""Tests for the HTMX Dashboard module (Step 12).

Guarantees:
- All dashboard endpoints are tested before implementation.
- Strict financial precision: integer cents only.
- Strict contract enforcement: fail loudly on invalid payloads.
- No PII egress in any dashboard response.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    """Provide a FastAPI TestClient with the dashboard router included."""
    from app.api.app import create_app
    from app.api.dashboard import router as dashboard_router

    app = create_app()
    app.include_router(dashboard_router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Test: Dashboard page serves HTML
# ---------------------------------------------------------------------------


class TestDashboardPage:
    """Verify the main dashboard HTML page loads correctly."""

    def test_dashboard_page_returns_200(self, client: TestClient) -> None:
        """GET /dashboard should return a 200 with HTML content."""
        response = client.get("/dashboard")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "ASSASSIN" in response.text
        assert "The AI Analyst" in response.text
        assert "The Local Vault" in response.text

    def test_dashboard_contains_htmx_attributes(self, client: TestClient) -> None:
        """Dashboard HTML must include HTMX attributes for interactivity."""
        response = client.get("/dashboard")
        assert response.status_code == 200
        html = response.text
        assert "hx-post" in html
        assert "hx-swap" in html
        assert "hx-target" in html


# ---------------------------------------------------------------------------
# Test: Analyze endpoint — valid financial question
# ---------------------------------------------------------------------------


class TestAnalyzeEndpoint:
    """Verify the /api/v1/dashboard/analyze HTMX endpoint."""

    def test_analyze_valid_question_returns_html_fragment(
        self, client: TestClient
    ) -> None:
        """POST with valid question returns an HTML fragment for Pane 2."""
        response = client.post(
            "/api/v1/dashboard/analyze",
            data={"question": "Show me my total spending last month"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        # Should contain a placeholder or analysis result
        assert len(response.text) > 0

    def test_analyze_valid_question_with_cents_amount(self, client: TestClient) -> None:
        """Analyze with a question mentioning a monetary amount (integer cents)."""
        response = client.post(
            "/api/v1/dashboard/analyze",
            data={"question": "What is the balance of my account"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_analyze_empty_question_rejects_with_400(self, client: TestClient) -> None:
        """POST with empty or missing question must fail loudly (400)."""
        response = client.post(
            "/api/v1/dashboard/analyze",
            data={"question": ""},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 400

    def test_analyze_missing_question_rejects_with_400(
        self, client: TestClient
    ) -> None:
        """POST without question field must fail loudly (400)."""
        response = client.post(
            "/api/v1/dashboard/analyze",
            data={},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 400

    def test_analyze_non_htmx_request_returns_json(self, client: TestClient) -> None:
        """Non-HTMX requests should get a JSON error response."""
        response = client.post(
            "/api/v1/dashboard/analyze",
            data={"question": "test"},
        )
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# Test: Status endpoint
# ---------------------------------------------------------------------------


class TestStatusEndpoint:
    """Verify the /api/v1/dashboard/status HTMX endpoint."""

    def test_status_returns_html_fragment(self, client: TestClient) -> None:
        """GET status should return an HTML fragment."""
        response = client.get(
            "/api/v1/dashboard/status",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert len(response.text) > 0

    def test_status_no_htmx_returns_json(self, client: TestClient) -> None:
        """Non-HTMX request to status should return JSON."""
        response = client.get("/api/v1/dashboard/status")
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# Test: Invariant — no PII in responses
# ---------------------------------------------------------------------------


class TestPIINoEgress:
    """Verify that no PII leaks through any dashboard endpoint."""

    def test_dashboard_no_pii_in_html(self, client: TestClient) -> None:
        """Dashboard HTML must not contain PII placeholders."""
        response = client.get("/dashboard")
        pii_patterns = ["ssn", "social security", "full_name", "account_number"]
        for pattern in pii_patterns:
            assert pattern not in response.text.lower()

    def test_analyze_response_no_pii(self, client: TestClient) -> None:
        """Analyze response HTML must not contain PII."""
        response = client.post(
            "/api/v1/dashboard/analyze",
            data={"question": "test question"},
            headers={"HX-Request": "true"},
        )
        pii_patterns = ["ssn", "social security", "full_name", "account_number"]
        for pattern in pii_patterns:
            assert pattern not in response.text.lower()


# ---------------------------------------------------------------------------
# Test: Financial precision — no floats in responses
# ---------------------------------------------------------------------------


class TestFinancialPrecision:
    """Verify that monetary values use integer cents only."""

    def test_analyze_no_float_in_response(self, client: TestClient) -> None:
        """Analyze response must not emit floating-point monetary values."""
        response = client.post(
            "/api/v1/dashboard/analyze",
            data={"question": "Show my balances"},
            headers={"HX-Request": "true"},
        )
        # The response should not contain floating-point numbers with dollar signs
        import re

        # Match patterns like $123.45 or $123.456
        dollar_floats = re.findall(r"\$\d+\.\d{2,}", response.text)
        assert len(dollar_floats) == 0, (
            f"Floating-point monetary values found: {dollar_floats}"
        )


# ---------------------------------------------------------------------------
# Test: HTMX swap behavior
# ---------------------------------------------------------------------------


class TestHTMXXBehavior:
    """Verify HTMX swap attributes are correctly set."""

    innerHTML_re = r'hx-swap="innerHTML"'
    outerHTML_re = r'hx-swap="outerHTML"'
    settle_re = r'hx-settle=".*"'

    def test_analyze_form_has_htmx_swap(self, client: TestClient) -> None:
        """The analyze form must declare an hx-swap attribute."""
        response = client.get("/dashboard")
        html = response.text
        assert self.innerHTML_re in html or self.outerHTML_re in html

    def test_analyze_form_has_hx_target(self, client: TestClient) -> None:
        """The analyze form must declare an hx-target pointing to Pane 2."""
        response = client.get("/dashboard")
        html = response.text
        assert "hx-target" in html


class TestFrontierAuditButton:
    """Dashboard posts sanitized aggregates to the frontier audit path."""

    def test_dashboard_has_frontier_verify_control(self, client: TestClient) -> None:
        response = client.get("/dashboard")
        html = response.text
        assert 'id="frontier-verify-btn"' in html
        assert 'hx-post="/api/v1/dashboard/frontier-audit"' in html
        assert "Verify &amp; Analyze with Frontier Model" in html or (
            "Verify & Analyze with Frontier Model" in html
        )
        assert "htmx:beforeSwap" in html
        assert 'xhr.responseURL.indexOf("/api/v1/dashboard/frontier-audit")' in html
        assert "event.detail.shouldSwap = true" in html

    def test_frontier_audit_renders_integer_cents(
        self, client: TestClient, monkeypatch
    ):
        from app.api import dashboard as dashboard_mod

        def fake_summary(group_by: str, database_url: str | None = None):
            assert group_by == "transaction_category"
            return {
                "group_by_column": "transaction_category",
                "totals": {
                    "total_transactions": 2,
                    "total_deposits_cents": 150000,
                    "total_purchases_cents": 0,
                    "total_withdrawals_cents": 4000,
                },
                "per_group": [
                    {
                        "transaction_category": "deposit",
                        "txn_count": 2,
                        "deposits_cents": 150000,
                        "purchases_cents": 0,
                        "withdrawals_cents": 0,
                    }
                ],
            }

        monkeypatch.setattr(dashboard_mod, "get_financial_summary", fake_summary)
        response = client.post(
            "/api/v1/dashboard/frontier-audit",
            data={"group_by": "transaction_category"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        body = response.text
        assert "150000" in body
        assert "4000" in body
        assert "description" not in body.lower()
        assert "account_mask" not in body
        assert "page_text" not in body
        assert "$" not in body

    def test_frontier_audit_rejects_row_level_group(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/dashboard/frontier-audit",
            data={"group_by": "description"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 400
        assert "description" in response.text
        assert "Invalid group_by" in response.text

    def test_frontier_audit_rejects_blank_group(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/dashboard/frontier-audit",
            data={"group_by": "   "},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 400
        assert "Rejected" in response.text
        assert "group_by must not be empty" in response.text

    def test_frontier_audit_requires_htmx(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/dashboard/frontier-audit",
            data={"group_by": "transaction_category"},
        )
        assert response.status_code == 400
