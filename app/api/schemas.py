"""Pydantic schemas for the Local Ingest API.

Strictly enforces zero PII egress:
- No customer names, SSNs, physical addresses, or raw scraped lines.
- Safe masked account representations only.
- Strict contract enforcement: frozen models with extra='forbid'.
"""

from __future__ import annotations

import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.enums import AccountDomain, AccountType, CurrencyCode, RunStatus


class AccountSummaryResponse(BaseModel):
    """Safe, non-PII account metadata representation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: UUID
    institution: str
    account_mask: str
    account_domain: AccountDomain
    account_type: AccountType
    currency: CurrencyCode


class StatementSummaryResponse(BaseModel):
    """High-level statement summary with zero customer PII."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    statement_id: UUID
    account_id: UUID
    statement_start_date: datetime.date
    statement_end_date: datetime.date
    transactions_count: int


class IngestSuccessResponse(BaseModel):
    """Payload returned upon successful statement ingestion and canonical persistence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: RunStatus
    run_id: UUID
    raw_payload_id: UUID
    adapter_id: str
    adapter_version: str
    statements_count: int
    transactions_count: int
    accounts: list[AccountSummaryResponse]


class ExtractionRunDetailResponse(BaseModel):
    """Audit details for an extraction run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    raw_payload_id: UUID
    adapter_id: str
    adapter_version: str
    status: RunStatus
    started_at: datetime.datetime
    error_code: str | None = None
    error_message: str | None = None
    statements_count: int
    transactions_count: int
    accounts: list[AccountSummaryResponse]


class IngestErrorResponse(BaseModel):
    """Structured error response for failed ingestion or invariant violations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str
    run_id: UUID | None = None
    raw_payload_id: UUID | None = None
    adapter_id: str | None = None
    error_code: str
    detail: str


class HealthResponse(BaseModel):
    """System health and connectivity status."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str
    database: str
    registered_adapters_count: int
    version: str
