"""FastAPI router for the Frontier-to-Local Orchestrator.

Tier-1 row-level SQL egress is quarantined (POST /execute_sql → HTTP 501)
until an aggregate-only handoff is available. Schema introspection remains
available for planning. This router is not mounted on the production app.

Endpoints:
- POST /v1/orchestrator/execute_sql — Quarantined (501); no Tier-1 row sets.
- GET  /v1/orchestrator/status/{execution_id} — Query execution status.
- GET  /v1/orchestrator/schemas — Return database schemas for frontier planning.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.mcp.cloud_tools import CloudToolError, get_schema_info

logger = logging.getLogger("app.routers.orchestrator")
router = APIRouter(prefix="/v1/orchestrator", tags=["orchestrator"])

# ── Request / Response Schemas ───────────────────────────────────────


class SqlExecutionRequest(BaseModel):
    """Payload submitted by the frontier model for local SQL execution.

    The frontier model generates SQL plans based on schemas only — no PII
    or raw data should be present in this payload.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sql: Annotated[
        str,
        Field(
            min_length=1,
            max_length=4096,
            description="Parameterized SQL statement to execute locally.",
        ),
    ]
    parameters: list[str | int | float | bool | None] | None = Field(
        default=None,
        description="Optional parameterized values for the SQL statement.",
    )
    plan_id: str | None = Field(
        default=None,
        description="Optional plan identifier from the frontier model for correlation.",
    )


class SqlExecutionResponse(BaseModel):
    """Response from successful local SQL execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_id: str
    status: str
    row_count: int
    results: list[dict[str, object]]
    plan_id: str | None = None


class SqlExecutionErrorResponse(BaseModel):
    """Structured error response for failed SQL execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str
    execution_id: str
    error_code: str
    detail: str
    plan_id: str | None = None


class SchemaResponse(BaseModel):
    """Database schema information for frontier model planning."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tables: list[dict[str, object]]


class ExecutionStatusResponse(BaseModel):
    """Status of a previously submitted SQL execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_id: str
    status: str  # "completed", "failed"
    row_count: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    plan_id: str | None = None


# ── In-Memory Execution Store ────────────────────────────────────────
# Lightweight store for tracking execution status.
# In production this would be a persistent store (Redis/DB).

_execution_store: dict[str, dict[str, object]] = {}


# ── Route Handlers ───────────────────────────────────────────────────


@router.post(
    "/execute_sql",
    response_model=SqlExecutionErrorResponse,
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    responses={
        501: {
            "model": SqlExecutionErrorResponse,
            "description": "Frontier Tier-1 row egress is quarantined.",
        },
    },
)
async def execute_sql_plan(
    payload: SqlExecutionRequest,
) -> SqlExecutionErrorResponse:
    """Quarantined: refuse Tier-1 row-level SQL egress to frontier clients.

    Production frontier handoff must use cloud aggregate tools only. This
    endpoint remains mounted for contract discovery but never returns
    Tier-1 ``execute_raw_sql`` result sets.

    Args:
        payload: SQL execution request from the frontier model (ignored).

    Returns:
        SqlExecutionErrorResponse with HTTP 501.
    """
    execution_id = uuid.uuid4().hex
    plan_id = payload.plan_id
    detail = (
        "Frontier Tier-1 SQL egress is quarantined until aggregate-only "
        "handoff is available. Use cloud aggregate tools instead."
    )

    logger.warning(
        "Rejected quarantined frontier SQL execution",
        extra={
            "execution_id": execution_id,
            "plan_id": plan_id,
            "sql_prefix": payload.sql[:80],
        },
    )

    _execution_store[execution_id] = {
        "status": "failed",
        "error_code": "FRONTIER_QUARANTINED",
        "error_message": detail,
        "plan_id": plan_id,
    }

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "status": "failed",
            "execution_id": execution_id,
            "error_code": "FRONTIER_QUARANTINED",
            "detail": detail,
            "plan_id": plan_id,
        },
    )


@router.get(
    "/status/{execution_id}",
    response_model=ExecutionStatusResponse,
    responses={404: {"description": "Execution not found"}},
)
def get_execution_status(
    execution_id: str,
) -> ExecutionStatusResponse:
    """Retrieve the status of a previously submitted SQL execution.

    Args:
        execution_id: The unique execution identifier returned by execute_sql_plan.

    Returns:
        ExecutionStatusResponse with current status.

    Raises:
        HTTPException(404): If the execution_id is not found.
    """
    record = _execution_store.get(execution_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Execution {execution_id} not found.",
        )

    return ExecutionStatusResponse(
        execution_id=execution_id,
        status=record["status"],
        row_count=record.get("row_count"),
        error_code=record.get("error_code"),
        error_message=record.get("error_message"),
        plan_id=record.get("plan_id"),
    )


@router.get(
    "/schemas",
    response_model=SchemaResponse,
    description="Return database schemas for frontier model SQL planning.",
)
def get_database_schemas() -> SchemaResponse:
    """Return database schema information for frontier model SQL planning.

    This endpoint provides table and column metadata without exposing any
    data or PII, enabling the frontier model to generate accurate SQL plans.

    Returns:
        SchemaResponse containing table names and column metadata.
    """
    logger.info("Schema introspection requested by frontier model")

    try:
        schema_info = get_schema_info()
        tables = []
        for table_name in schema_info.get("tables", []):
            try:
                table_details = get_schema_info(table_name=table_name)
                tables.append(table_details)
            except CloudToolError as exc:
                logger.warning(
                    "Failed to introspect table schema",
                    extra={"table": table_name, "error": str(exc)},
                )

        return SchemaResponse(tables=tables)

    except CloudToolError as exc:
        logger.error(
            "Schema introspection failed",
            extra={"error_type": type(exc).__name__},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Schema introspection failed: {exc}",
        ) from exc
    except Exception as exc:
        logger.error(
            "Schema introspection unavailable",
            extra={"error_type": type(exc).__name__},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Schema introspection unavailable",
        ) from exc
