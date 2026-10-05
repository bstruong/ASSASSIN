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

    def test_synthetic_pdf_extraction(self, tmp_path) -> None:
        import pypdfium2 as pdfium

        from app.extraction.core import extract_pdf_to_raw

        pdf_path = tmp_path / "synthetic.pdf"
        doc = pdfium.PdfDocument.new()
        doc.new_page(width=595, height=842)
        doc.save(str(pdf_path))

        extraction = extract_pdf_to_raw(pdf_path, "test_adapter", "1.0.0")
        assert extraction.payload.original_basename == "synthetic.pdf"
        assert len(extraction.payload.content_sha256) == 64
        assert extraction.payload.byte_length > 0
        assert extraction.run.status == RunStatus.EXTRACTED
        assert len(extraction.pages) == 1
        assert extraction.pages[0].page_number == 1
