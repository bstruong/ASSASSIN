"""Tier 1 MCP Tools: Raw SQL execution and raw PDF text extraction.

These tools are accessible only to local models. They provide:
- Parameterized raw SQL query execution against the PostgreSQL database.
- Raw PDF text extraction via pdfplumber.

All tools validate inputs and fail loudly on invalid payloads.
No PII or unmasked financial amounts are logged.
"""

from __future__ import annotations

import logging
import re
import uuid as uuid_mod
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.db.connection import get_connection
from app.models.exceptions import PipelineError

logger = logging.getLogger("app.mcp.local_tools")

_ALLOWED_STATEMENT_PREFIXES = frozenset(
    {
        "SELECT",
        "INSERT",
        
        
        
        
        
        
        "EXPLAIN",
        
    }
)

_INJECTION_PATTERNS = re.compile(
    r"(?:--\s|;\s*DROP\s|;\s*ALTER\s|;\s*DELETE\s|;\s*INSERT\s|;\s*UPDATE\s|\bOR\b\s+\d+\s*=.*\b\d+)",
    re.IGNORECASE,
)


class SqlExecutionError(PipelineError):
    """Raised when SQL execution fails validation or encounters a database error."""


def _validate_sql_statement(sql: str) -> str:
    """Validate that a SQL statement uses only allowed prefixes."""
    stripped = sql.strip()
    if not stripped:
        raise SqlExecutionError("SQL statement must not be empty.")
    first_word = stripped.split()[0].upper()
    if first_word not in _ALLOWED_STATEMENT_PREFIXES:
        raise SqlExecutionError(
            f"SQL must start with one of: {sorted(_ALLOWED_STATEMENT_PREFIXES)}. Got: {first_word!r}"
        )
    if _INJECTION_PATTERNS.search(stripped):
        raise SqlExecutionError("SQL statement contains potential injection pattern.")
    return stripped


def _validate_parameters(params: Sequence[Any] | None) -> Sequence[Any]:
    """Validate that query parameters are a simple sequence of basic types."""
    if params is None:
        return ()
    if not isinstance(params, (list, tuple)):
        raise SqlExecutionError("Query parameters must be a list or tuple.")
    for idx, param in enumerate(params):
        if not isinstance(param, (str, int, float, bool, type(None))):
            raise SqlExecutionError(
                f"Parameter at index {idx} has unsupported type: {type(param).__name__}"
            )
        if isinstance(param, str) and _INJECTION_PATTERNS.search(param):
            raise SqlExecutionError(
                f"Parameter at index {idx} contains potential injection pattern."
            )
    return params


def execute_raw_sql(
    sql: str,
    parameters: Sequence[Any] | None = None,
    database_url: str | None = None,
) -> list[dict[str, Any]]:
    """Execute a parameterized SQL query and return results as list of dicts.

    Tier 1 tool accessible only to local models.

    Args:
        sql: A parameterized SQL statement (uses %s placeholders).
        parameters: Optional sequence of parameter values.
        database_url: Optional override for the database connection URL.

    Returns:
        List of dicts, one per row, with column names as keys.

    Raises:
        SqlExecutionError: If SQL validation fails.
        psycopg.Error: If a database error occurs.
    """
    validated_sql = _validate_sql_statement(sql)
    validated_params = _validate_parameters(parameters)

    logger.info(
        "Executing raw SQL query (Tier 1)",
        extra={
            "sql_prefix": validated_sql[:80],
            "param_count": len(validated_params),
        },
    )

    conn = get_connection(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(validated_sql, validated_params)
            if not cur.description:
                rowcount = cur.rowcount
                logger.info("Non-SELECT SQL executed", extra={"rowcount": rowcount})
                return [{"status": "success", "rowcount": rowcount}]
            columns = [desc.name for desc in cur.description]
            rows = cur.fetchall()
            result = [dict(zip(columns, row)) for row in rows]
            logger.info("SQL query returned rows", extra={"row_count": len(result)})
            return result
    finally:
        conn.close()


def read_raw_pdf(file_path: str) -> list[dict[str, Any]]:
    """Extract raw text from a PDF file using pdfplumber.

    Tier 1 tool accessible only to local models.

    Args:
        file_path: Path to a PDF file.

    Returns:
        List of dicts with page_number, text, and char_count per page.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file is not a valid PDF.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF file not found: {file_path}")
    if not path.name.lower().endswith(".pdf"):
        raise ValueError(f"File is not a PDF: {file_path}")

    with open(path, "rb") as f:
        header = f.read(5)
    if not header.startswith(b"%PDF-"):
        raise ValueError(f"File does not appear to be a valid PDF: {file_path}")

    logger.info(
        "Extracting raw PDF text (Tier 1)",
        extra={
            "file_path": str(path),
            "file_size": path.stat().st_size,
        },
    )

    try:
        import pdfplumber
    except ImportError:
        raise ImportError(
            "pdfplumber is required for PDF extraction. Install with: uv add pdfplumber"
        )

    results: list[dict[str, Any]] = []
    with pdfplumber.open(path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            results.append(
                {
                    "page_number": page_num,
                    "text": text,
                    "char_count": len(text),
                }
            )
    logger.info("PDF extraction complete (Tier 1)", extra={"page_count": len(results)})
    return results


def get_raw_extraction(run_id: str) -> dict[str, Any]:
    """Retrieve raw extraction data for a given run_id.

    Returns raw pages and tokens without canonical interpretation.

    Args:
        run_id: UUID string of the extraction run.

    Returns:
        Dict with run_id, pages (list), and page_count.

    Raises:
        ValueError: If run_id is not a valid UUID.
        SqlExecutionError: If no raw extraction pages found.
    """
    try:
        parsed_id = uuid_mod.UUID(run_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError(
            f"run_id must be a valid UUID string, got: {run_id!r}"
        ) from exc

    logger.info(
        "Retrieving raw extraction data (Tier 1)", extra={"run_id": str(parsed_id)}
    )

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT raw_page_id, run_id, page_number, page_text FROM raw_pages "
                "WHERE run_id = %s ORDER BY page_number ASC;",
                (str(parsed_id),),
            )
            page_rows = cur.fetchall()
            if not page_rows:
                raise SqlExecutionError(
                    f"No raw extraction pages found for run_id: {run_id}"
                )

            pages: list[dict[str, Any]] = []
            for page_row in page_rows:
                page_id = str(page_row[0])
                page_number = int(page_row[2])
                page_text = str(page_row[3])
                cur.execute(
                    "SELECT raw_token_id, raw_page_id, token_kind, token_text, "
                    "x0_mp, y0_mp, x1_mp, y1_mp, table_index, row_index, col_index "
                    "FROM raw_tokens WHERE raw_page_id = %s;",
                    (page_id,),
                )
                token_rows = cur.fetchall()
                tokens = [
                    {
                        "raw_token_id": str(t[0]),
                        "token_kind": t[2],
                        "token_text": str(t[3]),
                        "x0_mp": int(t[4]),
                        "y0_mp": int(t[5]),
                        "x1_mp": int(t[6]),
                        "y1_mp": int(t[7]),
                        "table_index": t[8],
                        "row_index": t[9],
                        "col_index": t[10],
                    }
                    for t in token_rows
                ]
                pages.append(
                    {
                        "raw_page_id": page_id,
                        "page_number": page_number,
                        "page_text": page_text,
                        "token_count": len(tokens),
                        "tokens": tokens,
                    }
                )
            return {"run_id": str(parsed_id), "pages": pages, "page_count": len(pages)}
    finally:
        conn.close()
