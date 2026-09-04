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
        raise NotImplementedError(
            "SchwabAdapter._extract_summary() is not yet implemented. "
            "Add regex patterns to locate the account number, period dates, "
            "and opening/closing balances on page 1."
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
        raise NotImplementedError(
            "SchwabAdapter._parse_cents() is not yet implemented. "
            "Add string-cleaning and decimal-to-integer-cents conversion."
        )
