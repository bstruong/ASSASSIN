"""Vanguard brokerage adapter: cash identity, portfolio bridge, and holdings."""

from __future__ import annotations

import datetime
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from reportlab.pdfgen import canvas

from app.adapters.schwab import SchwabAdapter
from app.adapters.vanguard import VanguardAdapter
from app.models.enums import AccountDomain, AccountType, RunStatus, TransactionCategory
from app.models.exceptions import (
    AmbiguousAccountsError,
    InvariantError,
    MissingSectionError,
    SchemaDriftError,
    TokenError,
)
from app.models.raw import ExtractionRun, RawExtraction, RawPage, RawPayload

CASH_ONLY = """VANGUARD BROKERAGE SERVICES
Vanguard Account: *0000
Registration: Brokerage Cash
Period: 2025-08-01 through 2025-08-31

CASH SUMMARY
Opening Cash: $1,000.00
Closing Cash: $1,100.00

ACTIVITY
Date | Activity | Amount
2025-08-15 | SYNTHETIC Dividend | +$100.00
"""

WITH_HOLDINGS = (
    CASH_ONLY
    + """
FUND POSITIONS
Symbol | Description | Shares | Value
SYN | SYNTHETIC Equity | 10 | $1,000.00
BND | SYNTHETIC Bond | 2.5 | $250.50
Positions Total: $1,250.50
"""
)

WITH_PORTFOLIO = """VANGUARD BROKERAGE SERVICES
Vanguard Account: *0000
Registration: Brokerage Cash
Period: 2025-08-01 through 2025-08-31

CASH SUMMARY
Opening Cash: $1,000.00
Closing Cash: $1,100.00

ACTIVITY
Date | Activity | Amount
2025-08-15 | SYNTHETIC Dividend | +$100.00

PORTFOLIO BRIDGE
Opening Portfolio Value: $5,000.00
Transfers In: $100.00
Transfers Out: $0.00
Dividends: $100.00
Realized Gain: $0.00
Unrealized Gain: ($25.00)
Closing Portfolio Value: $5,175.00
"""


def extraction_from(text: str) -> RawExtraction:
    payload = RawPayload(
        content_sha256="a" * 64,
        byte_length=len(text.encode("utf-8")),
        original_basename="vanguard.pdf",
    )
    run = ExtractionRun(
        raw_payload_id=payload.raw_payload_id,
        adapter_id="vanguard_brokerage",
        adapter_version="1.0.0",
        status=RunStatus.EXTRACTED,
    )
    page = RawPage(run_id=run.run_id, page_number=1, page_text=text, tokens=[])
    return RawExtraction(payload=payload, run=run, pages=[page])


def create_pdf(path: Path, text: str) -> Path:
    c = canvas.Canvas(str(path))
    c.drawString(72, 750, text)
    c.save()
    return path


