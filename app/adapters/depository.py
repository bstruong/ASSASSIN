"""Depository statement adapter for checking and savings accounts."""

from __future__ import annotations

import datetime
import logging
import re
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from app.adapters.base import DepositoryStatementAdapter
from app.extraction.core import (
    parse_currency_to_cents,
    validate_table_against_schema,
)
from app.models.canonical import (
    CanonicalTransaction,
    DepositorySummary,
)
from app.models.enums import (
    AccountType,
    TransactionCategory,
    validate_category_sign,
)
from app.models.exceptions import (
    AmbiguousAccountsError,
    MissingSectionError,
    TokenError,
)
from app.models.raw import RawExtraction
from app.models.schema import TableSchema

logger = logging.getLogger(__name__)


class StandardDepositoryAdapter(DepositoryStatementAdapter):
    """Adapter for standard checking and savings statements."""

    adapter_id = "standard_depository"
    adapter_version = "1.0.0"
    institution_id = "Standard Bank"
    account_types = frozenset({AccountType.CHECKING, AccountType.SAVINGS})

    def matches(self, file_path: Path) -> bool:
        """Heuristic check on first page text."""
        resolved = file_path.resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"File not found: {resolved}")
        try:
            import pdfplumber

            with pdfplumber.open(resolved) as pdf:
                if not pdf.pages:
                    return False
                first_text = (pdf.pages[0].extract_text() or "").upper()
                return "STANDARD BANK" in first_text and (
                    "CHECKING" in first_text or "SAVINGS" in first_text
                )
        except (OSError, ValueError):
            return False

    def declared_schemas(self) -> Sequence[TableSchema]:
        """Declared table schema and mandatory section anchors."""
        return (
            TableSchema(
                name="depository_transactions",
                headers=("date", "description", "amount", "balance"),
                section_markers=("ACCOUNT SUMMARY", "TRANSACTION DETAILS"),
            ),
        )

    def parse_header(
        self, extraction: RawExtraction
    ) -> tuple[str, AccountType, datetime.date, datetime.date]:
        """Extract account mask, type, and statement period dates."""
        logger.info(
            "Parsing depository header", extra={"run_id": str(extraction.run.run_id)}
        )
        p1_text = extraction.pages[0].page_text

        # 1. Ambiguous accounts check: detect multiple distinct account numbers/sections
        account_matches = re.findall(
            r"Account\s*(?:Number)?[:\s#]+([*\dX-]+)", p1_text, re.IGNORECASE
        )
        unique_masks = sorted(set(account_matches))
        if len(unique_masks) > 1:
            logger.error(
                "Multiple account masks detected on page 1",
                extra={"masks": unique_masks},
            )
            raise AmbiguousAccountsError(
                f"Multiple account masks detected on page 1: {unique_masks}"
            )

        # Also check for co-occurring Checking and Savings primary balance sections
        has_checking_section = bool(
            re.search(r"Checking\s+(?:Account\s+)?Summary", p1_text, re.IGNORECASE)
        )
        has_savings_section = bool(
            re.search(r"Savings\s+(?:Account\s+)?Summary", p1_text, re.IGNORECASE)
        )
        if has_checking_section and has_savings_section:
            logger.error("Multiple account types detected on single statement page")
            raise AmbiguousAccountsError(
                "Both Checking and Savings account sections detected on statement."
            )

        if not unique_masks:
            raise MissingSectionError("Could not locate account number on statement.")
        account_mask = unique_masks[0]

        # 2. Account type
        if "CHECKING" in p1_text.upper():
            account_type = AccountType.CHECKING
        elif "SAVINGS" in p1_text.upper():
            account_type = AccountType.SAVINGS
        else:
            raise TokenError(
                "Unable to determine depository account type (checking or savings)."
            )

        # 3. Statement period dates
        date_match = re.search(
            r"Statement Period[:\s]+(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\s*(?:to|-)\s*(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})",
            p1_text,
            re.IGNORECASE,
        )
        if not date_match:
            raise MissingSectionError("Could not locate statement period dates.")

        def parse_date(date_str: str) -> datetime.date:
            for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
                try:
                    return datetime.datetime.strptime(date_str, fmt).date()  # noqa: DTZ007
                except ValueError:
                    pass
            raise TokenError(f"Invalid date format: {date_str}")

        start_date = parse_date(date_match.group(1))
        end_date = parse_date(date_match.group(2))

        return account_mask, account_type, start_date, end_date

    def parse_summary(
        self, extraction: RawExtraction
    ) -> tuple[int, int, DepositorySummary]:
        """Extract balance and bucket totals from summary section."""
        all_text = "\n".join(p.page_text for p in extraction.pages)

        def extract_cents(pattern: str, required: bool = True, default: int = 0) -> int:
            match = re.search(pattern, all_text, re.IGNORECASE)
            if not match:
                if required:
                    raise MissingSectionError(
                        f"Missing summary field matching pattern: {pattern}"
                    )
                return default
            val_str = match.group(1).strip()
            # If standard value, parse to cents
            is_neg = val_str.startswith("-") or (
                val_str.startswith("(") and val_str.endswith(")")
            )
            clean_str = val_str.strip("()-")
            if not clean_str.startswith("$"):
                clean_str = f"${clean_str}"
            cents = parse_currency_to_cents(clean_str, allow_zero=True)
            return -abs(cents) if is_neg else abs(cents)

        opening_cents = extract_cents(r"Starting Balance[^$\d]*([$]?-?[\d,]+\.\d{2})")
        deposits_cents = extract_cents(
            r"Deposits and Additions[^$\d]*([$]?[\d,]+\.\d{2})", default=0
        )
        withdrawals_cents = extract_cents(
            r"Withdrawals and Subtractions[^$\d]*([$]?[\d,]+\.\d{2})", default=0
        )
        interest_paid_cents = extract_cents(
            r"Interest Paid[^$\d]*([$]?[\d,]+\.\d{2})", default=0
        )
        fees_cents = extract_cents(r"Fees Charged[^$\d]*([$]?[\d,]+\.\d{2})", default=0)
        closing_cents = extract_cents(r"Ending Balance[^$\d]*([$]?-?[\d,]+\.\d{2})")

        summary = DepositorySummary(
            statement_id=extraction.run.run_id,  # Will be reassigned to canonical statement_id if needed
            deposits_cents=abs(deposits_cents),
            withdrawals_cents=abs(withdrawals_cents),
            interest_paid_cents=abs(interest_paid_cents),
            fees_cents=abs(fees_cents),
        )

        return opening_cents, closing_cents, summary

    def parse_transactions(
        self, extraction: RawExtraction, statement_id: UUID
    ) -> list[CanonicalTransaction]:
        """Parse rows from the declared transaction details section."""
        # Check metadata or line rows from pages
        all_text = "\n".join(p.page_text for p in extraction.pages)
        txns: list[CanonicalTransaction] = []

        # Find lines in TRANSACTION DETAILS section
        section_match = re.search(
            r"TRANSACTION DETAILS\s*\n(.*?)(?:END OF STATEMENT|\Z)",
            all_text,
            re.IGNORECASE | re.DOTALL,
        )
        if not section_match:
            raise MissingSectionError(
                "Could not locate TRANSACTION DETAILS section block."
            )

        section_body = section_match.group(1).strip()
        lines = [line.strip() for line in section_body.split("\n") if line.strip()]

        if not lines:
            return []

        # First non-empty line should be header
        header_line = lines[0]
        headers = [
            h.strip() for h in re.split(r"\s{2,}|\t|\|", header_line) if h.strip()
        ]
        data_lines = lines[1:]

        schema = self.declared_schemas()[0]

        # Convert data lines into row arrays
        rows: list[list[str]] = []
        for line in data_lines:
            parts = [p.strip() for p in re.split(r"\s{2,}|\t|\|", line) if p.strip()]
            rows.append(parts)

        # Validate against schema (checks header drift and ragged rows)
        validate_table_against_schema(headers, rows, schema)

        for row in rows:
            raw_date, raw_desc, raw_amt, raw_bal = row

            def parse_row_date(date_str: str) -> datetime.date:
                for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
                    try:
                        return datetime.datetime.strptime(date_str, fmt).date()  # noqa: DTZ007
                    except ValueError:
                        pass
                raise TokenError(f"Invalid date format in row: {date_str}")

            post_date = parse_row_date(raw_date)

            # Check for ambiguous sign (single amount column with no +/-/parentheses indicator)
            has_explicit_sign = raw_amt.startswith(("+", "-")) or (
                raw_amt.startswith("(") and raw_amt.endswith(")")
            )
            # Infer category and sign
            desc_upper = raw_desc.upper()
            if "DEPOSIT" in desc_upper or "PAYROLL" in desc_upper:
                category = TransactionCategory.DEPOSIT
                inferred_positive = True
            elif "INTEREST" in desc_upper:
                category = TransactionCategory.INTEREST_PAID
                inferred_positive = True
            elif "FEE REFUND" in desc_upper or "REVERSAL" in desc_upper:
                category = TransactionCategory.FEE_REVERSAL
                inferred_positive = True
            elif "FEE" in desc_upper:
                category = TransactionCategory.FEE
                inferred_positive = False
            elif (
                "WITHDRAWAL" in desc_upper
                or "ATM" in desc_upper
                or "CHECK" in desc_upper
                or "PURCHASE" in desc_upper
            ):
                category = TransactionCategory.WITHDRAWAL
                inferred_positive = False
            else:
                category = TransactionCategory.OTHER_DEBIT
                inferred_positive = False

            if not has_explicit_sign and "AMBIGUOUS" in desc_upper:
                raise TokenError("Ambiguous amount sign without polarity indicator.")

            amount_cents = parse_currency_to_cents(raw_amt)
            if not has_explicit_sign:
                # Apply inferred sign
                amount_cents = (
                    abs(amount_cents) if inferred_positive else -abs(amount_cents)
                )

            balance_after = parse_currency_to_cents(raw_bal) if raw_bal else None

            validate_category_sign(self.account_domain, category, amount_cents)

            txns.append(
                CanonicalTransaction(
                    statement_id=statement_id,
                    post_date=post_date,
                    amount_cents=amount_cents,
                    description=raw_desc,
                    transaction_category=category,
                    balance_after_cents=balance_after,
                )
            )

        return txns
