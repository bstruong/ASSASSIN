"""Unit tests for the fail-loud extract core under Step 2."""

from __future__ import annotations

import pytest

from app.extraction.core import (
    parse_currency_to_cents,
    validate_extraction_against_schemas,
    validate_table_against_schema,
)
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
)
from app.models.schema import TableSchema


class TestSchemaValidation:
    """Tests asserting fail-loud behavior on schema drift and missing markers."""

    def test_missing_mandatory_marker_raises(self) -> None:
        payload = RawPayload(
            content_sha256="0" * 64,
            byte_length=100,
            original_basename="stmt.pdf",
        )
        run = ExtractionRun(
            raw_payload_id=payload.raw_payload_id,
            adapter_id="test_adapter",
            adapter_version="1.0.0",
            status=RunStatus.EXTRACTED,
        )
        page = RawPage(
            run_id=run.run_id,
            page_number=1,
            page_text="This page contains only welcome text.",
            tokens=[],
        )
        extraction = RawExtraction(payload=payload, run=run, pages=[page])

        schema = TableSchema(
            name="transactions",
            headers=("date", "description", "amount"),
            section_markers=("Activity Summary", "Payment Due Date"),
        )

        with pytest.raises(MissingSectionError, match="Mandatory section marker"):
            validate_extraction_against_schemas(extraction, [schema])

    def test_present_mandatory_marker_passes(self) -> None:
        payload = RawPayload(
            content_sha256="0" * 64,
            byte_length=100,
            original_basename="stmt.pdf",
        )
        run = ExtractionRun(
            raw_payload_id=payload.raw_payload_id,
            adapter_id="test_adapter",
            adapter_version="1.0.0",
            status=RunStatus.EXTRACTED,
        )
        page = RawPage(
            run_id=run.run_id,
            page_number=1,
            page_text="Statement Account Activity Summary and Details",
            tokens=[],
        )
        extraction = RawExtraction(payload=payload, run=run, pages=[page])

        schema = TableSchema(
            name="transactions",
            headers=("date", "description", "amount"),
            section_markers=("Activity Summary",),
        )

        validate_extraction_against_schemas(extraction, [schema])

    def test_table_extra_column_raises_schema_drift(self) -> None:
        schema = TableSchema(
            name="transactions",
            headers=("date", "description", "amount"),
        )
        headers = ["date", "description", "category", "amount"]
        rows = [["01/01/2025", "Store", "Shopping", "$10.00"]]

        with pytest.raises(SchemaDriftError, match="Header drift"):
            validate_table_against_schema(headers, rows, schema)

    def test_table_missing_column_raises_schema_drift(self) -> None:
        schema = TableSchema(
            name="transactions",
            headers=("date", "description", "amount"),
        )
        headers = ["date", "description"]
        rows = [["01/01/2025", "Store"]]

        with pytest.raises(SchemaDriftError, match="Header drift"):
            validate_table_against_schema(headers, rows, schema)

    def test_table_ragged_row_raises_schema_drift(self) -> None:
        schema = TableSchema(
            name="transactions",
            headers=("date", "description", "amount"),
        )
        headers = ["date", "description", "amount"]
        rows = [
            ["01/01/2025", "Store", "$10.00"],
            ["01/02/2025", "Broken"],  # Missing amount column
        ]

        with pytest.raises(SchemaDriftError, match="Ragged row in table"):
            validate_table_against_schema(headers, rows, schema)

    def test_exact_headers_and_uniform_rows_pass(self) -> None:
        schema = TableSchema(
            name="transactions",
            headers=("date", "description", "amount"),
        )
        headers = ["Date", "Description", "Amount"]
        rows = [
            ["01/01/2025", "Store A", "$10.00"],
            ["01/02/2025", "Store B", "$20.00"],
        ]

        validate_table_against_schema(headers, rows, schema)


