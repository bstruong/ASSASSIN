"""Fail-loud extraction core for PDF documents.

Extracts raw pages, raw integer millipoint tokens, and performs closed-schema validation
(header drift, missing mandatory section markers, ragged rows, and exact two-digit currency parsing).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

import pdfplumber

from app.models.enums import RunStatus
from app.models.exceptions import (
    MissingSectionError,
    SchemaDriftError,
    TokenError,
)
from app.models.raw import (
    ExtractionRun,
    RawExtraction,
    RawPage,
    RawPayload,
    RawToken,
)
from app.models.schema import TableSchema


def extract_pdf_to_raw(
    file_path: Path,
    adapter_id: str,
    adapter_version: str,
    original_basename: str | None = None,
) -> RawExtraction:
    """Extract raw pages and tokens from a PDF file with integer geometry.

    Args:
        file_path: Path to the target PDF file.
        adapter_id: Identifier of the invoking adapter.
        adapter_version: Version string of the invoking adapter.
        original_basename: Optional original filename override.

    Returns:
        A populated RawExtraction instance.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file has no pages or is not a readable PDF.
    """
    resolved = file_path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"PDF file not found: {resolved}")

    file_bytes = resolved.read_bytes()
    if not file_bytes:
        raise ValueError(f"PDF file is empty: {resolved}")

    sha256_hash = hashlib.sha256(file_bytes).hexdigest()
    byte_length = len(file_bytes)

    payload = RawPayload(
        content_sha256=sha256_hash,
        byte_length=byte_length,
        original_basename=original_basename
        if original_basename is not None
        else resolved.name,
    )

    run = ExtractionRun(
        raw_payload_id=payload.raw_payload_id,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        status=RunStatus.EXTRACTED,
    )

    pages: list[RawPage] = []
    with pdfplumber.open(resolved) as pdf:
        if not pdf.pages:
            raise ValueError(f"PDF has no pages: {resolved}")

        for page_idx, page in enumerate(pdf.pages):
            page_number = page_idx + 1
            page_text = page.extract_text() or ""
            tokens: list[RawToken] = []
            page_id = uuid4()

            # 1. Extract words
            for word in page.extract_words():
                w_text = word.get("text", "")
                x0 = float(word.get("x0", 0.0))
                top = float(word.get("top", 0.0))
                x1 = float(word.get("x1", 0.0))
                bottom = float(word.get("bottom", 0.0))

                x0_mp = max(0, round(x0 * 1000))
                y0_mp = max(0, round(top * 1000))
                x1_mp = max(x0_mp + 1, round(x1 * 1000))
                y1_mp = max(y0_mp + 1, round(bottom * 1000))

                tokens.append(
                    RawToken(
                        raw_token_id=uuid4(),
                        raw_page_id=page_id,
                        token_kind="word",
                        token_text=w_text,
                        x0_mp=x0_mp,
                        y0_mp=y0_mp,
                        x1_mp=x1_mp,
                        y1_mp=y1_mp,
                    )
                )

            # 2. Extract table cells
            tables = page.find_tables()

            for t_idx, table in enumerate(tables):
                for r_idx, row in enumerate(table.rows):
                    for c_idx, cell in enumerate(row.cells):
                        if cell is not None:
                            cx0, ctop, cx1, cbottom = cell
                            x0_mp = max(0, round(float(cx0) * 1000))
                            y0_mp = max(0, round(float(ctop) * 1000))
                            x1_mp = max(x0_mp + 1, round(float(cx1) * 1000))
                            y1_mp = max(y0_mp + 1, round(float(cbottom) * 1000))

                            tokens.append(
                                RawToken(
                                    raw_token_id=uuid4(),
                                    raw_page_id=page_id,
                                    token_kind="cell",
                                    token_text="",
                                    x0_mp=x0_mp,
                                    y0_mp=y0_mp,
                                    x1_mp=x1_mp,
                                    y1_mp=y1_mp,
                                    table_index=t_idx,
                                    row_index=r_idx,
                                    col_index=c_idx,
                                )
                            )

            pages.append(
                RawPage(
                    raw_page_id=page_id,
                    run_id=run.run_id,
                    page_number=page_number,
                    page_text=page_text,
                    tokens=tokens,
                )
            )

    return RawExtraction(
        payload=payload,
        run=run,
        pages=pages,
    )


def validate_extraction_against_schemas(
    extraction: RawExtraction,
    schemas: Sequence[TableSchema],
) -> None:
    """Validate that all mandatory section markers declared in schemas exist in page text.

    Args:
        extraction: The raw extracted pages.
        schemas: The sequence of declared table schemas.

    Raises:
        MissingSectionError: If any mandatory section marker is absent.
    """
    all_text = "\n".join(page.page_text for page in extraction.pages)
    for schema in schemas:
        for marker in schema.section_markers:
            if marker not in all_text:
                raise MissingSectionError(
                    f"Mandatory section marker {marker!r} for schema {schema.name!r} not found."
                )


def validate_table_against_schema(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    schema: TableSchema,
) -> None:
    """Assert exact header match and uniform row width.

    Args:
        headers: The extracted header strings.
        rows: The extracted data rows.
        schema: The expected TableSchema.

    Raises:
        SchemaDriftError: If headers differ (extra, missing, or renamed) or any row is ragged.
    """
    clean_headers = tuple(str(h).strip().lower() for h in headers)
    expected_headers = tuple(str(h).strip().lower() for h in schema.headers)

    if clean_headers != expected_headers:
        raise SchemaDriftError(
            f"Header drift in table {schema.name!r}: "
            f"expected {expected_headers}, got {clean_headers}"
        )

    expected_len = len(expected_headers)
    for row_idx, row in enumerate(rows):
        if len(row) != expected_len:
            raise SchemaDriftError(
                f"Ragged row in table {schema.name!r} at index {row_idx}: "
                f"expected {expected_len} columns, got {len(row)}"
            )


def parse_currency_to_cents(amount_str: str, allow_zero: bool = False) -> int:
    """Parse a currency string into signed integer cents.

    Requirements:
    - USD only, strictly 2 minor digits (e.g. '12.34', '$100.00').
    - Disallows non-2 decimal places (e.g. '12', '12.3', '12.345').
    - Disallows float conversion.
    - Handles standard accounting negative notations: '($12.34)', '-$12.34', '$-12.34'.
    - Disallows zero amount unless allow_zero is explicitly True.

    Args:
        amount_str: Raw text representing currency.
        allow_zero: Whether $0.00 is allowed (e.g. for statement summary totals).

    Returns:
        Signed integer cents.

    Raises:
        TokenError: If formatting is invalid, decimal digits != 2, or amount is zero.
    """
    if not amount_str or not isinstance(amount_str, str):
        raise TokenError(
            f"Amount string must be a non-empty string, got {amount_str!r}"
        )

    cleaned = amount_str.strip()
    is_negative = False

    # Check for parentheses negative: (12.34) or ($12.34)
    if cleaned.startswith("(") and cleaned.endswith(")"):
        is_negative = True
        cleaned = cleaned[1:-1].strip()

    # Check for minus sign or plus sign
    if cleaned.startswith("-"):
        is_negative = True
        cleaned = cleaned[1:].strip()
    elif cleaned.startswith("+"):
        cleaned = cleaned[1:].strip()

    # Strip currency symbol
    if cleaned.startswith("$"):
        cleaned = cleaned[1:].strip()

    # Check for embedded minus or plus after currency symbol: $-12.34 or $+12.34
    if cleaned.startswith("-"):
        is_negative = True
        cleaned = cleaned[1:].strip()
    elif cleaned.startswith("+"):
        cleaned = cleaned[1:].strip()

    # Remove commas
    cleaned = cleaned.replace(",", "")

    # Must contain exactly one decimal point with exactly 2 digits after it
    if "." not in cleaned:
        raise TokenError(
            f"Invalid currency '{amount_str}': currency must have exactly 2 decimal digits."
        )

    parts = cleaned.split(".")
    if len(parts) != 2:
        raise TokenError(
            f"Invalid currency '{amount_str}': multiple decimal points detected."
        )

    dollars_part, cents_part = parts
    if not dollars_part or not dollars_part.isdigit():
        raise TokenError(
            f"Invalid currency '{amount_str}': non-digit or empty dollar component."
        )

    if len(cents_part) != 2 or not cents_part.isdigit():
        raise TokenError(
            f"Invalid currency '{amount_str}': cents component must be exactly 2 digits."
        )

    cents = int(dollars_part) * 100 + int(cents_part)
    if cents == 0 and not allow_zero:
        raise TokenError("Zero amount is not permitted.")

    return -cents if is_negative else cents
