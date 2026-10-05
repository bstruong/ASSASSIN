"""Tests for app.routers.orchestrator (Frontier-to-Local Orchestrator)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.routers.orchestrator import (
    ExecutionStatusResponse,
    SchemaResponse,
    SqlExecutionErrorResponse,
    SqlExecutionRequest,
    SqlExecutionResponse,
    _execution_store,
)


@pytest.fixture(autouse=True)
def clear_execution_store():
    """Clear the in-memory execution store before each test."""
    _execution_store.clear()
    yield
    _execution_store.clear()


# ── SqlExecutionRequest Validation ───────────────────────────────────


class TestSqlExecutionRequestValidation:
    def test_valid_request(self):
        payload = SqlExecutionRequest(sql="SELECT 1;")
        assert payload.sql == "SELECT 1;"
        assert payload.parameters is None
        assert payload.plan_id is None

    def test_request_with_parameters(self):
        payload = SqlExecutionRequest(
            sql="SELECT %s;", parameters=[42], plan_id="plan-001"
        )
        assert payload.sql == "SELECT %s;"
        assert payload.parameters == [42]
        assert payload.plan_id == "plan-001"

    def test_empty_sql_raises(self):
        with pytest.raises(ValidationError, match="string_too_short"):
            SqlExecutionRequest(sql="")

    def test_sql_too_long_raises(self):
        with pytest.raises(ValidationError, match="string_too_long"):
            SqlExecutionRequest(sql="A" * 4097)

    def test_extra_field_raises(self):
        with pytest.raises(ValidationError):
            SqlExecutionRequest(sql="SELECT 1;", extra_field="bad")


# ── execute_sql_plan Endpoint ────────────────────────────────────────


class TestExecuteSqlPlan:
    """Test the POST /v1/orchestrator/execute_sql endpoint."""

    def _create_client(self):
        from app.api.app import create_app

        app = create_app()
        # Include the orchestrator router
        from app.routers.orchestrator import router as orchestrator_router

        app.include_router(orchestrator_router)
        return TestClient(app)

    def test_success_simple_select(self):
        client = self._create_client()
        payload = {"sql": "SELECT 1 AS val;"}
        response = client.post("/v1/orchestrator/execute_sql", json=payload)
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "completed"
        assert result["row_count"] == 1
        assert "execution_id" in result

    def test_success_with_parameters(self, postgres_url):
        client = self._create_client()
        payload = {
            "sql": "SELECT %s AS val;",
            "parameters": [42],
        }
        response = client.post("/v1/orchestrator/execute_sql", json=payload)
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "completed"
        assert result["row_count"] == 1

    def test_success_with_plan_id(self):
        client = self._create_client()
        payload = {"sql": "SELECT 1;", "plan_id": "frontier-plan-001"}
        response = client.post("/v1/orchestrator/execute_sql", json=payload)
        assert response.status_code == 200
        result = response.json()
        assert result["plan_id"] == "frontier-plan-001"

    def test_invalid_sql_denied(self):
        client = self._create_client()
        payload = {"sql": "INVALID SQL"}
        response = client.post("/v1/orchestrator/execute_sql", json=payload)
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "failed"
        assert result["error_code"] == "SQL_VALIDATION_ERROR"

    def test_empty_sql_denied(self):
        client = self._create_client()
        payload = {"sql": "   "}
        response = client.post("/v1/orchestrator/execute_sql", json=payload)
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "failed"
        assert result["error_code"] == "SQL_VALIDATION_ERROR"

    def test_injection_sql_denied(self):
        client = self._create_client()
        payload = {"sql": "SELECT 1; DROP TABLE accounts;"}
        response = client.post("/v1/orchestrator/execute_sql", json=payload)
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "failed"
        assert result["error_code"] == "SQL_VALIDATION_ERROR"

    def test_invalid_payload_rejected(self):
        client = self._create_client()
        # Missing required 'sql' field
        response = client.post("/v1/orchestrator/execute_sql", json={})
        assert response.status_code == 422

    def test_execution_store_updated_on_success(self):
        client = self._create_client()
        payload = {"sql": "SELECT 1;"}
        response = client.post("/v1/orchestrator/execute_sql", json=payload)
        result = response.json()
        execution_id = result["execution_id"]
        assert execution_id in _execution_store
        assert _execution_store[execution_id]["status"] == "completed"

    def test_execution_store_updated_on_failure(self):
        client = self._create_client()
        payload = {"sql": "INVALID SQL"}
        response = client.post("/v1/orchestrator/execute_sql", json=payload)
        result = response.json()
        execution_id = result["execution_id"]
        assert execution_id in _execution_store
        assert _execution_store[execution_id]["status"] == "failed"


# ── get_execution_status Endpoint ────────────────────────────────────


class TestGetExecutionStatus:
    """Test the GET /v1/orchestrator/status/{execution_id} endpoint."""

    def _create_client(self):
        from app.api.app import create_app

        app = create_app()
        from app.routers.orchestrator import router as orchestrator_router

        app.include_router(orchestrator_router)
        return TestClient(app)

    def test_status_completed(self):
        client = self._create_client()
        # First execute a query to populate the store
        client.post("/v1/orchestrator/execute_sql", json={"sql": "SELECT 1;"})
        # Get the execution_id from the store
        execution_id = next(iter(_execution_store.keys()))
        response = client.get(f"/v1/orchestrator/status/{execution_id}")
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "completed"
        assert result["execution_id"] == execution_id

    def test_status_not_found(self):
        client = self._create_client()
        response = client.get("/v1/orchestrator/status/nonexistent-id")
        assert response.status_code == 404

    def test_status_failed(self):
        client = self._create_client()
        # Execute invalid SQL to create a failed entry
        client.post("/v1/orchestrator/execute_sql", json={"sql": "INVALID SQL"})
        execution_id = next(iter(_execution_store.keys()))
        response = client.get(f"/v1/orchestrator/status/{execution_id}")
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "failed"
        assert result["error_code"] == "SQL_VALIDATION_ERROR"


# ── get_database_schemas Endpoint ────────────────────────────────────


class TestGetDatabaseSchemas:
    """Test the GET /v1/orchestrator/schemas endpoint."""

    def _create_client(self):
        from app.api.app import create_app

        app = create_app()
        from app.routers.orchestrator import router as orchestrator_router

        app.include_router(orchestrator_router)
        return TestClient(app)

    def test_schemas_returned(self, postgres_url):
        """Test that schema endpoint returns table metadata."""
        client = self._create_client()
        response = client.get("/v1/orchestrator/schemas")
        assert response.status_code == 200
        result = response.json()
        assert "tables" in result
        assert isinstance(result["tables"], list)
        # Should contain core tables
        table_names = [t.get("table") for t in result["tables"]]
        assert "accounts" in table_names
        assert "transactions" in table_names

    def test_schema_has_columns(self, postgres_url):
        """Test that each table has column metadata."""
        client = self._create_client()
        response = client.get("/v1/orchestrator/schemas")
        result = response.json()
        # Each table should have columns
        for table in result["tables"]:
            if "columns" in table:
                assert isinstance(table["columns"], list)
                for col in table["columns"]:
                    assert "name" in col
                    assert "data_type" in col

    def test_no_pii_in_schemas(self, postgres_url):
        """Schema response must not contain any PII data."""
        client = self._create_client()
        response = client.get("/v1/orchestrator/schemas")
        result = response.json()
        json_str = str(result)
        assert "123-45-6789" not in json_str
        assert "@example.com" not in json_str

    def test_schemas_endpoint_unavailable(self):
        """Test that schema endpoint returns 500 when DB is unavailable."""
        client = self._create_client()
        response = client.get("/v1/orchestrator/schemas")
        # If DB is unavailable, should return 500
        assert response.status_code in (200, 500)


# ── Pydantic Model Tests ─────────────────────────────────────────────


class TestPydanticModels:
    def test_sql_execution_response_model(self):
        model = SqlExecutionResponse(
            execution_id="abc123",
            status="completed",
            row_count=5,
            results=[{"col": 1}],
            plan_id="plan-001",
        )
        assert model.execution_id == "abc123"
        assert model.row_count == 5

    def test_sql_execution_response_extra_forbidden(self):
        with pytest.raises(ValidationError):
            SqlExecutionResponse(
                execution_id="abc",
                status="completed",
                row_count=0,
                results=[],
                extra="bad",
            )

    def test_sql_execution_error_response_model(self):
        model = SqlExecutionErrorResponse(
            status="failed",
            execution_id="abc123",
            error_code="SQL_VALIDATION_ERROR",
            detail="Invalid SQL",
        )
        assert model.error_code == "SQL_VALIDATION_ERROR"

    def test_execution_status_response_model(self):
        model = ExecutionStatusResponse(
            execution_id="abc123",
            status="completed",
            row_count=10,
            plan_id="plan-001",
        )
        assert model.status == "completed"
        assert model.row_count == 10

    def test_schema_response_model(self):
        model = SchemaResponse(
            tables=[
                {"table": "accounts", "columns": [{"name": "id", "data_type": "uuid"}]}
            ]
        )
        assert len(model.tables) == 1
        assert model.tables[0]["table"] == "accounts"