class TestVanguardParse:
    def setup_method(self) -> None:
        self.adapter = VanguardAdapter()

    def test_cash_happy_path(self) -> None:
        account, statement, summary, txns, holdings = self.adapter.parse_canonical(
            extraction_from(CASH_ONLY)
        )
        assert account.institution == "Vanguard"
        assert account.account_mask == "*0000"
        assert account.account_domain == AccountDomain.CUSTODIAL_BROKERAGE
        assert account.account_type == AccountType.BROKERAGE_CASH
        assert statement.opening_balance_cents == 100_000
        assert statement.closing_balance_cents == 110_000
        assert summary.opening_portfolio_cents is None
        assert len(txns) == 1
        assert txns[0].amount_cents == 10_000
        assert txns[0].transaction_category == TransactionCategory.DIVIDEND
        assert txns[0].post_date == datetime.date(2025, 8, 15)
        assert holdings == []

    def test_holdings_nanos_and_cents(self) -> None:
        _account, statement, _summary, _txns, holdings = self.adapter.parse_canonical(
            extraction_from(WITH_HOLDINGS)
        )
        assert len(holdings) == 2
        assert holdings[0].symbol == "SYN"
        assert holdings[0].description == "SYNTHETIC Equity"
        assert holdings[0].quantity_nanos == 10_000_000_000
        assert holdings[0].market_value_cents == 100_000
        assert holdings[1].quantity_nanos == 2_500_000_000
        assert holdings[1].market_value_cents == 25_050
        assert holdings[0].as_of_date == statement.statement_end_date

    def test_portfolio_bridge_keeps_signed_loss(self) -> None:
        _account, _statement, summary, _txns, _holdings = self.adapter.parse_canonical(
            extraction_from(WITH_PORTFOLIO)
        )
        assert summary.opening_portfolio_cents == 500_000
        assert summary.transfers_in_cents == 10_000
        assert summary.transfers_out_cents == 0
        assert summary.income_dividends_cents == 10_000
        assert summary.realized_gains_cents == 0
        assert summary.unrealized_gains_cents == -2_500
        assert summary.closing_portfolio_cents == 517_500

    def test_cash_mismatch_fails(self) -> None:
        text = CASH_ONLY.replace("Closing Cash: $1,100.00", "Closing Cash: $1,100.01")
        with pytest.raises(InvariantError):
            self.adapter.parse_canonical(extraction_from(text))

    def test_holdings_total_mismatch_fails(self) -> None:
        text = WITH_HOLDINGS.replace(
            "Positions Total: $1,250.50", "Positions Total: $1,250.51"
        )
        with pytest.raises(InvariantError, match="Holdings valuation mismatch"):
            self.adapter.parse_canonical(extraction_from(text))

    def test_missing_positions_total_fails(self) -> None:
        text = WITH_HOLDINGS.replace("Positions Total: $1,250.50\n", "")
        with pytest.raises(MissingSectionError, match="Positions Total"):
            self.adapter.parse_canonical(extraction_from(text))

    def test_activity_header_drift_fails(self) -> None:
        text = CASH_ONLY.replace("Date | Activity | Amount", "Date | Memo | Amount")
        with pytest.raises(SchemaDriftError):
            self.adapter.parse_canonical(extraction_from(text))

    def test_unsigned_amount_fails(self) -> None:
        text = CASH_ONLY.replace("+$100.00", "$100.00")
        with pytest.raises(TokenError, match="Ambiguous amount sign"):
            self.adapter.parse_canonical(extraction_from(text))

    def test_negative_transfer_in_is_not_flipped(self) -> None:
        text = WITH_PORTFOLIO.replace(
            "Transfers In: $100.00", "Transfers In: ($100.00)"
        )
        text = text.replace(
            "Closing Portfolio Value: $5,175.00",
            "Closing Portfolio Value: $4,975.00",
        )
        with pytest.raises(InvariantError, match="transfers_in_cents must be >= 0"):
            self.adapter.parse_canonical(extraction_from(text))

    def test_missing_portfolio_term_fails(self) -> None:
        text = WITH_PORTFOLIO.replace("Dividends: $100.00\n", "")
        with pytest.raises(MissingSectionError, match="Dividends"):
            self.adapter.parse_canonical(extraction_from(text))

    def test_two_accounts_fail(self) -> None:
        text = CASH_ONLY.replace(
            "Vanguard Account: *0000",
            "Vanguard Account: *0000\nVanguard Account: *1111",
        )
        with pytest.raises(AmbiguousAccountsError):
            self.adapter.parse_canonical(extraction_from(text))

    def test_schwab_text_is_not_vanguard(self) -> None:
        schwab = Path("tests/fixtures/brokerage_cash_happy.json").read_text()
        with pytest.raises(MissingSectionError):
            self.adapter.parse_canonical(extraction_from(schwab))


