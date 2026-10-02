"""Charles Schwab brokerage-statement adapter.

This adapter targets the standard Schwab *Individual Brokerage Account*
monthly PDF statement.  It identifies candidate documents by scanning
the first page for the ``"Charles Schwab"`` header and then extracts
the cash-transaction ledger into canonical models.

Extraction pipeline:

1. Verify file existence and open the PDF with ``pdfplumber``.
2. Extract the account summary (masked account number, period dates,
   opening/closing cash balances) from page 1.
3. Walk the cash-transaction table(s) across all relevant pages and
   build a date-sorted list of ``CashTransaction`` records.
4. Run pipeline validators (date continuity, cash-balance invariant)
   against the assembled ``RawStatement`` before returning it.
"""

from __future__ import annotations

import datetime
import logging
import re
from pathlib import Path

import pdfplumber

from app.adapters.base import StatementAdapter
from app.models.canonical import (
    CashTransaction,
    RawStatement,
    StatementSummary,
    TransactionType,
)
from app.models.exceptions import (
    MissingSectionError,
    SchemaDriftError,
    TokenError,
)
from app.pipeline.validator import validate_cash_balance, validate_date_continuity

logger = logging.getLogger(__name__)


class SchwabAdapter(StatementAdapter):
    """Parse Charles Schwab monthly brokerage statements."""

    BROKER_NAME: str = "Charles Schwab"
    _HEADER_MARKER: str = "Charles Schwab"

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

        matched = self._HEADER_MARKER in first_page_text
        if matched:
            logger.debug("SchwabAdapter matched document.", extra={"file": str(resolved)})
        return matched

    # ------------------------------------------------------------------
    # Extraction entry point
    # ------------------------------------------------------------------

    def parse(self, file_path: Path) -> RawStatement:
        """Extract cash transactions from a Schwab statement PDF."""
        resolved: Path = file_path.resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"Statement file not found: {resolved}")

        logger.info("Starting Schwab statement extraction", extra={"file": str(resolved)})

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
            logger.error("Schwab parsing failed", extra={"file": str(resolved), "error": str(exc)})
            if isinstance(exc, (FileNotFoundError, ValueError, MissingSectionError, SchemaDriftError, TokenError)):
                raise
            raise ValueError(f"Failed to parse Schwab PDF at {resolved}: {exc}") from exc

        statement: RawStatement = RawStatement(
            source_file=resolved.name,
            broker=self.BROKER_NAME,
            summary=summary,
            transactions=transactions,
        )

        logger.info("Running pipeline validation on Schwab statement", extra={"file": str(resolved)})
        validate_date_continuity(statement)
        validate_cash_balance(statement)

        logger.info("Schwab statement extraction successful", extra={
            "file": str(resolved),
            "transactions_count": len(transactions)
        })
        return statement

    # ------------------------------------------------------------------
    # Summary extraction
    # ------------------------------------------------------------------

    def _extract_summary(self, pdf: pdfplumber.PDF) -> StatementSummary:
        """Parse the account summary block from the first page of the PDF."""
        first_page_text: str = pdf.pages[0].extract_text() or ""

        # 1. Extract Account number
        account_match = re.search(r"Account(?: Number)?[:\s]+([*X\d-]+)", first_page_text, re.IGNORECASE)
        if not account_match:
            logger.error("Missing account number block", extra={"page_text_len": len(first_page_text)})
            raise MissingSectionError("Could not locate account number on page 1.")
        account_number_masked = account_match.group(1)

        # 2. Extract Statement Period dates
        date_match = re.search(
           r"Statement Period.*?(\d{1,2}/\d{1,2}/\d{2,4})\s*(?:-|to)\s*(\d{1,2}/\d{1,2}/\d{2,4})", 
           first_page_text,
           re.IGNORECASE | re.DOTALL
        )
        if not date_match:
            logger.error("Missing statement period dates", extra={"page_text_len": len(first_page_text)})
            raise MissingSectionError("Could not locate statement period dates on page 1.")

        def parse_date(date_str: str) -> datetime.date:
            for fmt in ("%m/%d/%Y", "%m/%d/%y"):
                try:
                    return datetime.datetime.strptime(date_str, fmt).date() # noqa: DTZ007
                except ValueError:
                    pass
            logger.error("Invalid summary date format", extra={"date_str": date_str})
            raise TokenError(f"Could not parse date format: {date_str}")

        period_start = parse_date(date_match.group(1))
        period_end = parse_date(date_match.group(2))

        # 3. Extract Opening and Closing Cash Balances
        start_match = re.search(
            r"(?:Beginning|Starting)(?: Cash)? Balance[^$\d]*([$]?-?[\d,]+\.\d{2})",
            first_page_text,
            re.IGNORECASE
        )
        if not start_match:
            logger.error("Missing starting cash balance")
            raise MissingSectionError("Could not locate starting balance on page 1.")

        end_match = re.search(
            r"(?:Ending|Closing)(?: Cash)? Balance[^$\d]*([$]?-?[\d,]+\.\d{2})",
            first_page_text,
            re.IGNORECASE
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
            end_balance_cents=end_balance_cents
        )

    # ------------------------------------------------------------------
    # Transaction extraction
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
                    logger.error("Transaction table headers drifted", extra={"headers": headers})
                    raise SchemaDriftError(f"Missing required columns in table: {headers}") from exc

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
                        # Recognized as a sub-header or visual divider
                        continue

                    # 1. Parse Date
                    txn_date = None
                    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
                        try:
                            txn_date = datetime.datetime.strptime(raw_date, fmt).date() # noqa: DTZ007
                            break
                        except ValueError:
                            pass

                    if not txn_date:
                        logger.error("Failed to parse transaction date", extra={"raw_date": raw_date, "row": row})
                        raise TokenError(f"Invalid date format in transaction row: '{raw_date}'")

                    if not (period_start <= txn_date <= period_end):
                        # Filter transactions strictly outside the statement period
                        # We don't fail here since statements often include pending/subsequent transactions
                        continue

                    # 2. Determine Transaction Type (Debit/Credit)
                    is_debit = raw_amt.startswith(("-", "("))
                    txn_type = TransactionType.DEBIT if is_debit else TransactionType.CREDIT

                    # 3. Parse Amount
                    try:
                        amount_cents = self._parse_cents(raw_amt)
                    except ValueError as exc:
                        logger.error("Failed to parse transaction amount", extra={"raw_amt": raw_amt, "row": row})
                        raise TokenError(f"Invalid amount format in transaction row: '{raw_amt}'") from exc

                    # 4. Extract Category
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

    # ------------------------------------------------------------------
    # Currency parsing
    # ------------------------------------------------------------------

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
