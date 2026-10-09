"""Vanguard brokerage-statement adapter.

Closed schema for synthetic Vanguard cash ledgers. Portfolio bridge terms are
stored as printed signed cents. Transfer magnitudes must be non-negative;
a printed negative transfer fails instead of being flipped with abs().
"""

from __future__ import annotations

import datetime
import logging
import re
from pathlib import Path
from uuid import UUID

import pdfplumber

from app.adapters.base import InvestmentStatementAdapter
from app.extraction.core import (
    parse_currency_to_cents,
    parse_quantity_to_nanos,
    validate_table_against_schema,
)
from app.models.canonical import (
    BrokerageSummary,
    CanonicalTransaction,
    Holding,
)
from app.models.enums import (
    AccountType,
    TransactionCategory,
    validate_category_for_domain,
    validate_category_sign,
)
from app.models.exceptions import (
    AmbiguousAccountsError,
    InvariantError,
    MissingSectionError,
    TokenError,
)
from app.models.raw import RawExtraction
from app.models.schema import TableSchema
from app.pipeline.validator import validate_holdings_valuation

logger = logging.getLogger(__name__)

_MONEY = r"([($]?-?\$?[\d,]+\.\d{2}\)?)"
_ACTIVITY_SCHEMA = TableSchema(
    name="vanguard_activity",
    headers=("Date", "Activity", "Amount"),
    section_markers=("CASH SUMMARY", "ACTIVITY"),
)
_POSITIONS_SCHEMA = TableSchema(
    name="vanguard_positions",
    headers=("Symbol", "Description", "Shares", "Value"),
    section_markers=("FUND POSITIONS",),
)