class TestParseCurrencyToCents:
    """parse_currency_to_cents strictly parses 2 decimal digits into integer cents."""

    def test_standard_positive_amounts(self) -> None:
        assert parse_currency_to_cents("10.00") == 1000
        assert parse_currency_to_cents("$10.50") == 1050
        assert parse_currency_to_cents("$1,234.56") == 123456

    def test_standard_negative_amounts(self) -> None:
        assert parse_currency_to_cents("-10.00") == -1000
        assert parse_currency_to_cents("-$10.50") == -1050
        assert parse_currency_to_cents("$-10.50") == -1050
        assert parse_currency_to_cents("($10.50)") == -1050
        assert parse_currency_to_cents("(10.50)") == -1050

    def test_rejects_missing_cents(self) -> None:
        with pytest.raises(TokenError, match="exactly 2 decimal digits"):
            parse_currency_to_cents("10")
        with pytest.raises(TokenError, match="exactly 2 decimal digits"):
            parse_currency_to_cents("$500")

    def test_rejects_single_digit_cent(self) -> None:
        with pytest.raises(TokenError, match="exactly 2 digits"):
            parse_currency_to_cents("10.5")

    def test_rejects_three_decimal_places(self) -> None:
        with pytest.raises(TokenError, match="exactly 2 digits"):
            parse_currency_to_cents("10.555")

    def test_rejects_zero_amount(self) -> None:
        with pytest.raises(TokenError, match="Zero amount is not permitted"):
            parse_currency_to_cents("0.00")
        with pytest.raises(TokenError, match="Zero amount is not permitted"):
            parse_currency_to_cents("$0.00")
        with pytest.raises(TokenError, match="Zero amount is not permitted"):
            parse_currency_to_cents("-0.00")

    def test_rejects_non_numeric_characters(self) -> None:
        with pytest.raises(TokenError):
            parse_currency_to_cents("abc.de")
        with pytest.raises(TokenError):
            parse_currency_to_cents("$12.ab")

    def test_rejects_invalid_types_and_empty(self) -> None:
        with pytest.raises(TokenError, match="non-empty string"):
            parse_currency_to_cents("")
        with pytest.raises(TokenError, match="non-empty string"):
            parse_currency_to_cents(None)  # type: ignore[arg-type]
        with pytest.raises(TokenError, match="non-empty string"):
            parse_currency_to_cents(123)  # type: ignore[arg-type]

    def test_currency_edge_cases(self) -> None:
        assert parse_currency_to_cents("$+12.34") == 1234
        assert parse_currency_to_cents("+12.34") == 1234
        assert parse_currency_to_cents("$0.00", allow_zero=True) == 0

    def test_rejects_empty_dollar_part(self) -> None:
        with pytest.raises(TokenError, match="non-digit or empty dollar component"):
            parse_currency_to_cents(".50")
        with pytest.raises(TokenError, match="non-digit or empty dollar component"):
            parse_currency_to_cents("$.50")

    def test_rejects_multiple_decimal_points(self) -> None:
        with pytest.raises(TokenError, match="multiple decimal points"):
            parse_currency_to_cents("1.2.3")


