"""In-memory models for append-only raw extraction data.

These models represent the geometry, raw tokens, and text extracted from a PDF
document before any domain or monetary interpretation.  All coordinates are stored
as integer millipoints (1/1000 of a PDF point) to prevent floating-point precision issues.
"""

from __future__ import annotations

import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import RunStatus


class RawToken(BaseModel):
    """A single word or table cell token extracted from a PDF page.

    Coordinates use integer millipoints (1/1000 PDF point) in pdfplumber space:
    origin at top-left, x increasing rightwards, y increasing downwards.
    Invariants: x1_mp > x0_mp >= 0 and y1_mp > y0_mp >= 0.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    raw_token_id: UUID = Field(default_factory=uuid4)
    raw_page_id: UUID
    token_kind: Literal["word", "cell"]
    token_text: str
    x0_mp: int = Field(ge=0)
    y0_mp: int = Field(ge=0)
    x1_mp: int
    y1_mp: int
    table_index: int | None = None
    row_index: int | None = None
    col_index: int | None = None

    @model_validator(mode="after")
    def validate_geometry(self) -> RawToken:
        if self.x1_mp <= self.x0_mp:
            raise ValueError(
                f"x1_mp ({self.x1_mp}) must be strictly greater than x0_mp ({self.x0_mp})"
            )
        if self.y1_mp <= self.y0_mp:
            raise ValueError(
                f"y1_mp ({self.y1_mp}) must be strictly greater than y0_mp ({self.y0_mp})"
            )
        return self


class RawPage(BaseModel):
    """Extracted text and tokens for a single page in an extraction run."""

    model_config = ConfigDict(frozen=True, strict=True)

    raw_page_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    page_number: int = Field(ge=1)
    page_text: str
    tokens: list[RawToken] = Field(default_factory=list)


class RawPayload(BaseModel):
    """Raw blob identity record keyed by SHA-256."""

    model_config = ConfigDict(frozen=True, strict=True)

    raw_payload_id: UUID = Field(default_factory=uuid4)
    content_sha256: str = Field(min_length=64, max_length=64)
    byte_length: int = Field(gt=0)
    original_basename: str = Field(min_length=1)
    ingested_at: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC)
    )


class ExtractionRun(BaseModel):
    """Execution lifecycle record for an adapter run against a raw payload."""

    model_config = ConfigDict(frozen=True, strict=True)

    run_id: UUID = Field(default_factory=uuid4)
    raw_payload_id: UUID
    adapter_id: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    started_at: datetime.datetime = Field(
        default_factory=lambda: datetime.datetime.now(datetime.UTC)
    )
    status: RunStatus
    error_code: str | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def validate_failure_error_code(self) -> ExtractionRun:
        is_failed = self.status == RunStatus.FAILED
        has_error = self.error_code is not None
        if is_failed != has_error:
            raise ValueError(
                f"status='failed' must correlate exactly with error_code presence. "
                f"status={self.status!r}, error_code={self.error_code!r}"
            )
        return self


class RawExtraction(BaseModel):
    """In-memory envelope returned by adapter.extract(path).

    Contains all raw pages, tokens, run metadata, and payload metadata without
    any monetary interpretation.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    payload: RawPayload
    run: ExtractionRun
    pages: list[RawPage] = Field(min_length=1)
