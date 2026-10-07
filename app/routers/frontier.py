"""Frontier aggregate-only audit handoff (Phase 2).

Cloud frontier models may only receive schemas and Tier-2 aggregates from
``app.mcp.cloud_tools``. Tier-1 row-level SQL egress remains quarantined on
``POST /v1/orchestrator/execute_sql`` (HTTP 501).

Endpoints:
- POST /chat/frontier/audit — Aggregate-only audit via cloud tools.
- GET  /chat/frontier/schemas — Schema introspection for planning.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.mcp.cloud_tools import (
    CloudToolError,
    execute_cloud_query,
    get_financial_summary,
    get_schema_info,
)

logger = logging.getLogger("app.routers.frontier")
router = APIRouter(prefix="/chat/frontier", tags=["frontier"])

# ── Request / Response Schemas ───────────────────────────────────────


class FrontierAuditRequest(BaseModel):
    """Aggregate-only frontier audit request.

    Exactly one of ``group_by`` (financial summary) or ``aggregate_sql``
    (validated cloud SELECT with aggregations) must drive the handoff.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    group_by: str | None = Field(
        default=None,
        description="Grouping column for get_financial_summary (Tier 2).",
    )
    aggregate_sql: Annotated[
        str | None,
        Field(
            default=None,
            max_length=4096,
            description="SELECT-only aggregate SQL for execute_cloud_query.",
        ),
    ] = None
    parameters: list[str | int | float | bool | None] | None = Field(
        default=None,
        description="Optional bind parameters for aggregate_sql.",
    )
    include_schemas: bool = Field(
        default=False,
        description="When true, include schema metadata alongside aggregates.",
    )
    plan_id: str | None = Field(
        default=None,
        description="Optional plan identifier for correlation.",
    )
    question: Annotated[
        str | None,
        Field(
            default=None,
            max_length=2048,
            description="Optional analysis question (must not contain PII).",
        ),
    ] = None

    @model_validator(mode="after")
    def require_aggregate_source(self) -> FrontierAuditRequest:
        if not self.group_by and not self.aggregate_sql:
            raise ValueError(
                "At least one of group_by or aggregate_sql must be provided."
            )
        return self


class FrontierAuditResponse(BaseModel):
    """Aggregate-only frontier audit response (never Tier-1 row sets)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    execution_id: str
    status: str
    summary: dict[str, Any] | None = None
    aggregates: list[dict[str, Any]] | None = None
    schemas: list[dict[str, Any]] | None = None
    plan_id: str | None = None


class FrontierAuditErrorDetail(BaseModel):
    """Structured error for failed frontier audit requests."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str
    execution_id: str
    error_code: str
    detail: str
    plan_id: str | None = None


class FrontierSchemaResponse(BaseModel):
    """Schema metadata for frontier planning (no row data)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tables: list[dict[str, Any]]


# ── In-Memory Execution Store ────────────────────────────────────────

_execution_store: dict[str, dict[str, object]] = {}


def _load_schemas() -> list[dict[str, Any]]:
    """Load allowed table schemas without row data or PII values."""
    schema_info = get_schema_info()
    tables: list[dict[str, Any]] = []
    for table_name in schema_info.get("tables", []):
        try:
            tables.append(get_schema_info(table_name=table_name))
        except CloudToolError:
            logger.warning(
                "Skipped disallowed or unavailable table during frontier schema load",
                extra={"table": table_name},
            )
    return tables


# ── Route Handlers ───────────────────────────────────────────────────


@router.post(
    "/audit",
    response_model=FrontierAuditResponse,
    responses={
        400: {"description": "Cloud tool validation failure"},
        500: {"description": "Aggregate execution unavailable"},
    },
)
async def frontier_audit(payload: FrontierAuditRequest) -> FrontierAuditResponse:
    """Execute Tier-2 aggregate tools for frontier audit handoff.

    Never invokes Tier-1 ``execute_raw_sql`` and never returns a ``results``
    row-set field. Only ``summary`` / ``aggregates`` / optional ``schemas``.
    """
    execution_id = uuid.uuid4().hex
    plan_id = payload.plan_id

    logger.info(
        "Frontier aggregate audit requested",
        extra={
            "execution_id": execution_id,
            "plan_id": plan_id,
            "has_group_by": payload.group_by is not None,
            "has_aggregate_sql": payload.aggregate_sql is not None,
            "include_schemas": payload.include_schemas,
            "question_length": len(payload.question) if payload.question else 0,
        },
    )

    summary: dict[str, Any] | None = None
    aggregates: list[dict[str, Any]] | None = None

    try:
        if payload.group_by is not None:
            summary = get_financial_summary(group_by=payload.group_by)
        if payload.aggregate_sql is not None:
            aggregates = execute_cloud_query(
                payload.aggregate_sql,
                parameters=payload.parameters,
            )
        schemas = _load_schemas() if payload.include_schemas else None
    except CloudToolError as exc:
        detail = str(exc)
        logger.warning(
            "Frontier aggregate validation failed",
            extra={
                "execution_id": execution_id,
                "plan_id": plan_id,
                "error_type": type(exc).__name__,
            },
        )
        _execution_store[execution_id] = {
            "status": "failed",
            "error_code": "CLOUD_TOOL_VALIDATION_ERROR",
            "error_message": detail,
            "plan_id": plan_id,
        }
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "status": "failed",
                "execution_id": execution_id,
                "error_code": "CLOUD_TOOL_VALIDATION_ERROR",
                "detail": detail,
                "plan_id": plan_id,
            },
        ) from exc
    except Exception as exc:
        logger.error(
            "Frontier aggregate execution unavailable",
            extra={
                "execution_id": execution_id,
                "plan_id": plan_id,
                "error_type": type(exc).__name__,
            },
        )
        _execution_store[execution_id] = {
            "status": "failed",
            "error_code": "AGGREGATE_EXECUTION_ERROR",
            "error_message": "Aggregate execution unavailable",
            "plan_id": plan_id,
        }
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "status": "failed",
                "execution_id": execution_id,
                "error_code": "AGGREGATE_EXECUTION_ERROR",
                "detail": "Aggregate execution unavailable",
                "plan_id": plan_id,
            },
        ) from exc

    _execution_store[execution_id] = {
        "status": "completed",
        "plan_id": plan_id,
        "has_summary": summary is not None,
        "aggregate_row_count": len(aggregates) if aggregates is not None else 0,
    }

    logger.info(
        "Frontier aggregate audit completed",
        extra={
            "execution_id": execution_id,
            "plan_id": plan_id,
            "has_summary": summary is not None,
            "aggregate_row_count": len(aggregates) if aggregates is not None else 0,
        },
    )

    return FrontierAuditResponse(
        execution_id=execution_id,
        status="completed",
        summary=summary,
        aggregates=aggregates,
        schemas=schemas,
        plan_id=plan_id,
    )


@router.get(
    "/schemas",
    response_model=FrontierSchemaResponse,
    description="Return database schemas for frontier aggregate planning.",
)
def frontier_schemas() -> FrontierSchemaResponse:
    """Return table/column metadata without row data or PII values."""
    logger.info("Frontier schema introspection requested")
    try:
        return FrontierSchemaResponse(tables=_load_schemas())
    except CloudToolError as exc:
        logger.error(
            "Frontier schema introspection failed",
            extra={"error_type": type(exc).__name__},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Schema introspection failed: {exc}",
        ) from exc
    except Exception as exc:
        logger.error(
            "Frontier schema introspection unavailable",
            extra={"error_type": type(exc).__name__},
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Schema introspection unavailable",
        ) from exc
