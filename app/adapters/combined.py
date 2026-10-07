"""Combined multi-account statement adapter for Standard Bank.

Extracts aggregate statements containing multiple depository accounts (Checking and Savings),
emitting one statement row per account under the same raw_payload_id and run_id.
Enforces mutual exclusivity, per-account invariant balance reconciliation, and
combined total validation.
"""

from __future__ import annotations

import datetime
import logging
import re
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

from app.adapters.base import CombinedStatementAdapter
from app.extraction.core import (
    parse_currency_to_cents,
    validate_extraction_against_schemas,
)
from app.models.canonical import (
    Account,
    CanonicalStatement,
    CanonicalTransaction,
    DepositorySummary,
)
from app.models.enums import (
    AccountDomain,
    AccountType,
    CurrencyCode,
    TransactionCategory,
    validate_category_for_domain,
    validate_category_sign,
)
from app.models.exceptions import (
    InvariantError,
    MissingSectionError,
    TokenError,
)
from app.models.raw import RawExtraction
from app.models.schema import TableSchema
from app.pipeline.validator import (
    validate_depository_reconciliation,
    validate_universal_reconciliation,
)

logger = logging.getLogger(__name__)


class StandardCombinedDepositoryAdapter(CombinedStatementAdapter):
    """Adapter for Standard Bank multi-account combined statements."""

    adapter_id = "standard_combined_depository"
    adapter_version = "1.0.0"
    institution_id = "standard_bank"
    account_domain = AccountDomain.DEPOSITORY
    account_types = frozenset({AccountType.CHECKING, AccountType.SAVINGS})

    def matches(self, file_path: Path) -> bool:
        """Heuristic check on first page text ensuring mutual exclusivity."""
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
                return "COMBINED" in first_text or (
                    "CHECKING SUMMARY" in first_text and "SAVINGS SUMMARY" in first_text
                )
        except Exception:  # noqa: BLE001
            return False

    def declared_schemas(self) -> Sequence[TableSchema]:
        """Declared table schema and mandatory section anchors for combined statement."""
        return (
            TableSchema(
                name="checking_transactions",
                headers=("date", "description", "amount", "balance"),
                section_markers=(
                    "CHECKING ACCOUNT SUMMARY",
                    "CHECKING TRANSACTION DETAILS",
                ),
            ),
            TableSchema(
                name="savings_transactions",
                headers=("date", "description", "amount", "balance"),
                section_markers=(
                    "SAVINGS ACCOUNT SUMMARY",
                    "SAVINGS TRANSACTION DETAILS",
                ),
            ),
        )

    def parse_canonical(
        self, extraction: RawExtraction
    ) -> Sequence[
        tuple[
            Account,
            CanonicalStatement,
            DepositorySummary,
            list[CanonicalTransaction],
        ]
    ]:
        """Extract and validate one statement bundle per account, adhering to all invariants."""
        logger.info(
            "Parsing combined depository statement",
            extra={"run_id": str(extraction.run.run_id)},
        )

        # 1. Validate schemas and section anchors
        validate_extraction_against_schemas(extraction, self.declared_schemas())

        full_text = "\n".join(page.page_text for page in extraction.pages)

        # 2. Extract period dates
        period_match = re.search(
            r"Statement\s+Period:\s*(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})",
            full_text,
            re.IGNORECASE,
        )
        if not period_match:
            raise MissingSectionError("Could not locate statement period dates.")

        start_date = datetime.date.fromisoformat(period_match.group(1))
        end_date = datetime.date.fromisoformat(period_match.group(2))

        # 3. Optional shared combined total balance
        combined_total_cents: int | None = None
        comb_match = re.search(
            r"Combined\s+Total\s+Balance:\s*([^\n\r]+)", full_text, re.IGNORECASE
        )
        if comb_match:
            combined_total_cents = parse_currency_to_cents(
                comb_match.group(1).strip(), allow_zero=True
            )

        bundles: list[
            tuple[
                Account,
                CanonicalStatement,
                DepositorySummary,
                list[CanonicalTransaction],
            ]
        ] = []

        # 4. Parse Checking Account
        chk_mask_match = re.search(
            r"Checking\s+Account\s+Number:\s*([*\dX-]+)", full_text, re.IGNORECASE
        )
        if not chk_mask_match:
            raise MissingSectionError("Could not locate checking account number.")
        chk_mask = chk_mask_match.group(1)

        chk_account = Account(
            institution=self.institution_id,
            account_mask=chk_mask,
            account_domain=AccountDomain.DEPOSITORY,
            account_type=AccountType.CHECKING,
            currency=CurrencyCode.USD,
        )

        chk_opening, chk_closing, chk_summary = self._parse_account_summary(
            full_text, "Checking"
        )
        chk_statement = CanonicalStatement(
            account_id=chk_account.account_id,
            run_id=extraction.run.run_id,
            raw_payload_id=extraction.payload.raw_payload_id,
            statement_start_date=start_date,
            statement_end_date=end_date,
            opening_balance_cents=chk_opening,
            closing_balance_cents=chk_closing,
            net_change_cents=chk_closing - chk_opening,
        )
        chk_summary = chk_summary.model_copy(
            update={"statement_id": chk_statement.statement_id}
        )

        chk_transactions = self._parse_account_transactions(
            full_text,
            start_marker="CHECKING TRANSACTION DETAILS",
            end_marker="SAVINGS ACCOUNT SUMMARY",
            statement_id=chk_statement.statement_id,
            account_type=AccountType.CHECKING,
        )

        validate_universal_reconciliation(
            chk_statement, chk_transactions, AccountDomain.DEPOSITORY
        )
        validate_depository_reconciliation(chk_statement, chk_summary, chk_transactions)

        bundles.append((chk_account, chk_statement, chk_summary, chk_transactions))

        # 5. Parse Savings Account
        sav_mask_match = re.search(
            r"Savings\s+Account\s+Number:\s*([*\dX-]+)", full_text, re.IGNORECASE
        )
        if not sav_mask_match:
            raise MissingSectionError("Could not locate savings account number.")
        sav_mask = sav_mask_match.group(1)

        sav_account = Account(
            institution=self.institution_id,
            account_mask=sav_mask,
            account_domain=AccountDomain.DEPOSITORY,
            account_type=AccountType.SAVINGS,
            currency=CurrencyCode.USD,
        )

        sav_opening, sav_closing, sav_summary = self._parse_account_summary(
            full_text, "Savings"
        )
        sav_statement = CanonicalStatement(
            account_id=sav_account.account_id,
            run_id=extraction.run.run_id,
            raw_payload_id=extraction.payload.raw_payload_id,
            statement_start_date=start_date,
            statement_end_date=end_date,
            opening_balance_cents=sav_opening,
            closing_balance_cents=sav_closing,
            net_change_cents=sav_closing - sav_opening,
        )
        sav_summary = sav_summary.model_copy(
            update={"statement_id": sav_statement.statement_id}
        )

        sav_transactions = self._parse_account_transactions(
            full_text,
            start_marker="SAVINGS TRANSACTION DETAILS",
            end_marker="END OF STATEMENT",
            statement_id=sav_statement.statement_id,
            account_type=AccountType.SAVINGS,
        )

        validate_universal_reconciliation(
            sav_statement, sav_transactions, AccountDomain.DEPOSITORY
        )
        validate_depository_reconciliation(sav_statement, sav_summary, sav_transactions)

        bundles.append((sav_account, sav_statement, sav_summary, sav_transactions))

        # 6. Combined total balance validation (if declared/printed)
        if combined_total_cents is not None:
            sum_closings = chk_closing + sav_closing
            if combined_total_cents != sum_closings:
                logger.error(
                    "Combined total balance mismatch",
                    extra={
                        "combined_total_cents": combined_total_cents,
                        "sum_closings": sum_closings,
                    },
                )
                raise InvariantError(
                    f"Combined total balance mismatch: stated {combined_total_cents} cents "
                    f"!= sum of account closings {sum_closings} cents"
                )

        logger.info(
            "Successfully parsed combined depository statement",
            extra={"accounts_count": len(bundles)},
        )
        return bundles

    def _parse_account_summary(
        self, text: str, prefix: str
    ) -> tuple[int, int, DepositorySummary]:
        """Extract starting, ending, and bucket totals for a specific account prefix."""
        section_pattern = rf"{prefix}\s+Summary.*?(?=(?:Savings\s+Summary|CHECKING\s+ACCOUNT\s+SUMMARY|SAVINGS\s+ACCOUNT\s+SUMMARY|END\s+OF\s+STATEMENT|\Z))"
        sec_match = re.search(section_pattern, text, re.IGNORECASE | re.DOTALL)
        if not sec_match:
            raise MissingSectionError(f"Could not locate {prefix} Summary section.")
        sec_text = sec_match.group(0)

        # Starting balance
        start_m = re.search(
            r"Starting\s+Balance:\s*([^\n\r]+)", sec_text, re.IGNORECASE
        )
        if not start_m:
            raise MissingSectionError(f"Missing Starting Balance in {prefix} Summary.")
        opening_cents = parse_currency_to_cents(
            start_m.group(1).strip(), allow_zero=True
        )

        # Ending balance
        end_m = re.search(r"Ending\s+Balance:\s*([^\n\r]+)", sec_text, re.IGNORECASE)
        if not end_m:
            raise MissingSectionError(f"Missing Ending Balance in {prefix} Summary.")
        closing_cents = parse_currency_to_cents(end_m.group(1).strip(), allow_zero=True)

        # Buckets
        deposits_m = re.search(r"Deposits:\s*([^\n\r]+)", sec_text, re.IGNORECASE)
        deposits_cents = (
            parse_currency_to_cents(deposits_m.group(1).strip(), allow_zero=True)
            if deposits_m
            else 0
        )

        withdrawals_m = re.search(r"Withdrawals:\s*([^\n\r]+)", sec_text, re.IGNORECASE)
        withdrawals_cents = (
            parse_currency_to_cents(withdrawals_m.group(1).strip(), allow_zero=True)
            if withdrawals_m
            else 0
        )

        fees_m = re.search(r"Fees:\s*([^\n\r]+)", sec_text, re.IGNORECASE)
        fees_cents = (
            parse_currency_to_cents(fees_m.group(1).strip(), allow_zero=True)
            if fees_m
            else 0
        )

        interest_m = re.search(
            r"Interest\s+Paid:\s*([^\n\r]+)", sec_text, re.IGNORECASE
        )
        interest_paid_cents = (
            parse_currency_to_cents(interest_m.group(1).strip(), allow_zero=True)
            if interest_m
            else 0
        )

        summary = DepositorySummary(
            statement_id=uuid4(),
            deposits_cents=deposits_cents,
            withdrawals_cents=withdrawals_cents,
            fees_cents=fees_cents,
            interest_paid_cents=interest_paid_cents,
        )
        return opening_cents, closing_cents, summary

    def _parse_account_transactions(
        self,
        text: str,
        start_marker: str,
        end_marker: str,
        statement_id,
        account_type: AccountType,
    ) -> list[CanonicalTransaction]:
        """Extract transactions bounded between start_marker and end_marker."""
        if start_marker not in text:
            raise MissingSectionError(f"Mandatory marker {start_marker} missing.")

        after_start = text.split(start_marker, 1)[1]
        section_content = (
            after_start.split(end_marker, 1)[0]
            if end_marker in after_start
            else after_start
        )

        lines = [line.strip() for line in section_content.splitlines() if line.strip()]
        transactions: list[CanonicalTransaction] = []

        for line in lines:
            # Explicit skip: table header rows only (DATE... DESCRIPTION...).
            if line.upper().startswith("DATE") or (
                "DESCRIPTION" in line.upper() and "AMOUNT" in line.upper()
            ):
                continue
            parts = [p.strip() for p in re.split(r"\s{2,}|\t+|\|", line) if p.strip()]
            if len(parts) < 3:
                raise TokenError(
                    f"Malformed transaction row (need date, description, amount): {line!r}"
                )

            date_str = parts[0].strip()
            desc = parts[1].strip()
            amt_str = parts[2].strip()
            bal_str = parts[3].strip() if len(parts) >= 4 else None

            try:
                txn_date = datetime.date.fromisoformat(date_str)
            except ValueError as exc:
                raise TokenError(
                    f"Unparseable transaction date {date_str!r} in row: {line!r}"
                ) from exc

            has_explicit_sign = amt_str.startswith(("+", "-")) or (
                amt_str.startswith("(") and amt_str.endswith(")")
            )
            if not has_explicit_sign:
                raise TokenError(
                    "Ambiguous amount sign without polarity indicator "
                    f"(got {amt_str!r})."
                )

            amt_cents = parse_currency_to_cents(amt_str, allow_zero=False)
            bal_after_cents = (
                parse_currency_to_cents(bal_str, allow_zero=True) if bal_str else None
            )

            # Classify category
            desc_upper = desc.upper()
            if "FEE REVERSAL" in desc_upper:
                category = TransactionCategory.FEE_REVERSAL
            elif "FEE" in desc_upper:
                category = TransactionCategory.FEE
            elif "INTEREST" in desc_upper:
                category = TransactionCategory.INTEREST_PAID
            elif amt_cents > 0:
                category = TransactionCategory.DEPOSIT
            else:
                category = TransactionCategory.WITHDRAWAL

            validate_category_for_domain(AccountDomain.DEPOSITORY, category)
            validate_category_sign(AccountDomain.DEPOSITORY, category, amt_cents)

            transactions.append(
                CanonicalTransaction(
                    statement_id=statement_id,
                    post_date=txn_date,
                    description=desc,
                    amount_cents=amt_cents,
                    balance_after_cents=bal_after_cents,
                    transaction_category=category,
                )
            )

        return transactions
