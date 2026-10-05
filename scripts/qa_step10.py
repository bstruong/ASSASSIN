"""QA Verification Script for Step 10: Tiered MCP Harness.

Run with: uv run python scripts/qa_step10.py
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("qa_step10")

PASS, FAIL = "[PASS]", "[FAIL]"


def check(desc: str, cond: bool) -> None:
    status = PASS if cond else FAIL
    print(f"  {status} {desc}")
    if not cond:
        logger.error("Check failed: %s", desc)


def main() -> int:
    print("=" * 60)
    print("  Step 10: Tiered MCP Harness - QA Verification")
    print("=" * 60)
    print()

    postgres_url = (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or "postgresql://postgres:assassin@localhost:54329/assassin_test"
    )
    tmp_path = Path("/tmp/qa_step10")
    tmp_path.mkdir(exist_ok=True)

    # ── Tier 1: SQL Validation ─────────────────────────────────────
    print("TIER 1: SQL Statement Validation")
    from app.mcp.local_tools import SqlExecutionError, _validate_sql_statement

    check("SELECT accepted", _validate_sql_statement("SELECT 1;") is not None)
    check(
        "INSERT accepted",
        _validate_sql_statement("INSERT INTO x VALUES (1);") is not None,
    )
    try:
        _validate_sql_statement("EXEC proc();")
        check("EXEC rejected", False)
    except SqlExecutionError:
        check("EXEC rejected", True)
    try:
        _validate_sql_statement("SELECT 1; -- comment")
        check("Comment injection rejected", False)
    except SqlExecutionError:
        check("Comment injection rejected", True)
    try:
        _validate_sql_statement("SELECT 1; DROP TABLE x;")
        check("DROP after semicolon rejected", False)
    except SqlExecutionError:
        check("DROP after semicolon rejected", True)
    try:
        _validate_sql_statement("")
        check("Empty SQL rejected", False)
    except SqlExecutionError:
        check("Empty SQL rejected", True)

    # ── Tier 1: Parameter Validation ───────────────────────────────
    print("\nTIER 1: Parameter Validation")
    from app.mcp.local_tools import _validate_parameters

    check("None params accepted", _validate_parameters(None) == ())
    check("String list accepted", len(_validate_parameters(["a", "b"])) == 2)
    check("Mixed types accepted", len(_validate_parameters([1, "x", None])) == 3)
    try:
        _validate_parameters("not a list")
        check("String params rejected", False)
    except SqlExecutionError:
        check("String params rejected", True)
    try:
        _validate_parameters([object()])
        check("Object type rejected", False)
    except SqlExecutionError:
        check("Object type rejected", True)

    # ── Tier 1: Raw SQL Execution ──────────────────────────────────
    print("\nTIER 1: Raw SQL Execution")
    from app.mcp.local_tools import execute_raw_sql

    result = execute_raw_sql("SELECT 42 AS answer;", database_url=postgres_url)
    check("SELECT returns row", isinstance(result, list) and len(result) == 1)
    check("SELECT value correct", result[0]["answer"] == 42)

    result = execute_raw_sql(
        "SELECT %s AS val;", parameters=[100], database_url=postgres_url
    )
    check("Parameterized SELECT works", result[0]["val"] == 100)

    result = execute_raw_sql(
        "TRUNCATE TABLE raw_payloads RESTART IDENTITY CASCADE;",
        database_url=postgres_url,
    )
    check("TRUNCATE returns status", result[0]["status"] == "success")

    try:
        execute_raw_sql("EXEC proc();", database_url=postgres_url)
        check("Disallowed SQL raises", False)
    except SqlExecutionError:
        check("Disallowed SQL raises", True)

    # ── Tier 1: PDF Extraction ─────────────────────────────────────
    print("\nTIER 1: PDF Text Extraction")
    from unittest.mock import MagicMock, patch

    from app.mcp.local_tools import read_raw_pdf

    pdf_content = b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    pdf_content += b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    pdf_content += b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>endobj\n"
    pdf_content += (
        b"endobj\nxref\n0 4\ntrailer<</Size 4/Root 1 0 R>>\nstartxref\n0\n%%EOF"
    )
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(pdf_content)

    with patch("pdfplumber.open") as mock_open:
        mock_page = MagicMock()
        mock_page.extract_text.return_value = "Test PDF"
        mock_pdf = MagicMock()
        mock_pdf.pages = [mock_page]
        mock_open.return_value.__enter__ = MagicMock(return_value=mock_pdf)
        mock_open.return_value.__exit__ = MagicMock(return_value=False)
        result = read_raw_pdf(str(pdf_path))
        check("PDF returns list", isinstance(result, list))
        check("PDF page_number=1", result[0]["page_number"] == 1)
        check("PDF text matches", result[0]["text"] == "Test PDF")

    try:
        read_raw_pdf("/nonexistent/file.pdf")
        check("Nonexistent file raises", False)
    except FileNotFoundError:
        check("Nonexistent file raises", True)

    txt_path = tmp_path / "file.txt"
    txt_path.write_text("hello")
    try:
        read_raw_pdf(str(txt_path))
        check("Non-PDF raises", False)
    except ValueError:
        check("Non-PDF raises", True)

    # ── Tier 1: Raw Extraction ─────────────────────────────────────
    print("\nTIER 1: Raw Extraction Retrieval")
    from app.mcp.local_tools import get_raw_extraction

    try:
        get_raw_extraction("not-a-uuid")
        check("Invalid UUID raises", False)
    except ValueError:
        check("Invalid UUID raises", True)

    try:
        get_raw_extraction(str(uuid.uuid4()))
        check("Nonexistent run raises", False)
    except SqlExecutionError:
        check("Nonexistent run raises", True)

    # ── Tier 2: Cloud SQL Validation ───────────────────────────────
    print("\nTIER 2: Cloud SQL Validation")
    from app.mcp.cloud_tools import CloudToolError, _validate_cloud_sql

    check("SELECT accepted", _validate_cloud_sql("SELECT 1;") is not None)
    check(
        "SELECT COUNT accepted",
        _validate_cloud_sql("SELECT COUNT(*) FROM transactions;") is not None,
    )
    check(
        "SELECT SUM accepted",
        _validate_cloud_sql("SELECT SUM(x) FROM transactions;") is not None,
    )
    check(
        "GROUP BY accepted",
        _validate_cloud_sql(
            "SELECT transaction_category, COUNT(*) FROM transactions GROUP BY transaction_category;"
        )
        is not None,
    )

    for stmt in [
        "INSERT INTO x VALUES (1);",
        "UPDATE x SET y=1;",
        "DELETE FROM x;",
        "DROP TABLE x;",
    ]:
        try:
            _validate_cloud_sql(stmt)
            check(f"{stmt.split()[0]} rejected", False)
        except CloudToolError:
            check(f"{stmt.split()[0]} rejected", True)

    try:
        _validate_cloud_sql("SELECT * FROM raw_pages;")
        check("raw_pages table rejected", False)
    except CloudToolError:
        check("raw_pages table rejected", True)

    try:
        _validate_cloud_sql("SELECT 1; -- comment")
        check("Comment injection rejected", False)
    except CloudToolError:
        check("Comment injection rejected", True)

    # ── Tier 2: Cloud Query Execution ──────────────────────────────
    print("\nTIER 2: Cloud Query Execution")
    from app.mcp.cloud_tools import execute_cloud_query

    result = execute_cloud_query("SELECT 1 AS val;", database_url=postgres_url)
    check("Cloud SELECT works", result[0]["val"] == 1)

    result = execute_cloud_query(
        "SELECT COUNT(*) AS cnt FROM extraction_runs;",
        database_url=postgres_url,
    )
    check("Cloud COUNT works", "cnt" in result[0])

    try:
        execute_cloud_query("INSERT INTO x VALUES (1);", database_url=postgres_url)
        check("INSERT denied in cloud", False)
    except CloudToolError:
        check("INSERT denied in cloud", True)

    try:
        execute_cloud_query("SELECT * FROM raw_pages;", database_url=postgres_url)
        check("raw_pages denied", False)
    except CloudToolError:
        check("raw_pages denied", True)

    # ── Tier 2: Schema Introspection ───────────────────────────────
    print("\nTIER 2: Schema Introspection")
    from app.mcp.cloud_tools import get_schema_info

    result = get_schema_info(database_url=postgres_url)
    check("Schema returns tables", "tables" in result)
    check("accounts in schema", "accounts" in result["tables"])
    check("transactions in schema", "transactions" in result["tables"])

    result = get_schema_info(table_name="transactions", database_url=postgres_url)
    check("Specific table returns columns", "columns" in result)
    col_names = [c["name"] for c in result["columns"]]
    check("transaction_id column", "transaction_id" in col_names)
    check("amount_cents column", "amount_cents" in col_names)

    try:
        get_schema_info(table_name="raw_pages")
        check("raw_pages introspection denied", False)
    except CloudToolError:
        check("raw_pages introspection denied", True)

    # ── Tier 2: Financial Summary ──────────────────────────────────
    print("\nTIER 2: Financial Summary")
    from app.mcp.cloud_tools import get_financial_summary

    result = get_financial_summary(
        group_by="transaction_category", database_url=postgres_url
    )
    check(
        "Summary group_by_column", result["group_by_column"] == "transaction_category"
    )
    check("Summary has totals", "totals" in result)
    check("Summary has per_group", "per_group" in result)
    check("Totals has transaction count", "total_transactions" in result["totals"])
    check(
        "Totals has deposit/purchase/withdrawal",
        all(
            k in result["totals"]
            for k in [
                "total_deposits_cents",
                "total_purchases_cents",
                "total_withdrawals_cents",
            ]
        ),
    )

    try:
        get_financial_summary(group_by="nonexistent_column")
        check("Invalid group_by rejected", False)
    except CloudToolError:
        check("Invalid group_by rejected", True)

    try:
        get_financial_summary(group_by="raw_page_id")
        check("Disallowed group_by rejected", False)
    except CloudToolError:
        check("Disallowed group_by rejected", True)

    print()
    print("=" * 60)
    print("  All QA checks completed.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
