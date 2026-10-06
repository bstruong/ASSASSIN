"""Tests for app.mcp.local_tools (Tier 1 MCP Tools)."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.mcp.local_tools import (
    SqlExecutionError,
    _validate_parameters,
    _validate_sql_statement,
    execute_raw_sql,
    get_raw_extraction,
    read_raw_pdf,
)

# ── _validate_sql_statement ──────────────────────────────────────────


class TestValidateSqlStatement:
    def test_empty_string_raises(self):
        with pytest.raises(SqlExecutionError, match="must not be empty"):
            _validate_sql_statement("")

    def test_whitespace_only_raises(self):
        with pytest.raises(SqlExecutionError, match="must not be empty"):
            _validate_sql_statement("   \n  ")

    def test_allowed_select_prefix(self):
        result = _validate_sql_statement("SELECT * FROM accounts;")
        assert "SELECT" in result

    def test_allowed_insert_prefix(self):
        result = _validate_sql_statement("INSERT INTO accounts VALUES (1);")
        assert "INSERT" in result

    def test_disallowed_prefix_raises(self):
        with pytest.raises(SqlExecutionError, match="must start with"):
            _validate_sql_statement("EXEC some_proc();")

    def test_disallowed_function_raises(self):
        with pytest.raises(SqlExecutionError, match="must start with"):
            _validate_sql_statement("CALL some_proc();")

    def test_injection_comment_pattern_raises(self):
        with pytest.raises(SqlExecutionError, match="injection"):
            _validate_sql_statement("SELECT * FROM accounts; -- malicious")

    def test_injection_drop_pattern_raises(self):
        with pytest.raises(SqlExecutionError, match="injection"):
            _validate_sql_statement("SELECT * FROM accounts; DROP TABLE users;")

    def test_injection_or1eq1_pattern_raises(self):
        with pytest.raises(SqlExecutionError, match="injection"):
            _validate_sql_statement("SELECT * FROM accounts WHERE 1=1 OR 1 = 1")

    def test_valid_parameterized_select(self):
        sql = "SELECT * FROM accounts WHERE account_mask = %s;"
        result = _validate_sql_statement(sql)
        assert result == sql


# ── _validate_parameters ─────────────────────────────────────────────


class TestValidateParameters:
    def test_none_returns_empty_tuple(self):
        assert _validate_parameters(None) == ()

    def test_empty_list_returns_empty(self):
        assert len(_validate_parameters([])) == 0

    def test_valid_string_params(self):
        result = _validate_parameters(["hello", "world"])
        assert len(result) == 2
        assert result[0] == "hello"
        assert result[1] == "world"

    def test_valid_int_params(self):
        result = _validate_parameters([1, 42, 0])
        assert len(result) == 3
        assert result[0] == 1
        assert result[1] == 42
        assert result[2] == 0

    def test_valid_mixed_params(self):
        result = _validate_parameters([1, "text", None, True, 3.14])
        assert len(result) == 5

    def test_tuple_params(self):
        result = _validate_parameters((1, 2, 3))
        assert result == (1, 2, 3)

    def test_non_sequence_raises(self):
        with pytest.raises(SqlExecutionError, match="must be a list"):
            _validate_parameters("not a sequence")

    def test_dict_raises(self):
        with pytest.raises(SqlExecutionError, match="must be a list"):
            _validate_parameters({"key": "value"})

    def test_unsupported_type_raises(self):
        with pytest.raises(SqlExecutionError, match="unsupported type"):
            _validate_parameters([object()])

    def test_injection_in_parameter_raises(self):
        with pytest.raises(SqlExecutionError, match="injection"):
            _validate_parameters(["SELECT * FROM accounts; DROP TABLE x"])


# ── execute_raw_sql ──────────────────────────────────────────────────


class TestExecuteRawSql:
    @pytest.fixture()
    def mock_conn(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.description = [MagicMock(name="col1"), MagicMock(name="col2")]
        cur.fetchall.return_value = [(1, "a"), (2, "b")]
        cur.rowcount = 2
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return conn

    def test_select_returns_rows_as_dicts(self, mock_conn, postgres_url):
        # Use real DB for integration test
        result = execute_raw_sql(
            "SELECT 1 AS col1, 'test' AS col2;",
            database_url=postgres_url,
        )
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["col1"] == 1
        assert result[0]["col2"] == "test"

    def test_parameterized_query(self, mock_conn, postgres_url):
        result = execute_raw_sql(
            "SELECT %s AS val;",
            parameters=[42],
            database_url=postgres_url,
        )
        assert result[0]["val"] == 42

    def test_invalid_sql_raises(self, postgres_url):
        with pytest.raises(SqlExecutionError, match="must start with"):
            execute_raw_sql("EXEC some_proc();", database_url=postgres_url)

    def test_empty_sql_raises(self, postgres_url):
        with pytest.raises(SqlExecutionError, match="must not be empty"):
            execute_raw_sql("", database_url=postgres_url)

    def test_injection_in_sql_raises(self, postgres_url):
        with pytest.raises(SqlExecutionError, match="injection"):
            execute_raw_sql("SELECT 1; DROP TABLE foo;", database_url=postgres_url)

    def test_injection_in_parameter_raises(self, postgres_url):
        with pytest.raises(SqlExecutionError, match="injection"):
            execute_raw_sql(
                "SELECT %s;",
                parameters=["foo; DROP TABLE bar"],
                database_url=postgres_url,
            )

    def test_invalid_parameter_type_raises(self, postgres_url):
        with pytest.raises(SqlExecutionError, match="unsupported type"):
            execute_raw_sql(
                "SELECT %s;",
                parameters=[object()],
                database_url=postgres_url,
            )


# ── read_raw_pdf ─────────────────────────────────────────────────────


class TestReadRawPdf:
    def test_nonexistent_file_raises(self):
        with pytest.raises(FileNotFoundError, match="not found"):
            read_raw_pdf("/nonexistent/path/file.pdf")

    def test_non_pdf_extension_raises(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("hello")
        with pytest.raises(ValueError, match="not a PDF"):
            read_raw_pdf(str(f))

    def test_invalid_pdf_header_raises(self, tmp_path):
        f = tmp_path / "file.pdf"
        f.write_text("not a real pdf")
        with pytest.raises(ValueError, match="not appear to be a valid PDF"):
            read_raw_pdf(str(f))

    def test_valid_pdf_returns_pages(self, tmp_path):
        # Create a minimal valid PDF
        pdf_content = b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        pdf_content += b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        pdf_content += (
            b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>endobj\n"
        )
        pdf_content += (
            b"endobj\nxref\n0 4\ntrailer<</Size 4/Root 1 0 R>>\nstartxref\n0\n%%EOF"
        )
        f = tmp_path / "sample.pdf"
        f.write_bytes(pdf_content)

        with patch("pdfplumber.open") as mock_open:
            mock_page = MagicMock()
            mock_page.extract_text.return_value = "Hello from PDF"
            mock_pdf = MagicMock()
            mock_pdf.pages = [mock_page]
            mock_open.return_value.__enter__ = MagicMock(return_value=mock_pdf)
            mock_open.return_value.__exit__ = MagicMock(return_value=False)

            result = read_raw_pdf(str(f))
            assert isinstance(result, list)
            assert len(result) == 1
            assert result[0]["page_number"] == 1
            assert result[0]["text"] == "Hello from PDF"
            assert result[0]["char_count"] == 14

    def test_pdf_import_error(self, tmp_path):
        pdf_content = b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        pdf_content += b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        pdf_content += (
            b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>endobj\n"
        )
        pdf_content += (
            b"endobj\nxref\n0 4\ntrailer<</Size 4/Root 1 0 R>>\nstartxref\n0\n%%EOF"
        )
        f = tmp_path / "sample.pdf"
        f.write_bytes(pdf_content)

        with (
            patch("pdfplumber.open", side_effect=ImportError("no pdfplumber")),
            pytest.raises(ImportError, match="pdfplumber"),
        ):
            read_raw_pdf(str(f))


# ── get_raw_extraction ───────────────────────────────────────────────


class TestGetRawExtraction:
    def test_invalid_uuid_raises(self):
        with pytest.raises(ValueError, match="must be a valid UUID"):
            get_raw_extraction("not-a-uuid")

    def test_invalid_uuid_format_raises(self):
        with pytest.raises(ValueError, match="must be a valid UUID"):
            get_raw_extraction("12345")

    def test_nonexistent_run_raises(self, postgres_url):
        fake_uuid = str(uuid.uuid4())
        with pytest.raises(SqlExecutionError, match="No raw extraction pages found"):
            get_raw_extraction(fake_uuid)

    def test_run_with_pages(self, postgres_url, db_conn):
        # Insert a fake raw payload first (FK requirement)
        payload_id = uuid.uuid4()
        db_conn.execute(
            "INSERT INTO raw_payloads (raw_payload_id, content_sha256, byte_length, original_basename, ingested_at) "
            "VALUES (%s, %s, %s, %s, NOW());",
            (str(payload_id), str(uuid.uuid4()).replace("-", "") * 2, 100, "test.pdf"),
        )

        # Insert a fake raw extraction run with pages and tokens
        run_id = uuid.uuid4()
        page_id = uuid.uuid4()
        token_id = uuid.uuid4()

        db_conn.execute(
            "INSERT INTO extraction_runs (run_id, raw_payload_id, adapter_id, adapter_version, started_at, status) "
            "VALUES (%s, %s, %s, %s, NOW(), 'raw_stored');",
            (str(run_id), str(payload_id), "test", "1.0"),
        )
        db_conn.execute(
            "INSERT INTO raw_pages (raw_page_id, run_id, page_number, page_text) "
            "VALUES (%s, %s, %s, %s);",
            (str(page_id), str(run_id), 1, "Test page text"),
        )
        db_conn.execute(
            "INSERT INTO raw_tokens (raw_token_id, raw_page_id, token_kind, token_text, x0_mp, y0_mp, x1_mp, y1_mp) "
            "VALUES (%s, %s, %s, %s, 0, 0, 100, 20);",
            (str(token_id), str(page_id), "word", "test"),
        )
        db_conn.commit()

        result = get_raw_extraction(str(run_id))
        assert result["run_id"] == str(run_id)
        assert result["page_count"] == 1
        assert len(result["pages"]) == 1
        page = result["pages"][0]
        assert page["page_number"] == 1
        assert page["page_text"] == "Test page text"
        assert page["token_count"] == 1
        assert page["tokens"][0]["token_kind"] == "word"
        assert page["tokens"][0]["token_text"] == "test"