class TestVanguardMatches:
    def test_matches_vanguard_pdf_only(self, tmp_path: Path) -> None:
        adapter = VanguardAdapter()
        vanguard = create_pdf(tmp_path / "vanguard.pdf", "VANGUARD BROKERAGE SERVICES")
        schwab = create_pdf(tmp_path / "schwab.pdf", "CHARLES SCHWAB")
        assert adapter.matches(vanguard) is True
        assert adapter.matches(schwab) is False
        assert SchwabAdapter().matches(vanguard) is False

    def test_matches_rejects_missing_corrupt_and_empty(self, tmp_path: Path) -> None:
        adapter = VanguardAdapter()
        with pytest.raises(FileNotFoundError):
            adapter.matches(tmp_path / "missing.pdf")
        corrupt = tmp_path / "corrupt.pdf"
        corrupt.write_bytes(b"NOT A PDF")
        with pytest.raises(ValueError, match="Failed to read PDF"):
            adapter.matches(corrupt)
        empty_doc = tmp_path / "empty.pdf"
        doc = pdfium.PdfDocument.new()
        doc.save(str(empty_doc))
        with pytest.raises(ValueError, match="PDF has no pages"):
            adapter.matches(empty_doc)

    def test_margin_registration(self) -> None:
        text = CASH_ONLY.replace("Brokerage Cash", "Brokerage Margin")
        account, *_rest = VanguardAdapter().parse_canonical(extraction_from(text))
        assert account.account_type == AccountType.BROKERAGE_MARGIN

    def test_missing_registration_period_and_account(self) -> None:
        adapter = VanguardAdapter()
        with pytest.raises(MissingSectionError, match="registration"):
            adapter.parse_canonical(
                extraction_from(CASH_ONLY.replace("Registration: Brokerage Cash\n", ""))
            )
        with pytest.raises(MissingSectionError, match="period"):
            adapter.parse_canonical(
                extraction_from(
                    CASH_ONLY.replace("Period: 2025-08-01 through 2025-08-31\n", "")
                )
            )
        with pytest.raises(MissingSectionError, match="account mask"):
            adapter.parse_canonical(
                extraction_from(CASH_ONLY.replace("Vanguard Account: *0000\n", ""))
            )

    def test_both_registrations_fail(self) -> None:
        text = CASH_ONLY.replace(
            "Registration: Brokerage Cash",
            "Registration: Brokerage Cash\nRegistration: Brokerage Margin",
        )
        with pytest.raises(AmbiguousAccountsError):
            VanguardAdapter().parse_canonical(extraction_from(text))

    def test_negative_transfer_out_is_not_flipped(self) -> None:
        text = WITH_PORTFOLIO.replace("Transfers Out: $0.00", "Transfers Out: ($1.00)")
        with pytest.raises(InvariantError, match="transfers_out_cents must be >= 0"):
            VanguardAdapter().parse_canonical(extraction_from(text))

    def test_blank_activity_section_fails(self) -> None:
        text = CASH_ONLY.split("ACTIVITY", maxsplit=1)[0] + "ACTIVITY\n\n"
        with pytest.raises(MissingSectionError, match="header row"):
            VanguardAdapter().parse_canonical(extraction_from(text))

    def test_activity_word_without_section_fails(self) -> None:
        text = """VANGUARD BROKERAGE SERVICES
Vanguard Account: *0000
Registration: Brokerage Cash
Period: 2025-08-01 through 2025-08-31

CASH SUMMARY
Opening Cash: $1,000.00
Closing Cash: $1,000.00

See the ACTIVITY note.
"""
        with pytest.raises(MissingSectionError, match="ACTIVITY section"):
            VanguardAdapter().parse_canonical(extraction_from(text))

    def test_positions_heading_without_table_fails(self) -> None:
        text = CASH_ONLY + "FUND POSITIONS"
        with pytest.raises(MissingSectionError, match="FUND POSITIONS"):
            VanguardAdapter().parse_canonical(extraction_from(text))

    def test_positions_missing_header_fails(self) -> None:
        text = CASH_ONLY + "FUND POSITIONS\nPositions Total: $0.00\n"
        with pytest.raises(MissingSectionError, match="header row"):
            VanguardAdapter().parse_canonical(extraction_from(text))

    def test_invalid_activity_date_fails(self) -> None:
        text = CASH_ONLY.replace("2025-08-15", "2025-13-01")
        with pytest.raises(TokenError, match="Invalid date"):
            VanguardAdapter().parse_canonical(extraction_from(text))

    def test_activity_categories(self) -> None:
        text = """VANGUARD BROKERAGE SERVICES
Vanguard Account: *0000
Registration: Brokerage Cash
Period: 2025-08-01 through 2025-08-31

CASH SUMMARY
Opening Cash: $1,000.00
Closing Cash: $1,040.00

ACTIVITY
Date | Activity | Amount
2025-08-01 | SYNTHETIC Fee | -$10.00
2025-08-02 | SYNTHETIC Transfer In | +$50.00
2025-08-03 | SYNTHETIC Transfer Out | -$20.00
2025-08-04 | SYNTHETIC Buy | -$30.00
2025-08-05 | SYNTHETIC Sell | +$40.00
2025-08-06 | SYNTHETIC Credit | +$15.00
2025-08-07 | SYNTHETIC Debit | -$5.00
"""
        _account, _statement, _summary, txns, _holdings = (
            VanguardAdapter().parse_canonical(extraction_from(text))
        )
        assert [txn.transaction_category for txn in txns] == [
            TransactionCategory.FEE,
            TransactionCategory.TRANSFER_IN,
            TransactionCategory.TRANSFER_OUT,
            TransactionCategory.TRADE_CASH,
            TransactionCategory.TRADE_CASH,
            TransactionCategory.OTHER_CREDIT,
            TransactionCategory.OTHER_DEBIT,
        ]
