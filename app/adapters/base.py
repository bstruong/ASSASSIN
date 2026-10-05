"""Abstract base classes for statement adapters across all financial domains."""

from __future__ import annotations

import abc
import datetime
from collections.abc import Sequence
from pathlib import Path

from app.models.canonical import (
    Account,
    BrokerageSummary,
    CanonicalStatement,
    CanonicalTransaction,
    CreditCardSummary,
    DepositorySummary,
    RawStatement,
)
from app.models.enums import AccountDomain, AccountType
from app.models.raw import RawExtraction
from app.models.schema import TableSchema


class StatementAdapter(abc.ABC):
    """Universal contract that every domain-specific parser satisfies."""

    adapter_id: str
    adapter_version: str
    institution_id: str
    account_domain: AccountDomain
    account_types: frozenset[AccountType]

    @abc.abstractmethod
    def matches(self, file_path: Path) -> bool:
        """Return True if this adapter can parse the file at file_path (page-1 detection)."""

    def extract(self, file_path: Path) -> RawExtraction:
        """Extract uninterpreted raw pages, tokens, and integer bboxes from PDF."""
        from app.extraction.core import extract_pdf_to_raw

        return extract_pdf_to_raw(file_path, self.adapter_id, self.adapter_version)

    def declared_schemas(self) -> Sequence[TableSchema]:
        """Return declared table schemas and mandatory section markers."""
        return ()

    def parse(self, file_path: Path) -> RawStatement:
        """Legacy parse method returning RawStatement for backward compatibility."""
        raise NotImplementedError("Use parse_canonical or domain parse methods.")


class DepositoryStatementAdapter(StatementAdapter, abc.ABC):
    """Base adapter for depository accounts (checking and savings)."""

    account_domain = AccountDomain.DEPOSITORY

    @abc.abstractmethod
    def parse_header(
        self, extraction: RawExtraction
    ) -> tuple[str, AccountType, datetime.date, datetime.date]:
        """Extract (account_mask, account_type, statement_start_date, statement_end_date)."""

    @abc.abstractmethod
    def parse_summary(
        self, extraction: RawExtraction
    ) -> tuple[int, int, DepositorySummary]:
        """Extract (opening_balance_cents, closing_balance_cents, DepositorySummary)."""

    @abc.abstractmethod
    def parse_transactions(
        self, extraction: RawExtraction, statement_id
    ) -> list[CanonicalTransaction]:
        """Extract and map closed cash table into canonical transactions."""

    def parse_canonical(
        self, extraction: RawExtraction
    ) -> tuple[
        Account, CanonicalStatement, DepositorySummary, list[CanonicalTransaction]
    ]:
        """Orchestrate fail-loud parsing, schema checks, and balance reconciliation."""
        from app.extraction.core import validate_extraction_against_schemas
        from app.pipeline.validator import (
            validate_depository_reconciliation,
            validate_universal_reconciliation,
        )

        # 1. Closed schema and section marker validation
        validate_extraction_against_schemas(extraction, self.declared_schemas())

        # 2. Domain header parsing
        mask, acc_type, start_date, end_date = self.parse_header(extraction)
        account = Account(
            institution=self.institution_id,
            account_mask=mask,
            account_domain=AccountDomain.DEPOSITORY,
            account_type=acc_type,
        )

        # 3. Domain summary parsing
        opening_cents, closing_cents, summary = self.parse_summary(extraction)
        statement = CanonicalStatement(
            account_id=account.account_id,
            run_id=extraction.run.run_id,
            raw_payload_id=extraction.payload.raw_payload_id,
            statement_start_date=start_date,
            statement_end_date=end_date,
            opening_balance_cents=opening_cents,
            closing_balance_cents=closing_cents,
            net_change_cents=closing_cents - opening_cents,
        )

        summary = summary.model_copy(update={"statement_id": statement.statement_id})

        # 4. Domain transaction parsing
        transactions = self.parse_transactions(extraction, statement.statement_id)

        # 5. Invariant reconciliation
        validate_universal_reconciliation(
            statement, transactions, AccountDomain.DEPOSITORY
        )
        validate_depository_reconciliation(statement, summary, transactions)

        return account, statement, summary, transactions


