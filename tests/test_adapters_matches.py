"""Tests for adapter matches() heuristics on disk files."""

from __future__ import annotations

from pathlib import Path

import pypdfium2 as pdfium
import pytest
from reportlab.pdfgen import canvas

from app.adapters.credit_card import StandardCreditCardAdapter
from app.adapters.depository import StandardDepositoryAdapter
from app.adapters.schwab import SchwabAdapter


def create_pdf(path: Path, text: str) -> Path:
    c = canvas.Canvas(str(path))
    c.drawString(100, 750, text)
    c.save()
    return path


def test_standard_depository_matches(tmp_path: Path):
    adapter = StandardDepositoryAdapter()

    # Missing file
    with pytest.raises(FileNotFoundError):
        adapter.matches(tmp_path / "missing.pdf")

    # Corrupt PDF
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"NOT A PDF")
    assert adapter.matches(corrupt) is False

    # Empty pages PDF
    empty_doc = tmp_path / "empty_pages.pdf"
    doc = pdfium.PdfDocument.new()
    doc.save(str(empty_doc))
    assert adapter.matches(empty_doc) is False

    # Valid checking
    checking = create_pdf(tmp_path / "checking.pdf", "STANDARD BANK CHECKING")
    assert adapter.matches(checking) is True

    # Valid savings
    savings = create_pdf(tmp_path / "savings.pdf", "STANDARD BANK SAVINGS")
    assert adapter.matches(savings) is True

    # Non matching
    other = create_pdf(tmp_path / "other.pdf", "SOME OTHER BANK")
    assert adapter.matches(other) is False


def test_standard_credit_card_matches(tmp_path: Path):
    adapter = StandardCreditCardAdapter()

    # Missing file
    with pytest.raises(FileNotFoundError):
        adapter.matches(tmp_path / "missing.pdf")

    # Corrupt PDF
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"NOT A PDF")
    assert adapter.matches(corrupt) is False

    # Empty pages PDF
    empty_doc = tmp_path / "empty_pages.pdf"
    doc = pdfium.PdfDocument.new()
    doc.save(str(empty_doc))
    assert adapter.matches(empty_doc) is False

    # Valid credit card
    card = create_pdf(tmp_path / "card.pdf", "STANDARD CARD CREDIT CARD")
    assert adapter.matches(card) is True

    # Valid card statement
    stmt = create_pdf(tmp_path / "stmt.pdf", "STANDARD CARD CARD STATEMENT")
    assert adapter.matches(stmt) is True

    # Non matching
    other = create_pdf(tmp_path / "other.pdf", "SOME OTHER CARD")
    assert adapter.matches(other) is False


def test_schwab_matches(tmp_path: Path):
    adapter = SchwabAdapter()

    # Missing file
    with pytest.raises(FileNotFoundError):
        adapter.matches(tmp_path / "missing.pdf")

    # Corrupt PDF
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"NOT A PDF")
    with pytest.raises(ValueError, match="Failed to read PDF"):
        adapter.matches(corrupt)

    # Empty pages PDF
    empty_doc = tmp_path / "empty_pages.pdf"
    doc = pdfium.PdfDocument.new()
    doc.save(str(empty_doc))
    with pytest.raises(ValueError, match="PDF has no pages"):
        adapter.matches(empty_doc)

    # Valid schwab
    schwab = create_pdf(tmp_path / "schwab.pdf", "CHARLES SCHWAB")
    assert adapter.matches(schwab) is True

    # Non matching
    other = create_pdf(tmp_path / "other.pdf", "VANGUARD")
    assert adapter.matches(other) is False
