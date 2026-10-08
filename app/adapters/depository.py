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

logger = logging.getLogger(__name__)

# Printed summary money, including a leading minus or accounting parentheses.
_SUMMARY_MONEY = r"([($]?-?[$]?[\d,]+\.\d{2}\)?)"


def _require_non_negative(field_name: str, cents: int) -> int:
    """Reject a printed negative on a magnitude bucket before the summary model."""
    if cents < 0:
        raise InvariantError(f"{field_name} must be >= 0")
    return cents


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
                if "STANDARD BANK" not in first_text:
                    return False
                # Multi-account aggregate statements are exclusively handled by combined adapters
                if "COMBINED" in first_text or (
                    "CHECKING SUMMARY" in first_text and "SAVINGS SUMMARY" in first_text
                ):
                    return False
                return "CHECKING" in first_text or "SAVINGS" in first_text
        except Exception:  # noqa: BLE001
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
                    continue
            raise TokenError(f"Invalid date format: {date_str}")

        start_date = parse_date(date_match.group(1))
        end_date = parse_date(date_match.group(2))

        return account_mask, account_type, start_date, end_date

    def parse_summary(
        self, extraction: RawExtraction
    ) -> tuple[int, int, DepositorySummary]:
        """Extract balance and bucket totals from summary section."""
        all_text = "\n".join(p.page_text for p in extraction.pages)

        def extract_signed_cents(label: str) -> int:
            match = re.search(
                rf"{label}[:\s]+{_SUMMARY_MONEY}",
                all_text,
                re.IGNORECASE,
            )
            if not match:
                raise MissingSectionError(f"Missing summary field: {label}")
            return parse_currency_to_cents(match.group(1).strip(), allow_zero=True)

        # Balances stay signed. Bucket totals are magnitudes and reject a printed minus.
        opening_cents = extract_signed_cents("Starting Balance")
        deposits_cents = _require_non_negative(
            "deposits_cents", extract_signed_cents("Deposits and Additions")
        )
        withdrawals_cents = _require_non_negative(
            "withdrawals_cents", extract_signed_cents("Withdrawals and Subtractions")
        )
        interest_paid_cents = _require_non_negative(
            "interest_paid_cents", extract_signed_cents("Interest Paid")
        )
        fees_cents = _require_non_negative(
            "fees_cents", extract_signed_cents("Fees Charged")
        )
        closing_cents = extract_signed_cents("Ending Balance")

        summary = DepositorySummary(
            statement_id=extraction.run.run_id,  # Will be reassigned to canonical statement_id if needed
            deposits_cents=deposits_cents,
            withdrawals_cents=withdrawals_cents,
            interest_paid_cents=interest_paid_cents,
            fees_cents=fees_cents,
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
                        continue
                raise TokenError(f"Invalid date format in row: {date_str}")

            post_date = parse_row_date(raw_date)

            # Ambiguous sign: single amount column with no +/-/() indicator.
            # Description-based sign inference is forbidden.
            has_explicit_sign = raw_amt.startswith(("+", "-")) or (
                raw_amt.startswith("(") and raw_amt.endswith(")")
            )
            if not has_explicit_sign:
                raise TokenError(
                    "Ambiguous amount sign without polarity indicator "
                    f"(got {raw_amt!r})."
                )

            desc_upper = raw_desc.upper()
            if "DEPOSIT" in desc_upper or "PAYROLL" in desc_upper:
                category = TransactionCategory.DEPOSIT
            elif "INTEREST" in desc_upper:
                category = TransactionCategory.INTEREST_PAID
            elif "FEE REFUND" in desc_upper or "REVERSAL" in desc_upper:
                category = TransactionCategory.FEE_REVERSAL
            elif "FEE" in desc_upper:
                category = TransactionCategory.FEE
            elif (
                "WITHDRAWAL" in desc_upper
                or "ATM" in desc_upper
                or "CHECK" in desc_upper
                or "PURCHASE" in desc_upper
            ):
                category = TransactionCategory.WITHDRAWAL
            else:
                category = TransactionCategory.OTHER_DEBIT

            amount_cents = parse_currency_to_cents(raw_amt)
            balance_after = parse_currency_to_cents(raw_bal) if raw_bal else None

            validate_category_for_domain(self.account_domain, category)
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
