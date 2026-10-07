"""Credit card statement adapter for revolving credit accounts."""

from __future__ import annotations

import datetime
import logging
import re
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from app.adapters.base import CreditCardStatementAdapter
from app.extraction.core import (
    parse_currency_to_cents,
    validate_table_against_schema,
)
from app.models.canonical import (
    CanonicalTransaction,
    CreditCardSummary,
)
from app.models.enums import (
    AccountType,
    TransactionCategory,
    validate_category_for_domain,
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


class StandardCreditCardAdapter(CreditCardStatementAdapter):
    """Adapter for revolving credit card statements."""

    adapter_id = "standard_credit_card"
    adapter_version = "1.0.0"
    institution_id = "Standard Card"
    account_types = frozenset({AccountType.CREDIT_CARD})

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
                return "STANDARD CARD" in first_text and (
                    "CREDIT CARD" in first_text or "CARD STATEMENT" in first_text
                )
        except Exception:  # noqa: BLE001
            return False

    def declared_schemas(self) -> Sequence[TableSchema]:
        """Declared table schema and mandatory section anchors."""
        return (
            TableSchema(
                name="card_transactions",
                headers=("date", "description", "amount"),
                section_markers=("ACCOUNT SUMMARY", "TRANSACTIONS"),
            ),
        )

    def parse_header(
        self, extraction: RawExtraction
    ) -> tuple[str, datetime.date, datetime.date]:
        """Extract account mask and statement period dates."""
        logger.info(
            "Parsing credit card header", extra={"run_id": str(extraction.run.run_id)}
        )
        p1_text = extraction.pages[0].page_text

        account_matches = re.findall(
            r"(?:Card|Account)\s*(?:Number)?[:\s#]+([*\dX-]+)", p1_text, re.IGNORECASE
        )
        unique_masks = sorted(set(account_matches))
        if len(unique_masks) > 1:
            logger.error(
                "Multiple card masks detected on page 1", extra={"masks": unique_masks}
            )
            raise AmbiguousAccountsError(
                f"Multiple card masks detected on page 1: {unique_masks}"
            )
        if not unique_masks:
            raise MissingSectionError("Could not locate card number on statement.")
        card_mask = unique_masks[0]

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

        return card_mask, start_date, end_date

    def parse_summary(
        self, extraction: RawExtraction
    ) -> tuple[int, int, CreditCardSummary]:
        """Extract revolving credit balances, printed buckets, and due date."""
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
            is_neg = val_str.startswith("-") or (
                val_str.startswith("(") and val_str.endswith(")")
            )
            clean_str = val_str.strip("()-")
            if not clean_str.startswith("$"):
                clean_str = f"${clean_str}"
            cents = parse_currency_to_cents(clean_str, allow_zero=True)
            return -abs(cents) if is_neg else abs(cents)

        prev_balance_cents = extract_cents(
            r"Previous Balance[^$\d]*([$]?-?[\d,]+\.\d{2})"
        )
        payments_credits_cents = extract_cents(
            r"Payments and Credits[^$\d]*([$]?[\d,]+\.\d{2})", default=0
        )
        purchases_cents = extract_cents(
            r"Purchases[^$\d]*([$]?[\d,]+\.\d{2})", default=0
        )
        cash_advances_cents = extract_cents(
            r"Cash Advances[^$\d]*([$]?[\d,]+\.\d{2})", default=0
        )
        balance_transfers_cents = extract_cents(
            r"Balance Transfers[^$\d]*([$]?[\d,]+\.\d{2})", default=0
        )
        fees_charged_cents = extract_cents(
            r"Fees Charged[^$\d]*([$]?[\d,]+\.\d{2})", default=0
        )
        interest_charged_cents = extract_cents(
            r"Interest Charged[^$\d]*([$]?[\d,]+\.\d{2})", default=0
        )
        new_balance_cents = extract_cents(r"New Balance[^$\d]*([$]?-?[\d,]+\.\d{2})")

        # Payment due date
        due_date: datetime.date | None = None
        due_match = re.search(
            r"Payment Due Date[:\s]+(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})",
            all_text,
            re.IGNORECASE,
        )
        if due_match:
            for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
                try:
                    due_date = datetime.datetime.strptime(  # noqa: DTZ007
                        due_match.group(1), fmt
                    ).date()
                    break
                except ValueError:
                    continue

        if self.requires_payment_due_date and due_date is None:
            raise MissingSectionError(
                "Payment due date is required on card statements."
            )

        # Minimum payment due (required — DB column is NOT NULL; do not invent zero)
        min_match = re.search(
            r"Minimum Payment Due[:\s]+([-(]?\$?[\d,]+\.\d{2}\)?)",
            all_text,
            re.IGNORECASE,
        )
        if not min_match:
            raise MissingSectionError(
                "Minimum payment due is required on card statements."
            )
        val_min = min_match.group(1).strip()
        is_min_neg = (
            val_min.startswith("-")
            or (val_min.startswith("(") and val_min.endswith(")"))
            or "$-" in val_min
        )
        clean_digits = re.sub(r"[^\d.]", "", val_min)
        clean_min = f"${clean_digits}"
        parsed_min = parse_currency_to_cents(clean_min, allow_zero=True)
        min_payment_cents = -abs(parsed_min) if is_min_neg else abs(parsed_min)

        # Bucket fields are unsigned magnitudes for the revolving equation.
        # Opening/closing liability balances keep printed signs (no abs rewrite).
        summary = CreditCardSummary(
            statement_id=extraction.run.run_id,
            payment_due_date=due_date,
            minimum_payment_due_cents=min_payment_cents,
            previous_balance_cents=prev_balance_cents,
            payments_credits_cents=abs(payments_credits_cents),
            purchases_cents=abs(purchases_cents),
            cash_advances_cents=abs(cash_advances_cents),
            balance_transfers_cents=abs(balance_transfers_cents),
            fees_charged_cents=abs(fees_charged_cents),
            interest_charged_cents=abs(interest_charged_cents),
            new_balance_cents=new_balance_cents,
        )

        return prev_balance_cents, new_balance_cents, summary

    def parse_transactions(
        self, extraction: RawExtraction, statement_id: UUID
    ) -> list[CanonicalTransaction]:
        """Parse card transaction rows with revolving signed deltas."""
        all_text = "\n".join(p.page_text for p in extraction.pages)
        txns: list[CanonicalTransaction] = []

        section_match = re.search(
            r"TRANSACTIONS\s*\n(.*?)(?:END OF STATEMENT|\Z)",
            all_text,
            re.IGNORECASE | re.DOTALL,
        )
        if not section_match:
            raise MissingSectionError("Could not locate TRANSACTIONS section block.")

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
            desc_upper = raw_desc.upper()

            # Ambiguous sign: single amount column requires explicit +/-/().
            has_explicit_sign = raw_amt.startswith(("+", "-")) or (
                raw_amt.startswith("(") and raw_amt.endswith(")")
            )
            if not has_explicit_sign:
                raise TokenError(
                    "Ambiguous amount sign without polarity indicator "
                    f"(got {raw_amt!r})."
                )

            # Classify category using regex boundaries to avoid false positives
            # (e.g. COFFEE containing FEE). Sign comes from the printed glyph only.
            if re.search(r"\bPAYMENT\b", desc_upper):
                category = TransactionCategory.PAYMENT
                expects_balance_increase = False
            elif re.search(r"\b(REFUND|RETURN)\b", desc_upper):
                category = TransactionCategory.CREDIT
                expects_balance_increase = False
            elif re.search(r"\b(FEE REFUND|FEE REVERSAL|WAIVER)\b", desc_upper):
                category = TransactionCategory.FEE_REVERSAL
                expects_balance_increase = False
            elif re.search(r"\bCASH ADVANCE\b", desc_upper):
                category = TransactionCategory.CASH_ADVANCE
                expects_balance_increase = True
            elif re.search(r"\bBALANCE TRANSFER\b", desc_upper):
                category = TransactionCategory.BALANCE_TRANSFER
                expects_balance_increase = True
            elif re.search(r"\bINTEREST\b", desc_upper):
                category = TransactionCategory.INTEREST_CHARGED
                expects_balance_increase = True
            elif re.search(r"\b(FEE|LATE|ANNUAL)\b", desc_upper):
                category = TransactionCategory.FEE
                expects_balance_increase = True
            else:
                category = TransactionCategory.PURCHASE
                expects_balance_increase = True

            amount_cents = parse_currency_to_cents(raw_amt)
            glyph_is_increase = amount_cents > 0
            if glyph_is_increase != expects_balance_increase:
                raise TokenError(
                    f"Category {category.value!r} disagrees with printed amount "
                    f"sign for {raw_amt!r}."
                )

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
