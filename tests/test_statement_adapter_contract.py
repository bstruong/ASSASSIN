"""Contract tests for StatementAdapter defaults that concrete adapters inherit."""

from __future__ import annotations

from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from app.adapters.base import StatementAdapter
from app.models.enums import AccountDomain, AccountType


class _ContractAdapter(StatementAdapter):
    """Minimal concrete adapter that does not override extract or parse."""

    adapter_id = "qa_contract"
    adapter_version = "9.9.9"
    institution_id = "QA Bank"
    account_domain = AccountDomain.DEPOSITORY
    account_types = frozenset({AccountType.CHECKING})

    def matches(self, file_path: Path) -> bool:
        return False


def _write_synthetic_pdf(path: Path) -> Path:
    pdf = canvas.Canvas(str(path))
    pdf.drawString(72, 720, "SYNTHETIC statement")
    pdf.save()
    return path


def test_extract_forwards_identity_and_returns_raw_pages(tmp_path: Path) -> None:
    """extract() must pass the adapter id and version through to raw extraction."""
    pdf_path = _write_synthetic_pdf(tmp_path / "synthetic.pdf")
    extraction = _ContractAdapter().extract(pdf_path)

    assert extraction.run.adapter_id == "qa_contract"
    assert extraction.run.adapter_version == "9.9.9"
    assert extraction.payload.original_basename == "synthetic.pdf"
    assert extraction.payload.byte_length > 0
    assert len(extraction.pages) == 1
    assert extraction.pages[0].page_number == 1


def test_extract_missing_file_fails_loud(tmp_path: Path) -> None:
    missing = tmp_path / "missing.pdf"
    with pytest.raises(FileNotFoundError, match="PDF file not found"):
        _ContractAdapter().extract(missing)


def test_base_parse_rejects_legacy_entry_point(tmp_path: Path) -> None:
    """Adapters that do not implement legacy parse() must fail loudly."""
    with pytest.raises(
        NotImplementedError,
        match=r"^Use parse_canonical or domain parse methods\.$",
    ):
        _ContractAdapter().parse(tmp_path / "synthetic.pdf")