class VanguardAdapter(InvestmentStatementAdapter):
    """Parse Vanguard brokerage statements into the canonical cash ledger."""

    adapter_id: str = "vanguard_brokerage"
    adapter_version: str = "1.0.0"
    institution_id: str = "Vanguard"
    _HEADER_MARKER: str = "VANGUARD"

    def matches(self, file_path: Path) -> bool:
        """Detect a Vanguard statement from the first page."""
        resolved = file_path.resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"Statement file not found: {resolved}")

        try:
            with pdfplumber.open(resolved) as pdf:
                if not pdf.pages:
                    raise ValueError(f"PDF has no pages: {resolved}")
                first_page_text = pdf.pages[0].extract_text() or ""
        except Exception as exc:
            if isinstance(exc, (FileNotFoundError, ValueError)):
                raise
            raise ValueError(f"Failed to read PDF at {resolved}: {exc}") from exc

        return self._HEADER_MARKER in first_page_text.upper()

    def declared_schemas(self) -> tuple[TableSchema, ...]:
        """Cash summary and activity are mandatory. Positions are optional."""
        return (_ACTIVITY_SCHEMA,)

    def parse_header(
        self, extraction: RawExtraction
    ) -> tuple[str, AccountType, datetime.date, datetime.date]:
        """Extract the masked account, registration, and statement period."""
        text = _page_text(extraction)
        masks = re.findall(r"Vanguard Account:\s*(\*\d{4})", text)
        unique_masks = sorted(set(masks))
        if len(unique_masks) > 1:
            raise AmbiguousAccountsError(
                f"Multiple distinct account masks detected: {unique_masks}"
            )
        if not unique_masks:
            raise MissingSectionError("Could not locate Vanguard account mask.")

        registrations = re.findall(
            r"Registration:\s*(Brokerage Cash|Brokerage Margin)", text
        )
        unique_regs = sorted(set(registrations))
        if len(unique_regs) > 1:
            raise AmbiguousAccountsError(
                "Both Brokerage Cash and Brokerage Margin registrations are printed."
            )
        if not unique_regs:
            raise MissingSectionError("Could not locate Vanguard registration.")
        account_type = (
            AccountType.BROKERAGE_MARGIN
            if unique_regs[0] == "Brokerage Margin"
            else AccountType.BROKERAGE_CASH
        )

        period = re.search(
            r"Period:\s*(\d{4}-\d{2}-\d{2})\s+through\s+(\d{4}-\d{2}-\d{2})",
            text,
        )
        if period is None:
            raise MissingSectionError("Could not locate statement period dates.")
        start_date = _parse_iso_date(period.group(1))
        end_date = _parse_iso_date(period.group(2))
        return unique_masks[0], account_type, start_date, end_date

    def parse_summary(
        self, extraction: RawExtraction
    ) -> tuple[int, int, BrokerageSummary]:
        """Extract cash balances and an optional signed portfolio bridge."""
        text = _page_text(extraction)
        opening_cash = _require_money(text, "Opening Cash")
        closing_cash = _require_money(text, "Closing Cash")

        opening_portfolio = None
        closing_portfolio = None
        transfers_in = None
        transfers_out = None
        income_dividends = None
        realized_gains = None
        unrealized_gains = None

        if re.search(r"(?m)^PORTFOLIO BRIDGE\s*$", text):
            opening_portfolio = _require_money(text, "Opening Portfolio Value")
            closing_portfolio = _require_money(text, "Closing Portfolio Value")
            transfers_in = _require_money(text, "Transfers In")
            transfers_out = _require_money(text, "Transfers Out")
            income_dividends = _require_money(text, "Dividends")
            realized_gains = _require_money(text, "Realized Gain")
            unrealized_gains = _require_money(text, "Unrealized Gain")
            if transfers_in < 0:
                raise InvariantError("transfers_in_cents must be >= 0")
            if transfers_out < 0:
                raise InvariantError("transfers_out_cents must be >= 0")

        summary = BrokerageSummary(
            statement_id=extraction.run.run_id,
            opening_cash_cents=opening_cash,
            closing_cash_cents=closing_cash,
            opening_portfolio_cents=opening_portfolio,
            closing_portfolio_cents=closing_portfolio,
            transfers_in_cents=transfers_in,
            transfers_out_cents=transfers_out,
            income_dividends_cents=income_dividends,
            realized_gains_cents=realized_gains,
            unrealized_gains_cents=unrealized_gains,
        )
        return opening_cash, closing_cash, summary

    def parse_transactions(
        self, extraction: RawExtraction, statement_id: UUID
    ) -> list[CanonicalTransaction]:
        """Extract the closed cash activity table."""
        text = _page_text(extraction)
        section = re.search(
            r"(?m)^ACTIVITY\s*$\n(.*?)(?=^FUND POSITIONS\s*$|^PORTFOLIO BRIDGE\s*$|\Z)",
            text,
            re.DOTALL,
        )
        if section is None:
            raise MissingSectionError("Could not locate ACTIVITY section block.")

        lines = [line.strip() for line in section.group(1).splitlines() if line.strip()]
        if not lines:
            raise MissingSectionError("ACTIVITY section is missing a header row.")

        headers = _split_columns(lines[0])
        rows = [_split_columns(line) for line in lines[1:]]
        validate_table_against_schema(headers, rows, _ACTIVITY_SCHEMA)

        transactions: list[CanonicalTransaction] = []
        for raw_date, raw_activity, raw_amount in rows:
            if not (
                raw_amount.startswith(("+", "-"))
                or (raw_amount.startswith("(") and raw_amount.endswith(")"))
            ):
                raise TokenError(
                    "Ambiguous amount sign without polarity indicator "
                    f"(got {raw_amount!r})."
                )
            category = _category_for_activity(raw_activity, raw_amount)
            amount_cents = parse_currency_to_cents(raw_amount)
            validate_category_for_domain(self.account_domain, category)
            validate_category_sign(self.account_domain, category, amount_cents)
            transactions.append(
                CanonicalTransaction(
                    statement_id=statement_id,
                    post_date=_parse_iso_date(raw_date),
                    amount_cents=amount_cents,
                    description=raw_activity,
                    transaction_category=category,
                )
            )

        logger.info(
            "Parsed Vanguard cash activity",
            extra={"transaction_count": len(transactions)},
        )
        return transactions

    def parse_holdings(
        self,
        extraction: RawExtraction,
        statement_id: UUID,
        period_start: datetime.date,
        period_end: datetime.date,
    ) -> list[Holding]:
        """Parse FUND POSITIONS when printed. Cash-only statements return none."""
        text = _page_text(extraction)
        if not re.search(r"(?m)^FUND POSITIONS\s*$", text):
            return []

        section = re.search(
            r"(?m)^FUND POSITIONS\s*$\n(.*?)(?=^PORTFOLIO BRIDGE\s*$|\Z)",
            text,
            re.DOTALL,
        )
        if section is None:
            raise MissingSectionError("Could not locate FUND POSITIONS section block.")

        body = section.group(1).strip()
        total_match = re.search(rf"Positions Total:\s*{_MONEY}", body)
        if total_match is None:
            raise MissingSectionError(
                "Positions Total is required when FUND POSITIONS is printed."
            )
        stated_total = parse_currency_to_cents(total_match.group(1), allow_zero=True)
        table_body = re.sub(
            r"Positions Total:.*", "", body, count=1, flags=re.IGNORECASE
        ).strip()
        lines = [line.strip() for line in table_body.splitlines() if line.strip()]
        if not lines:
            raise MissingSectionError("FUND POSITIONS section is missing a header row.")

        headers = _split_columns(lines[0])
        rows = [_split_columns(line) for line in lines[1:]]
        validate_table_against_schema(headers, rows, _POSITIONS_SCHEMA)

        holdings = [
            Holding(
                statement_id=statement_id,
                as_of_date=period_end,
                symbol=symbol,
                description=description,
                quantity_nanos=parse_quantity_to_nanos(shares),
                market_value_cents=parse_currency_to_cents(value),
            )
            for symbol, description, shares, value in rows
        ]
        validate_holdings_valuation(holdings, stated_total, period_start, period_end)
        logger.info(
            "Parsed Vanguard holdings",
            extra={"holding_count": len(holdings)},
        )
        return holdings


def _page_text(extraction: RawExtraction) -> str:
    return "\n".join(page.page_text for page in extraction.pages)


def _split_columns(line: str) -> list[str]:
    return [part.strip() for part in line.split("|")]


def _parse_iso_date(value: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise TokenError(f"Invalid date format: {value}") from exc


def _require_money(text: str, label: str) -> int:
    match = re.search(rf"{re.escape(label)}:\s*{_MONEY}", text)
    if match is None:
        raise MissingSectionError(f"Missing summary field: {label}")
    return parse_currency_to_cents(match.group(1), allow_zero=True)


def _category_for_activity(activity: str, amount: str) -> TransactionCategory:
    label = activity.upper()
    if "DIVIDEND" in label:
        return TransactionCategory.DIVIDEND
    if "FEE" in label:
        return TransactionCategory.FEE
    if "TRANSFER IN" in label or "DEPOSIT" in label:
        return TransactionCategory.TRANSFER_IN
    if "TRANSFER OUT" in label or "WITHDRAWAL" in label:
        return TransactionCategory.TRANSFER_OUT
    if "BUY" in label or "SELL" in label:
        return TransactionCategory.TRADE_CASH
    if amount.startswith("+"):
        return TransactionCategory.OTHER_CREDIT
    return TransactionCategory.OTHER_DEBIT
