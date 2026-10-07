"""Tests that production create_app mounts PII middleware and frontier routes."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.middleware.sanitization import PiiSanitizationMiddleware


class TestProductionAppMounts:
    def test_pii_middleware_mounted(self):
        app = create_app()
        assert any(
            getattr(m, "cls", None) is PiiSanitizationMiddleware
            for m in app.user_middleware
        )

    def test_frontier_and_orchestrator_routes_present(self):
        app = create_app()
        paths = set(app.openapi()["paths"])
        assert "/chat/frontier/audit" in paths
        assert "/v1/orchestrator/execute_sql" in paths

    def test_frontier_response_scrubs_row_pii_keys(self):
        """Belt-and-suspenders: row PII keys cannot leave frontier paths."""
        app = create_app()

        @app.get("/chat/frontier/leak-probe")
        async def leak_probe():
            return {
                "description": "SECRET PAYEE NAME",
                "account_mask": "*9999",
                "page_text": "raw OCR line with secrets",
                "total_deposits_cents": 12345,
            }

        client = TestClient(app)
        response = client.get("/chat/frontier/leak-probe")
        assert response.status_code == 200
        body = response.json()
        assert body["description"] == "[REDACTED]"
        assert body["account_mask"] == "[REDACTED]"
        assert body["page_text"] == "[REDACTED]"
        # Integer cents aggregates must survive for frontier analysis
        assert body["total_deposits_cents"] == 12345

    def test_ingest_account_mask_still_returned(self):
        """Local ingest may return safe masks; global middleware must not strip them."""
        app = create_app()

        @app.get("/api/v1/mask-probe")
        async def mask_probe():
            return {"account_mask": "*1234", "institution": "Test Bank"}

        client = TestClient(app)
        response = client.get("/api/v1/mask-probe")
        assert response.status_code == 200
        assert response.json()["account_mask"] == "*1234"

    def test_trace_header_preserved_on_json_response(self):
        app = create_app()
        client = TestClient(app)
        response = client.get(
            "/api/v1/health",
            headers={"x-trace-id": "trace-preserve-001"},
        )
        assert response.status_code == 200
        assert response.headers.get("X-Trace-ID") == "trace-preserve-001"
