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
from pathlib import Path

import pdfplumber

from app.adapters.base import StatementAdapter
from app.models.canonical import (
    CashTransaction,
    RawStatement,
    StatementSummary,
)
from app.pipeline.validator import validate_cash_balance, validate_date_continuity


class SchwabAdapter(StatementAdapter):
    """Parse Charles Schwab monthly brokerage statements.

    Detection heuristic
    --------------------
    The first page of every Schwab statement contains the literal string
    ``"Charles Schwab"`` in the header region.  :meth:`matches` opens the
    PDF, extracts text from page 1, and checks for that marker.

    Extraction strategy
    --------------------
    1. Locate the *Cash Transactions* table via anchor text.
    2. Extract rows with ``pdfplumber``'s table-detection API.
    3. Parse dates, descriptions, and dollar amounts into
       :class:`~app.models.canonical.CashTransaction` instances using
       integer-cent arithmetic.
    4. Read the summary header for opening/closing balances.
    """

    BROKER_NAME: str = "Charles Schwab"
    _HEADER_MARKER: str = "Charles Schwab"

    # ------------------------------------------------------------------
    # Detection
    # ------------------------------------------------------------------

    def matches(self, file_path: Path) -> bool:
        """Detect whether *file_path* is a Charles Schwab statement.

        Opens the PDF with ``pdfplumber``, extracts the full text of
        page 1, and checks for the presence of :data:`_HEADER_MARKER`.

        Args:
            file_path: Path to a candidate PDF file.

        Returns:
            ``True`` when the first-page text contains the Schwab header
            marker.

        Raises:
            FileNotFoundError: If *file_path* does not exist on disk.
            ValueError: If the file cannot be opened as a valid PDF.
        """
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
            raise ValueError(
                f"Failed to read PDF at {resolved}: {exc}"
            ) from exc

        return self._HEADER_MARKER in first_page_text

    # ------------------------------------------------------------------
    # Extraction entry point
    # ------------------------------------------------------------------

    def parse(self, file_path: Path) -> RawStatement:
        """Extract cash transactions from a Schwab statement PDF.

        Opens the PDF, delegates to :meth:`_extract_summary` and
        :meth:`_extract_transactions` for structured extraction, then
        validates the assembled statement against the cash-balance
        invariant and date-continuity rules before returning it.

        Args:
            file_path: Path to the Schwab PDF statement.

        Returns:
            A fully populated and validated
            :class:`~app.models.canonical.RawStatement`.

        Raises:
            FileNotFoundError: If *file_path* does not exist on disk.
            ValueError: If the PDF is malformed, required data blocks
                are missing, or the extracted data fails validation.
        """
        resolved: Path = file_path.resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"Statement file not found: {resolved}")

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
            if isinstance(exc, (FileNotFoundError, ValueError, NotImplementedError)):
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

        # Pipeline validation: raises ValueError on invariant violations.
        validate_date_continuity(statement)
        validate_cash_balance(statement)

        return statement

    # ------------------------------------------------------------------
    # Summary extraction
    # ------------------------------------------------------------------

    def _extract_summary(self, pdf: pdfplumber.PDF) -> StatementSummary:
        """Parse the account summary block from the first page of the PDF.

        Reads the first page text to locate and extract:

        - Masked account number (e.g. ``"****1234"``).
        - Statement period start and end dates.
        - Opening (start) cash balance in integer cents.
        - Closing (end) cash balance in integer cents.

        Args:
            pdf: An open ``pdfplumber.PDF`` instance with at least one
                page.

        Returns:
            A populated :class:`~app.models.canonical.StatementSummary`.

        Raises:
            ValueError: If the summary block cannot be located or any
                required field is missing or unparseable.
            NotImplementedError: Extraction logic is not yet
                implemented.
        """
        import re
        import datetime

        first_page_text: str = pdf.pages[0].extract_text() or ""

        # 1. Extract Account number
        # Matches patterns like "Account Number ****1234" or "Account Number: XXXX-1234"
        account_match = re.search(r"Account(?: Number)?[:\s]+([*X\d-]+)", first_page_text, re.IGNORECASE)
        if not account_match:
            raise ValueError("Could not locate account number on page1.")
        account_number_masked = account_match.group(1)

        # 2. Extract Statement Period dates
        # Matches patterns like "Statement Period 08/01/25 to 08/31/25" or "08/01/2025 - 08/31/2025"
        date_match = re.search(
           r"Statement Period.*?(\d{1,2}/\d{1,2}/\d{2,4})\s*(?:-|to)\s*(\d{1,2}/\d{1,2}/\d{2,4})", 
           first_page_text,
           re.IGNORECASE | re.DOTALL
        )
        if not date_match:
            raise ValueError("Could not locate statement period dates on page 1.")

        def parse_date(date_str: str) -> datetime.date:
            for fmt in ("%m/%d/%Y", "%m/%d/%y"):
                try:
                    return datetime.datetime.strptime(date_str, fmt).date()
                except ValueError:
                    pass
            raise ValueError(f"Could not parse date format: {date_str}")

        period_start = parse_date(date_match.group(1))
        period_end = parse_date(date_match.group(2))

        # 3. Extract Opening and Closing Cash Balances
        # Matches patterns like "Beginning Balance $10,000.00" or "Starting Cash Balance: 10,000.00"
        start_match = re.search(
            r"(?:Beginning|Starting)(?: Cash)? Balance[^$\d]*([$]?-?[\d,]+\.\d{2})",
            first_page_text,
            re.IGNORECASE
        )
        if not start_match:
            raise ValueError("Could not locate starting balance on page 1.")

        end_match = re.search(
            r"(?:Ending|Closing)(?: Cash)? Balance[^$\d]*([$]?-?[\d,]+\.\d{2})",
            first_page_text,
            re.IGNORECASE
        )
        if not end_match:
            raise ValueError("Could not locate ending balance on page 1.")

        # Re-use the existing _parse_cents helper to cleanly handle '$', ',', and sign conversions
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
        """Walk the cash-transaction table(s) and build canonical records.

        Iterates over relevant pages of the PDF, locates the
        *Cash Transactions* table using ``pdfplumber``'s table-detection
        API, and converts each row into a
        :class:`~app.models.canonical.CashTransaction`.

        Transactions are returned sorted by date ascending.  The
        *period_start* and *period_end* arguments are provided so that
        year inference can be applied to dates printed without a year
        component (common on Schwab statements).

        Args:
            pdf: An open ``pdfplumber.PDF`` instance.
            period_start: First day of the statement period, used for
                year inference on date-only fields.
            period_end: Last day of the statement period, used for year
                inference on date-only fields.

        Returns:
            A list of ``CashTransaction`` records sorted by date
            ascending.

        Raises:
            ValueError: If the transaction table cannot be found, a row
                is malformed, or a required field is missing.
            NotImplementedError: Extraction logic is not yet
                implemented.
        """
        raise NotImplementedError(
            "SchwabAdapter._extract_transactions() is not yet implemented. "
            "Add table-detection logic to locate the Cash Transactions block "
            "and parse each row into a CashTransaction."
        )

    # ------------------------------------------------------------------
    # Currency parsing
    # ------------------------------------------------------------------

    def _parse_cents(self, amount_str: str) -> int:
        """Convert a raw currency string into a positive integer cent value.

        Handles common formatting variations found on Schwab statements:

        - Dollar signs: ``"$1,234.56"`` -> ``123456``
        - Thousands separators: ``"1,234.56"`` -> ``123456``
        - Parenthesised negatives: ``"($50.00)"`` -> ``5000``
        - Plain decimals: ``"100.00"`` -> ``10000``

        The returned value is always the unsigned magnitude.  Sign
        (credit vs. debit) is determined by the column the amount
        appears in, not by the string itself.

        Args:
            amount_str: Raw text scraped from a dollar-amount cell.

        Returns:
            A strictly positive ``int`` representing the amount in cents.

        Raises:
            ValueError: If *amount_str* is empty, contains no
                recognisable numeric content, or would result in a
                zero-cent value.
            NotImplementedError: Parsing logic is not yet implemented.
        """

        if not amount_str or not amount_str.strip():
            raise ValueError("Amount string must be a non-empty string.")

        cleaned = amount_str.strip()
        
        # Remove outer parentheses used for negative numbers
        if cleaned.startswith("(") and cleaned.endswith(")"):
            cleaned = cleaned[1:-1].strip()

        # Strip currency symbol and commas
        cleaned = cleaned.lstrip("$").replace(",", "").strip()

        # Handle negative sign if present
        cleaned = cleaned.lstrip("-").strip()

        if not cleaned:
            raise ValueError(
                f"Invalid currency format: '{amount_str}'"
            )

        parts = cleaned.split(".")
        if len(parts) == 1:
            dollars_str = parts[0]
            cents_str = "00"
        elif len(parts) == 2:
            dollars_str, cents_str = parts
            if len(cents_str) == 1:
                cents_str = f"{cents_str}0"
            elif len(cents_str) != 2:
                raise ValueError(
                    f"Invalid decimal places in: '{amount_str}'"
                )
        else:
            raise ValueError(
                    f"Multiple decimal points in: '{amount_str}'"
            )

        if not dollars_str.isdigit() or not cents_str.isdigit():
            raise ValueError(
                f"Non-digit numeric content in: '{amount_str}'"
            )

        total_cents = int(dollars_str) * 100 + int(cents_str)

        if total_cents <= 0:
            raise ValueError(
                f"Parsed amount must be positive, got: {total_cents}"
            )

        return total_cents


