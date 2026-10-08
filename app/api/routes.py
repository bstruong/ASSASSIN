"""FastAPI route handlers for local statement ingestion and status querying.

Guarantees:
- Zero PII egress in API responses and structured logs.
- Strict contract enforcement: fails loudly on invalid payloads or schemas.
- Complete round-trip extraction and canonical persistence across all domains.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse

from app.adapters.base import CombinedStatementAdapter
from app.adapters.registry import AdapterRegistryError, get_default_registry
from app.api.schemas import (
    AccountSummaryResponse,
    ExtractionRunDetailResponse,
    HealthResponse,
    IngestErrorResponse,
    IngestSuccessResponse,
)
from app.db.connection import get_db_connection
from app.db.repository import (
    get_extraction_run,
    get_run_statements,
    persist_canonical_statement,
    persist_raw_extraction,
    persist_raw_payload,
    update_run_status,
)
from app.extraction.core import extract_pdf_to_raw
from app.models.enums import RunStatus
from app.models.exceptions import (
    AmbiguousAccountsError,
    InvariantError,
    MissingSectionError,
    PersistenceError,
    SchemaDriftError,
    TokenError,
)
from app.models.raw import RawExtraction

logger = logging.getLogger("app.api.ingest")
router = APIRouter(prefix="/api/v1")


@router.get("/health", response_model=HealthResponse)
def health_check() -> HealthResponse:
    """Return system health status and database connectivity."""
    db_status = "connected"
    try:
        with get_db_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1;")
    except Exception as exc:  # noqa: BLE001
        logger.error("Health check database failure", extra={"error": str(exc)})
        db_status = "error"

    registry = get_default_registry()
    return HealthResponse(
        status="ok" if db_status == "connected" else "degraded",
        database=db_status,
        registered_adapters_count=len(registry.list_adapters()),
        version="0.1.0",
    )


@router.post(
    "/ingest/upload",
    response_model=IngestSuccessResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {
            "model": IngestErrorResponse,
            "description": "Invalid payload / bad request",
        },
        422: {
            "model": IngestErrorResponse,
            "description": "Schema, contract, or invariant violation",
        },
    },
)
async def upload_statement(
    file: Annotated[UploadFile, File(...)],
) -> IngestSuccessResponse | JSONResponse:
    """Ingest, extract, reconcile, and persist a financial statement PDF."""
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only PDF documents are supported for ingestion.",
        )

    content = await file.read()
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file payload is empty.",
        )

    if not content.startswith(b"%PDF"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is not a valid PDF binary.",
        )

    safe_basename = Path(file.filename).name
    logger.info(
        "Received statement upload",
        extra={"original_basename": safe_basename, "byte_length": len(content)},
    )

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        tmp.write(content)

    try:
        # 1. Match adapter exclusively
        registry = get_default_registry()
        try:
            adapter = registry.match(tmp_path)
        except AdapterRegistryError as exc:
            logger.warning(
                "Adapter registry matching failed",
                extra={"error_type": type(exc).__name__},
            )
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content=IngestErrorResponse(
                    status="failed",
                    error_code="ADAPTER_MATCH_FAILED",
                    detail=str(exc),
                ).model_dump(mode="json"),
            )

        # 2. Extract raw pages and tokens
        try:
            raw_extraction = extract_pdf_to_raw(
                file_path=tmp_path,
                adapter_id=adapter.adapter_id,
                adapter_version=adapter.adapter_version,
                original_basename=safe_basename,
            )
        except (ValueError, FileNotFoundError) as exc:
            logger.error(
                "Raw page and token extraction failed",
                extra={"error_type": type(exc).__name__},
            )
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content=IngestErrorResponse(
                    status="failed",
                    error_code="EXTRACTION_FAILED",
                    detail=str(exc),
                ).model_dump(mode="json"),
            )

        # 3. Store raw append-only extraction in database
        with get_db_connection() as conn:
            actual_payload_id = persist_raw_payload(conn, raw_extraction.payload)
            if raw_extraction.payload.raw_payload_id != actual_payload_id:
                raw_extraction = RawExtraction(
                    payload=raw_extraction.payload.model_copy(
                        update={"raw_payload_id": actual_payload_id}
                    ),
                    run=raw_extraction.run.model_copy(
                        update={"raw_payload_id": actual_payload_id}
                    ),
                    pages=raw_extraction.pages,
                )
            persist_raw_extraction(conn, raw_extraction)

        run_id = raw_extraction.run.run_id

        # 4. Parse canonical financial model(s)
        try:
            parsed = adapter.parse_canonical(raw_extraction)
            if isinstance(adapter, CombinedStatementAdapter):
                bundles: Sequence[tuple] = parsed  # type: ignore[assignment]
            else:
                bundles = [parsed]  # type: ignore[list-item]

            # 5. Persist canonical statements and sidecars
            with get_db_connection() as conn:
                for bundle in bundles:
                    acc, stmt, summary, txns = bundle[:4]
                    holdings = bundle[4] if len(bundle) > 4 else ()
                    persist_canonical_statement(
                        conn, acc, stmt, summary, txns, holdings=holdings
                    )
                update_run_status(conn, run_id, RunStatus.CANONICAL_PERSISTED)

            logger.info(
                "Statement successfully ingested and persisted",
                extra={
                    "run_id": str(run_id),
                    "adapter_id": adapter.adapter_id,
                    "statements_count": len(bundles),
                },
            )

            accounts_summary = [
                AccountSummaryResponse(
                    account_id=acc.account_id,
                    institution=acc.institution,
                    account_mask=acc.account_mask,
                    account_domain=acc.account_domain,
                    account_type=acc.account_type,
                    currency=acc.currency,
                )
                for acc, *_rest in bundles
            ]

            total_txns = sum(len(bundle[3]) for bundle in bundles)
            return IngestSuccessResponse(
                status=RunStatus.CANONICAL_PERSISTED,
                run_id=run_id,
                raw_payload_id=actual_payload_id,
                adapter_id=adapter.adapter_id,
                adapter_version=adapter.adapter_version,
                statements_count=len(bundles),
                transactions_count=total_txns,
                accounts=accounts_summary,
            )

        except (
            InvariantError,
            MissingSectionError,
            AmbiguousAccountsError,
            SchemaDriftError,
            TokenError,
            PersistenceError,
            ValueError,
        ) as exc:
            logger.error(
                "Statement processing failed invariant/schema contract",
                extra={
                    "run_id": str(run_id),
                    "adapter_id": adapter.adapter_id,
                    "error_type": type(exc).__name__,
                },
            )
            # Record failed extraction run in DB
            with get_db_connection() as conn:
                update_run_status(
                    conn,
                    run_id,
                    RunStatus.FAILED,
                    error_code=type(exc).__name__,
                    error_message=str(exc),
                )

            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content=IngestErrorResponse(
                    status="failed",
                    run_id=run_id,
                    raw_payload_id=actual_payload_id,
                    adapter_id=adapter.adapter_id,
                    error_code=type(exc).__name__,
                    detail=str(exc),
                ).model_dump(mode="json"),
            )

    finally:
        if tmp_path.exists():
            tmp_path.unlink()


@router.get(
    "/ingest/runs/{run_id}",
    response_model=ExtractionRunDetailResponse,
    responses={404: {"description": "Run not found"}},
)
def get_run_status(run_id: UUID) -> ExtractionRunDetailResponse:
    """Retrieve audit details for a specific extraction run without customer PII."""
    with get_db_connection() as conn:
        run = get_extraction_run(conn, run_id)
        if not run:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Extraction run {run_id} not found.",
            )

        bundles = get_run_statements(conn, run_id)
        accounts_summary = [
            AccountSummaryResponse(
                account_id=acc.account_id,
                institution=acc.institution,
                account_mask=acc.account_mask,
                account_domain=acc.account_domain,
                account_type=acc.account_type,
                currency=acc.currency,
            )
            for acc, _, _ in bundles
        ]
        total_txns = sum(txn_count for _, _, txn_count in bundles)

        return ExtractionRunDetailResponse(
            run_id=run.run_id,
            raw_payload_id=run.raw_payload_id,
            adapter_id=run.adapter_id,
            adapter_version=run.adapter_version,
            status=run.status,
            started_at=run.started_at,
            error_code=run.error_code,
            error_message=run.error_message,
            statements_count=len(bundles),
            transactions_count=total_txns,
            accounts=accounts_summary,
        )