class TestExtractPdfToRaw:
    """extract_pdf_to_raw loads pages and geometry into RawExtraction."""

    def test_missing_file_raises_filenotfound(self, tmp_path) -> None:
        from app.extraction.core import extract_pdf_to_raw

        with pytest.raises(FileNotFoundError):
            extract_pdf_to_raw(tmp_path / "nonexistent.pdf", "test", "1.0.0")

    def test_empty_file_raises_valueerror(self, tmp_path) -> None:
        from app.extraction.core import extract_pdf_to_raw

        empty_file = tmp_path / "empty.pdf"
        empty_file.write_bytes(b"")
        with pytest.raises(ValueError, match="empty"):
            extract_pdf_to_raw(empty_file, "test", "1.0.0")

    def test_empty_pdf_pages_raises_valueerror(self, tmp_path) -> None:
        from unittest.mock import MagicMock, patch

        from app.extraction.core import extract_pdf_to_raw

        dummy_file = tmp_path / "nopages.pdf"
        dummy_file.write_bytes(b"%PDF-1.4 dummy")
        mock_pdf = MagicMock()
        mock_pdf.__enter__.return_value = mock_pdf
        mock_pdf.pages = []
        with (
            patch("pdfplumber.open", return_value=mock_pdf),
            pytest.raises(ValueError, match="PDF has no pages"),
        ):
            extract_pdf_to_raw(dummy_file, "adapter", "1.0")

    def test_synthetic_pdf_extraction(self, tmp_path) -> None:
        from reportlab.pdfgen import canvas

        from app.extraction.core import extract_pdf_to_raw

        pdf_path = tmp_path / "synthetic.pdf"
        c = canvas.Canvas(str(pdf_path))
        c.drawString(100, 750, "STANDARD BANK STATEMENT")
        # Draw table lines
        c.rect(100, 600, 300, 100)
        c.line(100, 650, 400, 650)
        c.line(250, 600, 250, 700)
        c.save()

        extraction = extract_pdf_to_raw(pdf_path, "test_adapter", "1.0.0")
        assert extraction.payload.original_basename == "synthetic.pdf"
        assert len(extraction.payload.content_sha256) == 64
        assert extraction.payload.byte_length > 0
        assert extraction.run.status == RunStatus.EXTRACTED
        assert len(extraction.pages) == 1
        assert extraction.pages[0].page_number == 1
        assert len(extraction.pages[0].tokens) > 0
        assert any(t.token_kind == "word" for t in extraction.pages[0].tokens)

    def test_mocked_pdf_words_and_tables_extraction(self, tmp_path) -> None:
        from unittest.mock import MagicMock, patch

        from app.extraction.core import extract_pdf_to_raw

        dummy_file = tmp_path / "sample.pdf"
        dummy_file.write_bytes(b"%PDF-1.4 dummy content")

        mock_word1 = {
            "text": "Deposit",
            "x0": 10.5,
            "top": 20.25,
            "x1": 50.75,
            "bottom": 30.1,
        }
        mock_word2 = {
            "text": "ZeroWidth",
            "x0": -5.0,
            "top": -2.0,
            "x1": -10.0,  # x1 < x0 -> clamps to x0_mp + 1
            "bottom": -5.0,  # bottom < top -> clamps to y0_mp + 1
        }

        mock_table = MagicMock()
        mock_row1 = MagicMock()
        mock_row1.cells = [(10.0, 20.0, 100.0, 40.0), None]
        mock_row2 = MagicMock()
        mock_row2.cells = [(-1.0, -2.0, 0.0, 0.0)]
        mock_table.rows = [mock_row1, mock_row2]

        mock_page = MagicMock()
        mock_page.extract_text.return_value = "Page 1 Content Deposit"
        mock_page.extract_words.return_value = [mock_word1, mock_word2]
        mock_page.find_tables.return_value = [mock_table]

        mock_pdf = MagicMock()
        mock_pdf.__enter__.return_value = mock_pdf
        mock_pdf.pages = [mock_page]

        with patch("pdfplumber.open", return_value=mock_pdf):
            extraction = extract_pdf_to_raw(dummy_file, "mock_adapter", "2.1.0")

        assert extraction.run.adapter_id == "mock_adapter"
        assert extraction.run.adapter_version == "2.1.0"
        assert extraction.run.status == RunStatus.EXTRACTED
        assert len(extraction.pages) == 1
        page = extraction.pages[0]
        assert page.page_number == 1
        assert page.page_text == "Page 1 Content Deposit"
        # 2 words + 2 valid cells (None skipped) = 4 tokens
        assert len(page.tokens) == 4

        w1 = page.tokens[0]
        assert w1.token_kind == "word"
        assert w1.token_text == "Deposit"
        assert w1.x0_mp == 10500
        assert w1.y0_mp == 20250
        assert w1.x1_mp == 50750
        assert w1.y1_mp == 30100

        w2 = page.tokens[1]
        assert w2.token_kind == "word"
        assert w2.token_text == "ZeroWidth"
        assert w2.x0_mp == 0
        assert w2.y0_mp == 0
        assert w2.x1_mp == 1
        assert w2.y1_mp == 1

        c1 = page.tokens[2]
        assert c1.token_kind == "cell"
        assert c1.token_text == ""
        assert c1.x0_mp == 10000
        assert c1.y0_mp == 20000
        assert c1.x1_mp == 100000
        assert c1.y1_mp == 40000
        assert c1.table_index == 0
        assert c1.row_index == 0
        assert c1.col_index == 0

        c2 = page.tokens[3]
        assert c2.token_kind == "cell"
        assert c2.x0_mp == 0
        assert c2.y0_mp == 0
        assert c2.x1_mp == 1
        assert c2.y1_mp == 1
        assert c2.table_index == 0
        assert c2.row_index == 1
        assert c2.col_index == 0
