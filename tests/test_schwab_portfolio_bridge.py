"""Schwab cash-primary statements versus a printed portfolio bridge.

Cash-only text must leave portfolio cents unset. A printed bridge requires
every component and reconciles in integer cents. Activity wording must not
invent a bridge.
"""

from __future__ import annotations

import pytest

from app.adapters.schwab import SchwabAdapter
from app.models.enums import RunStatus
from app.models.exceptions import InvariantError, MissingSectionError
from app.models.raw import ExtractionRun, RawExtraction, RawPage, RawPayload

_CASH_ONLY = """CHARLES SCHWAB
Account Number: *0000
Statement Period: 2025-08-01 to 2025-08-31

ACCOUNT SUMMARY
Starting Cash Balance: $1,000.00
Ending Cash Balance: $1,100.00

TRANSACTION ACTIVITY
Date    Description    Amount
2025-08-10    SYNTHETIC Dividend    +$100.00
END OF STATEMENT
"""

_CASH_ACTIVITY_MENTIONS_PORTFOLIO = """CHARLES SCHWAB
Account Number: *0000
Statement Period: 2025-08-01 to 2025-08-31

ACCOUNT SUMMARY
Starting Cash Balance: $1,000.00
Ending Cash Balance: $1,100.00

TRANSACTION ACTIVITY
Date    Description    Amount
2025-08-10    SYNTHETIC Starting Portfolio Value    +$100.00
END OF STATEMENT
"""

_BRIDGE = """CHARLES SCHWAB
Account Number: *0000
Statement Period: 2025-08-01 to 2025-08-31

ACCOUNT SUMMARY
Starting Cash Balance: $1,000.00
Ending Cash Balance: $1,100.00

PORTFOLIO SUMMARY
Starting Portfolio Value: $5,000.00
Transfers In: $100.00
Transfers Out: $0.00
Income and Dividends: $100.00
Realized Gain/Loss: -$50.00
Unrealized Gain/Loss: -$25.00
Ending Portfolio Value: $5,125.00

TRANSACTION ACTIVITY
Date    Description    Amount
2025-08-10    SYNTHETIC Dividend    +$100.00
END OF STATEMENT
"""


def _extraction(text: str) -> RawExtraction:
    payload = RawPayload(
        content_sha256="c" * 64,
        byte_length=len(text.encode("utf-8")),
        original_basename="synthetic_schwab.pdf",
    )
    run = ExtractionRun(
        raw_payload_id=payload.raw_payload_id,
        adapter_id="schwab_brokerage",
        adapter_version="1.0.0",
        status=RunStatus.EXTRACTED,
    )
    page = RawPage(run_id=run.run_id, page_number=1, page_text=text, tokens=[])
    return RawExtraction(payload=payload, run=run, pages=[page])


class TestSchwabPortfolioBridge:
    def setup_method(self) -> None:
        self.adapter = SchwabAdapter()

    def test_cash_only_leaves_portfolio_unset(self) -> None:
        _account, statement, summary, txns, _holdings = self.adapter.parse_canonical(
            _extraction(_CASH_ONLY)
        )
        assert statement.opening_balance_cents == 100_000
        assert statement.closing_balance_cents == 110_000
        assert sum(txn.amount_cents for txn in txns) == 10_000
        assert summary.opening_portfolio_cents is None
        assert summary.closing_portfolio_cents is None
        assert summary.transfers_in_cents is None
        assert summary.transfers_out_cents is None
        assert summary.income_dividends_cents is None
        assert summary.realized_gains_cents is None
        assert summary.unrealized_gains_cents is None

    def test_activity_wording_does_not_invent_portfolio_cents(self) -> None:
        """A cash description is not a portfolio bridge section."""
        _account, statement, summary, txns, _holdings = self.adapter.parse_canonical(
            _extraction(_CASH_ACTIVITY_MENTIONS_PORTFOLIO)
        )
        assert statement.closing_balance_cents == 110_000
        assert sum(txn.amount_cents for txn in txns) == 10_000
        assert summary.opening_portfolio_cents is None
        assert summary.closing_portfolio_cents is None
        assert summary.realized_gains_cents is None
        assert summary.unrealized_gains_cents is None

    def test_printed_bridge_reconciles_apart_from_cash(self) -> None:
        _account, statement, summary, txns, _holdings = self.adapter.parse_canonical(
            _extraction(_BRIDGE)
        )
        assert summary.opening_portfolio_cents == 500_000
        assert summary.transfers_in_cents == 10_000
        assert summary.transfers_out_cents == 0
        assert summary.income_dividends_cents == 10_000
        assert summary.realized_gains_cents == -5_000
        assert summary.unrealized_gains_cents == -2_500
        assert summary.closing_portfolio_cents == 512_500
        computed = (
            summary.opening_portfolio_cents
            + summary.transfers_in_cents
            - summary.transfers_out_cents
            + summary.income_dividends_cents
            + summary.realized_gains_cents
            + summary.unrealized_gains_cents
        )
        assert computed == summary.closing_portfolio_cents
        cash_delta = sum(txn.amount_cents for txn in txns)
        assert (
            statement.opening_balance_cents + cash_delta
            == statement.closing_balance_cents
        )
        assert statement.closing_balance_cents == summary.closing_cash_cents
        assert summary.closing_portfolio_cents != statement.closing_balance_cents
        assert cash_delta != (
            summary.closing_portfolio_cents - summary.opening_portfolio_cents
        )

    def test_missing_printed_bridge_term_fails(self) -> None:
        bad = _BRIDGE.replace("Unrealized Gain/Loss: -$25.00\n", "")
        with pytest.raises(MissingSectionError):
            self.adapter.parse_canonical(_extraction(bad))

    def test_activity_line_does_not_fill_a_missing_bridge_term(self) -> None:
        """A missing summary line stays missing even if activity text mentions it."""
        bad = _BRIDGE.replace("Unrealized Gain/Loss: -$25.00\n", "")
        bad = bad.replace(
            "2025-08-10    SYNTHETIC Dividend    +$100.00",
            "2025-08-10    SYNTHETIC Unrealized Gain/Loss: $1.00    +$100.00",
        )
        with pytest.raises(MissingSectionError):
            self.adapter.parse_canonical(_extraction(bad))

    def test_bridge_mismatch_of_one_cent_fails(self) -> None:
        bad = _BRIDGE.replace(
            "Ending Portfolio Value: $5,125.00",
            "Ending Portfolio Value: $5,125.01",
        )
        with pytest.raises(InvariantError, match=r"Portfolio bridge mismatch"):
            self.adapter.parse_canonical(_extraction(bad))

    def test_negative_transfer_in_is_rejected(self) -> None:
        bad = _BRIDGE.replace("Transfers In: $100.00", "Transfers In: ($100.00)")
        with pytest.raises(InvariantError, match=r"^transfers_in_cents must be >= 0$"):
            self.adapter.parse_summary(_extraction(bad))

    def test_negative_transfer_out_is_rejected(self) -> None:
        bad = _BRIDGE.replace("Transfers Out: $0.00", "Transfers Out: -$1.00")
        with pytest.raises(InvariantError, match=r"^transfers_out_cents must be >= 0$"):
            self.adapter.parse_summary(_extraction(bad))
