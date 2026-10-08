"""Charles Schwab brokerage-statement adapter.

This adapter targets Charles Schwab brokerage accounts (cash and margin).
It supports:
1. Canonical extraction pipeline conforming to InvestmentStatementAdapter:
   - Closed-schema validation against declared section markers and headers.
   - Ambiguous multi-account detection.
   - Universal reconciliation: opening_cash + sum(signed cash deltas) == closing_cash.
   - Domain portfolio bridge reconciliation when portfolio summary components are present:
     opening_portfolio + transfers_in - transfers_out + income_dividends + realized_gains + unrealized_gains == closing_portfolio.
2. Backwards-compatible legacy PDF parsing returning RawStatement.
"""

from __future__ import annotations

import datetime
import logging
import re
from collections.abc import Sequence
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
    CashTransaction,
    Holding,
    RawStatement,
    StatementSummary,
    TransactionType,
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
    SchemaDriftError,
    TokenError,
)
from app.models.raw import RawExtraction
from app.models.schema import TableSchema
from app.pipeline.validator import (
    validate_cash_balance,
    validate_date_continuity,
    validate_holdings_valuation,
)

logger = logging.getLogger(__name__)


class SchwabAdapter(InvestmentStatementAdapter):
    """Parse Charles Schwab monthly brokerage statements."""

    adapter_id: str = "schwab_brokerage"
    adapter_version: str = "1.0.0"
    institution_id: str = "Charles Schwab"
    BROKER_NAME: str = "Charles Schwab"
    _HEADER_MARKER: str = "Charles Schwab"
    account_types = frozenset(
        {AccountType.BROKERAGE_CASH, AccountType.BROKERAGE_MARGIN}
    )

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def matches(self, file_path: Path) -> bool:
        """Detect whether *file_path* is a Charles Schwab statement."""
        resolved: Path = file_path.resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"Statement file not found: {resolved}")

        try:
            with pdfplumber.open(resolved) as pdf:
                if not pdf.pages:
                    raise ValueError(f"PDF has no pages: {resolved}")
                first_page_text: str = pdf.pages[0].extract_text() or ""
        except Exception as exc:
            if isinstance(exc, (FileNotFoundError, ValueError)):
                raise
            raise ValueError(f"Failed to read PDF at {resolved}: {exc}") from exc

        matched = self._HEADER_MARKER.upper() in first_page_text.upper()
        if matched:
            logger.debug(
                "SchwabAdapter matched document.", extra={"file": str(resolved)}
            )
        return matched

    # ------------------------------------------------------------------
    # Canonical Schema Declaration
    # ------------------------------------------------------------------

    def declared_schemas(self) -> Sequence[TableSchema]:
        """Declare expected table structures and mandatory section markers."""
        return [
            TableSchema(
                name="schwab_cash_transactions",
                headers=("Date", "Description", "Amount"),
                section_markers=("ACCOUNT SUMMARY", "TRANSACTION ACTIVITY"),
            )
        ]

    # ------------------------------------------------------------------
    # Canonical Header Extraction
    # ------------------------------------------------------------------

    def parse_header(
        self, extraction: RawExtraction
    ) -> tuple[str, AccountType, datetime.date, datetime.date]:
        """Extract account mask, account type, and statement period dates."""
        p1_text = extraction.pages[0].page_text if extraction.pages else ""

        # 1. Ambiguous accounts detection
        masks = re.findall(
            r"Account(?: Number)?[:\s]+([*X\d-]+)", p1_text, re.IGNORECASE
        )
        unique_masks = sorted(set(masks))
        if len(unique_masks) > 1:
            logger.error("Multiple distinct account masks detected on statement page")
            raise AmbiguousAccountsError(
                f"Multiple distinct account masks detected: {unique_masks}"
            )

        if not unique_masks:
            raise MissingSectionError("Could not locate account number on statement.")
        account_mask = unique_masks[0]

        # Check for multiple account sections
        has_cash_section = bool(
            re.search(r"Brokerage\s+Cash\s+Summary", p1_text, re.IGNORECASE)
        )
        has_margin_section = bool(
            re.search(r"Margin\s+Account\s+Summary", p1_text, re.IGNORECASE)
        )
        if has_cash_section and has_margin_section:
            raise AmbiguousAccountsError(
                "Both Cash and Margin account sections detected on statement."
            )

        # 2. Account type
        if "MARGIN" in p1_text.upper():
            account_type = AccountType.BROKERAGE_MARGIN
        else:
            account_type = AccountType.BROKERAGE_CASH

        # 3. Statement period dates
        date_match = re.search(
            r"Statement Period.*?(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\s*(?:to|-)\s*(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})",
            p1_text,
            re.IGNORECASE | re.DOTALL,
        )
        if not date_match:
            raise MissingSectionError("Could not locate statement period dates.")

        def parse_date(date_str: str) -> datetime.date:
            for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
                try:
                    return datetime.datetime.strptime(date_str, fmt).date()  # noqa: DTZ007
                except ValueError:
                    continue
            raise TokenError(f"Invalid date format: {date_str}")

        start_date = parse_date(date_match.group(1))
        end_date = parse_date(date_match.group(2))

        return account_mask, account_type, start_date, end_date

    # ------------------------------------------------------------------
    # Canonical Summary Extraction
    # ------------------------------------------------------------------

    def parse_summary(
        self, extraction: RawExtraction
    ) -> tuple[int, int, BrokerageSummary]:
        """Extract cash balances and optional portfolio bridge summary."""
        all_text = "\n".join(p.page_text for p in extraction.pages)

        def extract_cents(pattern: str, required: bool = True) -> int | None:
            match = re.search(pattern, all_text, re.IGNORECASE)
            if not match:
                if required:
                    raise MissingSectionError(
                        f"Missing summary field matching pattern: {pattern}"
                    )
                return None
            val_str = match.group(1).strip()
            return parse_currency_to_cents(val_str, allow_zero=True)

        opening_cash = extract_cents(
            r"(?:Beginning|Starting)(?: Cash)? Balance[:\s]+([($]?-?[$]?[\d,]+\.\d{2}\)?)",
            required=True,
        )
        closing_cash = extract_cents(
            r"(?:Ending|Closing)(?: Cash)? Balance[:\s]+([($]?-?[$]?[\d,]+\.\d{2}\)?)",
            required=True,
        )
        assert opening_cash is not None and closing_cash is not None

        # A bridge exists only when a summary line announces it. Wording inside
        # cash activity must not invent portfolio cents.
        has_portfolio_section = bool(
            re.search(
                r"(?im)^[ \t]*(?:PORTFOLIO SUMMARY|(?:Beginning|Starting)\s+Portfolio\b)",
                all_text,
            )
        )

        opening_portfolio = None
        closing_portfolio = None
        transfers_in = None
        transfers_out = None
        income_dividends = None
        realized_gains = None
        unrealized_gains = None

        if has_portfolio_section:
            opening_portfolio = extract_cents(
                r"(?m)^[ \t]*(?:Beginning|Starting)\s+Portfolio\s*(?:Value)?[:\s]+([($]?-?[$]?[\d,]+\.\d{2}\)?)",
                required=True,
            )
            closing_portfolio = extract_cents(
                r"(?m)^[ \t]*(?:Ending|Closing)\s+Portfolio\s*(?:Value)?[:\s]+([($]?-?[$]?[\d,]+\.\d{2}\)?)",
                required=True,
            )
            transfers_in = extract_cents(
                r"(?m)^[ \t]*Transfers\s+In[:\s]+([($]?-?[$]?[\d,]+\.\d{2}\)?)",
                required=True,
            )
            transfers_out = extract_cents(
                r"(?m)^[ \t]*Transfers\s+Out[:\s]+([($]?-?[$]?[\d,]+\.\d{2}\)?)",
                required=True,
            )
            # Magnitudes only. A printed minus is rejected before BrokerageSummary.
            # required=True above already raises when the token is absent.
            if transfers_in is not None and transfers_in < 0:
                raise InvariantError("transfers_in_cents must be >= 0")
            if transfers_out is not None and transfers_out < 0:
                raise InvariantError("transfers_out_cents must be >= 0")

            income_dividends = extract_cents(
                r"(?m)^[ \t]*(?:Income\s*(?:and|&)?\s*Dividends|Dividends\s*(?:and|&)?\s*Income)[:\s]+([($]?-?[$]?[\d,]+\.\d{2}\)?)",
                required=True,
            )
            realized_gains = extract_cents(
                r"(?m)^[ \t]*Realized\s+(?:Gain(?:s)?(?:/Loss(?:es)?)?|Loss(?:es)?|Gains/Losses)[:\s]+([($]?-?[$]?[\d,]+\.\d{2}\)?)",
                required=True,
            )
            unrealized_gains = extract_cents(
                r"(?m)^[ \t]*Unrealized\s+(?:Gain(?:s)?(?:/Loss(?:es)?)?|Loss(?:es)?|Gains/Losses)[:\s]+([($]?-?[$]?[\d,]+\.\d{2}\)?)",
                required=True,
            )

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

    # ------------------------------------------------------------------
    # Canonical Transaction Extraction
    # ------------------------------------------------------------------

    def parse_transactions(
        self, extraction: RawExtraction, statement_id: UUID
    ) -> list[CanonicalTransaction]:
        """Extract canonical transactions with signed primary deltas."""
        all_text = "\n".join(p.page_text for p in extraction.pages)
        txns: list[CanonicalTransaction] = []

        section_match = re.search(
            r"(?:TRANSACTION ACTIVITY|TRANSACTION DETAILS|CASH TRANSACTIONS)\s*\n(.*?)(?:\nPOSITIONS\b|END OF STATEMENT|\Z)",
            all_text,
            re.IGNORECASE | re.DOTALL,
        )
        if not section_match:
            raise MissingSectionError(
                "Could not locate TRANSACTION ACTIVITY section block."
            )

        section_body = section_match.group(1).strip()
        lines = [line.strip() for line in section_body.split("\n") if line.strip()]

        if not lines:
            return []

        header_line = lines[0]
        headers = [
            h.strip() for h in re.split(r"\s{2,}|\t|\|", header_line) if h.strip()
        ]
        data_lines = lines[1:]

        schema = self.declared_schemas()[0]
        rows: list[list[str]] = []
        for line in data_lines:
            parts = [p.strip() for p in re.split(r"\s{2,}|\t|\|", line) if p.strip()]
            rows.append(parts)

        validate_table_against_schema(headers, rows, schema)

        for row in rows:
            raw_date, raw_desc, raw_amt = row

            def parse_row_date(date_str: str) -> datetime.date:
                for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
                    try:
                        return datetime.datetime.strptime(date_str, fmt).date()  # noqa: DTZ007
                    except ValueError:
                        continue
                raise TokenError(f"Invalid date format in row: {date_str}")

            post_date = parse_row_date(raw_date)

            has_explicit_sign = raw_amt.startswith(("+", "-")) or (
                raw_amt.startswith("(") and raw_amt.endswith(")")
            )
            if not has_explicit_sign:
                raise TokenError(
                    "Ambiguous amount sign without polarity indicator "
                    f"(got {raw_amt!r})."
                )

            desc_upper = raw_desc.upper()

            if "DIVIDEND" in desc_upper or "DIV " in desc_upper:
                category = TransactionCategory.DIVIDEND
            elif "INTEREST" in desc_upper:
                category = TransactionCategory.INTEREST
            elif (
                "DEPOSIT" in desc_upper
                or "WIRE IN" in desc_upper
                or "TRANSFER IN" in desc_upper
            ):
                category = TransactionCategory.TRANSFER_IN
            elif (
                "WITHDRAWAL" in desc_upper
                or "WIRE OUT" in desc_upper
                or "TRANSFER OUT" in desc_upper
            ):
                category = TransactionCategory.TRANSFER_OUT
            elif "FEE" in desc_upper:
                category = TransactionCategory.FEE
            elif (
                "BOUGHT" in desc_upper
                or "PURCHASE" in desc_upper
                or "BUY" in desc_upper
                or "SOLD" in desc_upper
                or "SALE" in desc_upper
                or "SELL" in desc_upper
            ):
                category = TransactionCategory.TRADE_CASH
            elif "CREDIT" in desc_upper:
                category = TransactionCategory.OTHER_CREDIT
            else:
                category = (
                    TransactionCategory.OTHER_CREDIT
                    if raw_amt.startswith("+")
                    else TransactionCategory.OTHER_DEBIT
                )

            amount_cents = parse_currency_to_cents(raw_amt)

            validate_category_for_domain(self.account_domain, category)
            validate_category_sign(self.account_domain, category, amount_cents)

            txns.append(
                CanonicalTransaction(
                    statement_id=statement_id,
                    post_date=post_date,
                    amount_cents=amount_cents,
                    description=raw_desc,
                    transaction_category=category,
                )
            )

        return txns

    def parse_holdings(
        self,
        extraction: RawExtraction,
        statement_id: UUID,
        period_start: datetime.date,
        period_end: datetime.date,
    ) -> list[Holding]:
        """Parse a printed POSITIONS table. Cash-only statements return no rows.

        When POSITIONS is present, headers are closed and a Holdings Total line
        is mandatory. The sum of market values must match that total.
        """
        all_text = "\n".join(p.page_text for p in extraction.pages)
        if not re.search(r"(?m)^POSITIONS\s*$", all_text):
            return []

        section_match = re.search(
            r"^POSITIONS\s*\n(.*?)(?:END OF STATEMENT|\Z)",
            all_text,
            re.IGNORECASE | re.DOTALL | re.MULTILINE,
        )
        if not section_match:
            raise MissingSectionError("Could not locate POSITIONS section block.")

        section_body = section_match.group(1).strip()
        total_match = re.search(
            r"Holdings Total:\s*([($]?-?[$]?[\d,]+\.\d{2}\)?)",
            section_body,
            re.IGNORECASE,
        )
        if not total_match:
            raise MissingSectionError(
                "Holdings Total is required when a POSITIONS section is printed."
            )
        stated_total = parse_currency_to_cents(total_match.group(1), allow_zero=True)
        table_body = re.sub(
            r"Holdings Total:.*",
            "",
            section_body,
            count=1,
            flags=re.IGNORECASE,
        ).strip()
        lines = [line.strip() for line in table_body.split("\n") if line.strip()]
        if not lines:
            raise MissingSectionError("POSITIONS section is missing a header row.")

        headers = [h.strip() for h in re.split(r"\s{2,}|\t|\|", lines[0]) if h.strip()]
        rows: list[list[str]] = []
        for line in lines[1:]:
            parts = [p.strip() for p in re.split(r"\s{2,}|\t|\|", line) if p.strip()]
            rows.append(parts)

        schema = TableSchema(
            name="schwab_positions",
            headers=("Symbol", "Description", "Quantity", "Market Value"),
            section_markers=("POSITIONS",),
        )
        validate_table_against_schema(headers, rows, schema)

        holdings: list[Holding] = []
        for row in rows:
            symbol, description, quantity, market_value = row
            holdings.append(
                Holding(
                    statement_id=statement_id,
                    as_of_date=period_end,
                    symbol=symbol,
                    description=description,
                    quantity_nanos=parse_quantity_to_nanos(quantity),
                    market_value_cents=parse_currency_to_cents(market_value),
                )
            )

        validate_holdings_valuation(holdings, stated_total, period_start, period_end)
        logger.info(
            "Parsed Schwab holdings",
            extra={"holding_count": len(holdings)},
        )
        return holdings

    # ------------------------------------------------------------------
    # Backwards-compatible Legacy Extraction Entry Point
    # ------------------------------------------------------------------

    def parse(self, file_path: Path) -> RawStatement:
        """Extract cash transactions from a Schwab statement PDF into legacy RawStatement."""
        resolved: Path = file_path.resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"Statement file not found: {resolved}")

        logger.info(
            "Starting Schwab statement extraction",
            extra={"file": str(resolved)},
        )

        try:
            with pdfplumber.open(resolved) as pdf:
                if not pdf.pages:
                    raise ValueError(f"PDF has no pages: {resolved}")

                summary: StatementSummary = self._extract_summary(pdf)
                transactions: list[CashTransaction] = self._extract_transactions(
                    pdf,
                    period_start=summary.period_start,
                    period_end=summary.period_end,
                )
        except Exception as exc:
            logger.error(
                "Schwab parsing failed",
                extra={"file": str(resolved), "error": str(exc)},
            )
            if isinstance(
                exc,
                (
                    FileNotFoundError,
                    ValueError,
                    MissingSectionError,
                    SchemaDriftError,
                    TokenError,
                ),
            ):
                raise
            raise ValueError(
                f"Failed to parse Schwab PDF at {resolved}: {exc}"
            ) from exc

        statement: RawStatement = RawStatement(
            source_file=resolved.name,
            broker=self.BROKER_NAME,
            summary=summary,
            transactions=transactions,
        )

        logger.info(
            "Running pipeline validation on Schwab statement",
            extra={"file": str(resolved)},
        )
        validate_date_continuity(statement)
        validate_cash_balance(statement)

        logger.info(
            "Schwab statement extraction successful",
            extra={
                "file": str(resolved),
                "transactions_count": len(transactions),
            },
        )
        return statement

    # ------------------------------------------------------------------
    # Legacy Summary Extraction Helpers
    # ------------------------------------------------------------------

    def _extract_summary(self, pdf: pdfplumber.PDF) -> StatementSummary:
        """Parse the account summary block from the first page of the PDF."""
        first_page_text: str = pdf.pages[0].extract_text() or ""

        # 1. Extract Account number
        account_match = re.search(
            r"Account(?: Number)?[:\s]+([*X\d-]+)", first_page_text, re.IGNORECASE
        )
        if not account_match:
            logger.error(
                "Missing account number block",
                extra={"page_text_len": len(first_page_text)},
            )
            raise MissingSectionError("Could not locate account number on page 1.")
        account_number_masked = account_match.group(1)

        # 2. Extract Statement Period dates
        date_match = re.search(
            r"Statement Period.*?(\d{1,2}/\d{1,2}/\d{2,4})\s*(?:-|to)\s*(\d{1,2}/\d{1,2}/\d{2,4})",
            first_page_text,
            re.IGNORECASE | re.DOTALL,
        )
        if not date_match:
            logger.error(
                "Missing statement period dates",
                extra={"page_text_len": len(first_page_text)},
            )
            raise MissingSectionError(
                "Could not locate statement period dates on page 1."
            )

        def parse_date(date_str: str) -> datetime.date:
            for fmt in ("%m/%d/%Y", "%m/%d/%y"):
                try:
                    return datetime.datetime.strptime(date_str, fmt).date()  # noqa: DTZ007
                except ValueError:
                    continue
            logger.error("Invalid summary date format", extra={"date_str": date_str})
            raise TokenError(f"Could not parse date format: {date_str}")

        period_start = parse_date(date_match.group(1))
        period_end = parse_date(date_match.group(2))

        # 3. Extract Opening and Closing Cash Balances
        start_match = re.search(
            r"(?:Beginning|Starting)(?: Cash)? Balance[^$\d]*([$]?-?[\d,]+\.\d{2})",
            first_page_text,
            re.IGNORECASE,
        )
        if not start_match:
            logger.error("Missing starting cash balance")
            raise MissingSectionError("Could not locate starting balance on page 1.")

        end_match = re.search(
            r"(?:Ending|Closing)(?: Cash)? Balance[^$\d]*([$]?-?[\d,]+\.\d{2})",
            first_page_text,
            re.IGNORECASE,
        )
        if not end_match:
            logger.error("Missing ending cash balance")
            raise MissingSectionError("Could not locate ending balance on page 1.")

        start_balance_cents = self._parse_cents(start_match.group(1))
        end_balance_cents = self._parse_cents(end_match.group(1))

        return StatementSummary(
            account_number_masked=account_number_masked,
            period_start=period_start,
            period_end=period_end,
            start_balance_cents=start_balance_cents,
            end_balance_cents=end_balance_cents,
        )

    # ------------------------------------------------------------------
    # Legacy Transaction Extraction Helpers
    # ------------------------------------------------------------------

    def _extract_transactions(
        self,
        pdf: pdfplumber.PDF,
        period_start: datetime.date,
        period_end: datetime.date,
    ) -> list[CashTransaction]:
        """Extract the cash transactions ledger from the statement."""
        transactions: list[CashTransaction] = []
        target_headers = {"date", "description", "amount"}

        for page in pdf.pages:
            text = page.extract_text() or ""
            if "Transaction" not in text and "Activity" not in text:
                continue

            for table in page.extract_tables():
                if not table or not table[0]:
                    continue

                headers = [str(h).strip().lower() for h in table[0] if h]
                if not target_headers.issubset(set(headers)):
                    continue

                try:
                    date_idx = headers.index("date")
                    desc_idx = headers.index("description")
                    amt_idx = headers.index("amount")
                except ValueError as exc:
                    logger.error(
                        "Transaction table headers drifted",
                        extra={"headers": headers},
                    )
                    raise SchemaDriftError(
                        f"Missing required columns in table: {headers}"
                    ) from exc

                cat_idx = headers.index("category") if "category" in headers else None

                for row in table[1:]:
                    if not row or not any(str(c).strip() for c in row):
                        continue

                    if len(row) <= max(date_idx, desc_idx, amt_idx):
                        logger.error("Malformed row structure", extra={"row": row})
                        raise SchemaDriftError(f"Row has insufficient columns: {row}")

                    raw_date = str(row[date_idx] or "").strip()
                    raw_desc = str(row[desc_idx] or "").strip()
                    raw_amt = str(row[amt_idx] or "").strip()

                    if not raw_date and not raw_amt and raw_desc:
                        continue

                    # Parse Date
                    txn_date = None
                    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
                        try:
                            txn_date = datetime.datetime.strptime(  # noqa: DTZ007
                                raw_date, fmt
                            ).date()
                            break
                        except ValueError:
                            continue

                    if not txn_date:
                        logger.error(
                            "Failed to parse transaction date",
                            extra={"raw_date": raw_date, "row": row},
                        )
                        raise TokenError(
                            f"Invalid date format in transaction row: '{raw_date}'"
                        )

                    if not (period_start <= txn_date <= period_end):
                        raise InvariantError(
                            f"Transaction date {txn_date.isoformat()} is outside "
                            f"statement period "
                            f"{period_start.isoformat()}..{period_end.isoformat()}"
                        )

                    is_debit = raw_amt.startswith(("-", "("))
                    txn_type = (
                        TransactionType.DEBIT if is_debit else TransactionType.CREDIT
                    )

                    try:
                        amount_cents = self._parse_cents(raw_amt)
                    except ValueError as exc:
                        logger.error(
                            "Failed to parse transaction amount",
                            extra={"raw_amt": raw_amt, "row": row},
                        )
                        raise TokenError(
                            f"Invalid amount format in transaction row: '{raw_amt}'"
                        ) from exc

                    category = None
                    if cat_idx is not None and len(row) > cat_idx:
                        category = str(row[cat_idx] or "").strip() or None

                    transactions.append(
                        CashTransaction(
                            date=txn_date,
                            description=raw_desc,
                            transaction_type=txn_type,
                            amount_cents=amount_cents,
                            category=category,
                        )
                    )

        transactions.sort(key=lambda t: t.date)
        return transactions

    def _parse_cents(self, amount_str: str) -> int:
        """Convert a raw currency string into a positive integer cent value."""
        if not amount_str or not amount_str.strip():
            raise ValueError("Amount string must be a non-empty string.")

        cleaned = amount_str.strip()

        if cleaned.startswith("(") and cleaned.endswith(")"):
            cleaned = cleaned[1:-1].strip()

        cleaned = cleaned.lstrip("$-").replace(",", "").strip()

        if not cleaned:
            raise ValueError(f"Invalid currency format: '{amount_str}'")

        parts = cleaned.split(".")
        if len(parts) == 1:
            dollars_str = parts[0]
            cents_str = "00"
        elif len(parts) == 2:
            dollars_str, cents_str = parts
            if len(cents_str) == 1:
                cents_str = f"{cents_str}0"
            elif len(cents_str) != 2:
                raise ValueError(f"Invalid decimal places in: '{amount_str}'")
        else:
            raise ValueError(f"Multiple decimal points in: '{amount_str}'")

        if not dollars_str.isdigit() or not cents_str.isdigit():
            raise ValueError(f"Non-digit numeric content in: '{amount_str}'")

        total_cents = int(dollars_str) * 100 + int(cents_str)

        if total_cents <= 0:
            raise ValueError(f"Parsed amount must be positive, got: {total_cents}")

        return total_cents