class CreditCardStatementAdapter(StatementAdapter, abc.ABC):
    """Base adapter for revolving credit accounts (credit cards)."""

    account_domain = AccountDomain.REVOLVING_CREDIT
    account_types = frozenset({AccountType.CREDIT_CARD})
    requires_payment_due_date: bool = True

    @abc.abstractmethod
    def parse_header(
        self, extraction: RawExtraction
    ) -> tuple[str, datetime.date, datetime.date]:
        """Extract (account_mask, statement_start_date, statement_end_date)."""

    @abc.abstractmethod
    def parse_summary(
        self, extraction: RawExtraction
    ) -> tuple[int, int, CreditCardSummary]:
        """Extract (opening_balance_cents, closing_balance_cents, CreditCardSummary)."""

    @abc.abstractmethod
    def parse_transactions(
        self, extraction: RawExtraction, statement_id
    ) -> list[CanonicalTransaction]:
        """Extract credit card transactions with signed deltas."""

    def parse_canonical(
        self, extraction: RawExtraction
    ) -> tuple[
        Account, CanonicalStatement, CreditCardSummary, list[CanonicalTransaction]
    ]:
        """Orchestrate credit card parsing, schema verification, and reconciliation."""
        from app.extraction.core import validate_extraction_against_schemas
        from app.models.exceptions import MissingSectionError
        from app.pipeline.validator import (
            validate_credit_card_reconciliation,
            validate_universal_reconciliation,
        )

        validate_extraction_against_schemas(extraction, self.declared_schemas())

        mask, start_date, end_date = self.parse_header(extraction)
        account = Account(
            institution=self.institution_id,
            account_mask=mask,
            account_domain=AccountDomain.REVOLVING_CREDIT,
            account_type=AccountType.CREDIT_CARD,
        )

        opening_cents, closing_cents, summary = self.parse_summary(extraction)
        if self.requires_payment_due_date and summary.payment_due_date is None:
            raise MissingSectionError(
                "Payment due date is required on card statements."
            )
        statement = CanonicalStatement(
            account_id=account.account_id,
            run_id=extraction.run.run_id,
            raw_payload_id=extraction.payload.raw_payload_id,
            statement_start_date=start_date,
            statement_end_date=end_date,
            opening_balance_cents=opening_cents,
            closing_balance_cents=closing_cents,
            net_change_cents=closing_cents - opening_cents,
        )

        summary = summary.model_copy(update={"statement_id": statement.statement_id})

        transactions = self.parse_transactions(extraction, statement.statement_id)

        validate_universal_reconciliation(
            statement, transactions, AccountDomain.REVOLVING_CREDIT
        )
        validate_credit_card_reconciliation(statement, summary, transactions)

        return account, statement, summary, transactions


class InvestmentStatementAdapter(StatementAdapter, abc.ABC):
    """Base adapter for custodial / brokerage accounts."""

    account_domain = AccountDomain.CUSTODIAL_BROKERAGE
    account_types = frozenset(
        {AccountType.BROKERAGE_CASH, AccountType.BROKERAGE_MARGIN}
    )

    @abc.abstractmethod
    def parse_header(
        self, extraction: RawExtraction
    ) -> tuple[str, AccountType, datetime.date, datetime.date]:
        """Extract (account_mask, account_type, statement_start_date, statement_end_date)."""

    @abc.abstractmethod
    def parse_summary(
        self, extraction: RawExtraction
    ) -> tuple[int, int, BrokerageSummary]:
        """Extract (opening_cash_cents, closing_cash_cents, BrokerageSummary)."""

    @abc.abstractmethod
    def parse_transactions(
        self, extraction: RawExtraction, statement_id
    ) -> list[CanonicalTransaction]:
        """Extract investment cash/trade transactions."""

    def parse_canonical(
        self, extraction: RawExtraction
    ) -> tuple[
        Account, CanonicalStatement, BrokerageSummary, list[CanonicalTransaction]
    ]:
        """Orchestrate brokerage parsing and reconciliation."""
        from app.extraction.core import validate_extraction_against_schemas
        from app.pipeline.validator import (
            validate_brokerage_reconciliation,
            validate_universal_reconciliation,
        )

        validate_extraction_against_schemas(extraction, self.declared_schemas())

        mask, acc_type, start_date, end_date = self.parse_header(extraction)
        account = Account(
            institution=self.institution_id,
            account_mask=mask,
            account_domain=AccountDomain.CUSTODIAL_BROKERAGE,
            account_type=acc_type,
        )

        opening_cents, closing_cents, summary = self.parse_summary(extraction)
        statement = CanonicalStatement(
            account_id=account.account_id,
            run_id=extraction.run.run_id,
            raw_payload_id=extraction.payload.raw_payload_id,
            statement_start_date=start_date,
            statement_end_date=end_date,
            opening_balance_cents=opening_cents,
            closing_balance_cents=closing_cents,
            net_change_cents=closing_cents - opening_cents,
        )

        summary = summary.model_copy(update={"statement_id": statement.statement_id})

        transactions = self.parse_transactions(extraction, statement.statement_id)

        validate_universal_reconciliation(
            statement, transactions, AccountDomain.CUSTODIAL_BROKERAGE
        )
        validate_brokerage_reconciliation(statement, summary, transactions)

        return account, statement, summary, transactions


class CombinedStatementAdapter(StatementAdapter, abc.ABC):
    """Base adapter for multi-account aggregate statements emitting one statement per account."""

    @abc.abstractmethod
    def parse_canonical(
        self, extraction: RawExtraction
    ) -> Sequence[
        tuple[
            Account,
            CanonicalStatement,
            DepositorySummary | CreditCardSummary | BrokerageSummary,
            list[CanonicalTransaction],
        ]
    ]:
        """Extract and validate one statement bundle per account, adhering to all invariants."""
