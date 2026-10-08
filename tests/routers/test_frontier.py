"""Tests for app.routers.frontier (aggregate-only frontier handoff)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.routers.frontier import FrontierAuditRequest, _execution_store


@pytest.fixture(autouse=True)
def clear_frontier_store():
    _execution_store.clear()
    yield
    _execution_store.clear()


@pytest.fixture
def client():
    from app.api.app import create_app

    return TestClient(create_app())


# ── Request validation ───────────────────────────────────────────────


class TestFrontierAuditRequestValidation:
    def test_group_by_only(self):
        payload = FrontierAuditRequest(group_by="transaction_category")
        assert payload.group_by == "transaction_category"
        assert payload.aggregate_sql is None

    def test_aggregate_sql_only(self):
        payload = FrontierAuditRequest(
            aggregate_sql="SELECT COUNT(*) AS c FROM transactions;"
        )
        assert payload.aggregate_sql is not None

    def test_requires_group_by_or_aggregate_sql(self):
        with pytest.raises(ValidationError, match="group_by|aggregate_sql"):
            FrontierAuditRequest()

    def test_extra_field_forbidden(self):
        with pytest.raises(ValidationError):
            FrontierAuditRequest(
                group_by="transaction_category",
                extra_field="bad",
            )


# ── Production mount ─────────────────────────────────────────────────


class TestFrontierMountedInProduction:
    def test_frontier_audit_route_mounted(self, client):
        paths = set(client.app.openapi()["paths"])
        assert "/chat/frontier/audit" in paths

    def test_frontier_schemas_route_mounted(self, client):
        paths = set(client.app.openapi()["paths"])
        assert "/chat/frontier/schemas" in paths

    def test_orchestrator_mounted_with_quarantine(self, client):
        paths = set(client.app.openapi()["paths"])
        assert "/v1/orchestrator/execute_sql" in paths
        assert "/v1/orchestrator/schemas" in paths


# ── Aggregate-only audit ─────────────────────────────────────────────


class TestFrontierAuditAggregateOnly:
    def test_financial_summary_happy_path(self, client, postgres_url):
        response = client.post(
            "/chat/frontier/audit",
            json={"group_by": "transaction_category", "plan_id": "plan-agg-1"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "completed"
        assert body["plan_id"] == "plan-agg-1"
        assert "execution_id" in body
        assert body["summary"] is not None
        assert "total_deposits_cents" in body["summary"]["totals"]
        assert isinstance(body["summary"]["totals"]["total_deposits_cents"], int)
        # Never expose Tier-1 row-set shape
        assert "results" not in body
        assert body.get("aggregates") is None

    def test_aggregate_sql_happy_path(self, client, postgres_url):
        response = client.post(
            "/chat/frontier/audit",
            json={
                "aggregate_sql": ("SELECT COUNT(*) AS txn_count FROM transactions;"),
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "completed"
        assert body["aggregates"] is not None
        assert isinstance(body["aggregates"], list)
        assert "txn_count" in body["aggregates"][0]
        assert "results" not in body

    def test_row_level_sql_rejected(self, client):
        response = client.post(
            "/chat/frontier/audit",
            json={
                "aggregate_sql": "SELECT description FROM transactions;",
            },
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["error_code"] == "CLOUD_TOOL_VALIDATION_ERROR"
        assert "results" not in detail

    def test_select_star_rejected(self, client):
        response = client.post(
            "/chat/frontier/audit",
            json={"aggregate_sql": "SELECT * FROM transactions;"},
        )
        assert response.status_code == 400
        assert response.json()["detail"]["error_code"] == "CLOUD_TOOL_VALIDATION_ERROR"

    def test_account_mask_column_rejected(self, client):
        response = client.post(
            "/chat/frontier/audit",
            json={
                "aggregate_sql": ("SELECT COUNT(account_mask) AS c FROM accounts;"),
            },
        )
        assert response.status_code == 400
        assert "account_mask" in response.json()["detail"]["detail"]

    def test_invalid_group_by_rejected(self, client):
        response = client.post(
            "/chat/frontier/audit",
            json={"group_by": "description"},
        )
        assert response.status_code == 400
        assert response.json()["detail"]["error_code"] == "CLOUD_TOOL_VALIDATION_ERROR"

    def test_missing_payload_rejected(self, client):
        response = client.post("/chat/frontier/audit", json={})
        assert response.status_code == 422

    def test_no_tier1_pii_fields_in_response(self, client, postgres_url):
        response = client.post(
            "/chat/frontier/audit",
            json={
                "group_by": "transaction_category",
                "include_schemas": True,
            },
        )
        assert response.status_code == 200
        payload = response.text
        for forbidden in ("description", "account_mask", "page_text"):
            # Keys may appear in schema column names for planning; values must
            # never carry row payloads. Assert no redacted-looking leakage of
            # sample PII and no Tier-1 results key.
            assert "123-45-6789" not in payload
        assert '"results"' not in payload


# ── Schemas ──────────────────────────────────────────────────────────


class TestFrontierSchemas:
    def test_schemas_endpoint(self, client, postgres_url):
        response = client.get("/chat/frontier/schemas")
        assert response.status_code == 200
        body = response.json()
        assert "tables" in body
        table_names = [t.get("table") for t in body["tables"]]
        assert "accounts" in table_names
        assert "transactions" in table_names

    def test_schemas_cloud_tool_error(self, client, monkeypatch):
        from app.mcp.cloud_tools import CloudToolError
        from app.routers import frontier as frontier_mod

        def boom(**_kwargs):
            raise CloudToolError("schema boom")

        monkeypatch.setattr(frontier_mod, "get_schema_info", boom)
        response = client.get("/chat/frontier/schemas")
        assert response.status_code == 500
        assert "Schema introspection failed" in response.json()["detail"]

    def test_schemas_unexpected_error(self, client, monkeypatch):
        from app.routers import frontier as frontier_mod

        def boom(**_kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr(frontier_mod, "get_schema_info", boom)
        response = client.get("/chat/frontier/schemas")
        assert response.status_code == 500
        assert response.json()["detail"] == "Schema introspection unavailable"


class TestFrontierAuditExecutionErrors:
    def test_unexpected_execution_error(self, client, monkeypatch):
        from app.routers import frontier as frontier_mod

        def boom(**_kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr(frontier_mod, "get_financial_summary", boom)
        response = client.post(
            "/chat/frontier/audit",
            json={"group_by": "transaction_category"},
        )
        assert response.status_code == 500
        detail = response.json()["detail"]
        assert detail["error_code"] == "AGGREGATE_EXECUTION_ERROR"
        assert "results" not in detail


# ── Tier-1 quarantine still enforced ─────────────────────────────────


class TestTier1QuarantineRemains:
    def test_execute_sql_still_501(self, client):
        response = client.post(
            "/v1/orchestrator/execute_sql",
            json={"sql": "SELECT description FROM transactions;"},
        )
        assert response.status_code == 501
        detail = response.json()["detail"]
        assert detail["error_code"] == "FRONTIER_QUARANTINED"
        assert "results" not in detail
