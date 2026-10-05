"""Canonical Pydantic models for normalised financial statement data.

All monetary values are stored strictly as **integer cents** (BIGINT in database,
int in Python) to eliminate floating-point arithmetic errors.  A value of 1050
represents $10.50.

All transaction amounts are **signed deltas** to the domain primary balance,
never floats, never zero.
"""

from __future__ import annotations

import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import (
    AccountDomain,
    AccountType,
    CurrencyCode,
    TransactionCategory,
    validate_account_type_for_domain,
)

# ── Canonical Domain Entities ──────────────────────────────────────────


class Account(BaseModel):
    """Canonical financial account entity."""

    model_config = ConfigDict(frozen=True, strict=True)

    account_id: UUID = Field(default_factory=uuid4)
    institution: str = Field(min_length=1)
    account_mask: str = Field(min_length=1)
    account_domain: AccountDomain
    account_type: AccountType
    currency: CurrencyCode = CurrencyCode.USD

    @model_validator(mode="after")
    def validate_type_domain(self) -> Account:
        validate_account_type_for_domain(self.account_domain, self.account_type)
        return self


class CanonicalStatement(BaseModel):
    """Canonical statement record bookending a period for an account."""

    model_config = ConfigDict(frozen=True, strict=True)

    statement_id: UUID = Field(default_factory=uuid4)
    account_id: UUID
    run_id: UUID
    raw_payload_id: UUID
    statement_start_date: datetime.date
    statement_end_date: datetime.date
    opening_balance_cents: int
    closing_balance_cents: int
    net_change_cents: int

    @model_validator(mode="after")
    def validate_invariants(self) -> CanonicalStatement:
        if self.statement_start_date > self.statement_end_date:
            raise ValueError(
                f"statement_start_date ({self.statement_start_date}) must be <= "
                f"statement_end_date ({self.statement_end_date})"
            )
        expected_change = self.closing_balance_cents - self.opening_balance_cents
        if self.net_change_cents != expected_change:
            raise ValueError(
                f"net_change_cents ({self.net_change_cents}) must equal "
                f"closing_balance_cents ({self.closing_balance_cents}) - "
                f"opening_balance_cents ({self.opening_balance_cents}) = {expected_change}"
            )
        return self


class DepositorySummary(BaseModel):
    """Sidecar summary for depository accounts (checking / savings)."""

    model_config = ConfigDict(frozen=True, strict=True)

    statement_id: UUID
    deposits_cents: int = Field(ge=0)
    withdrawals_cents: int = Field(ge=0)
    interest_paid_cents: int = Field(ge=0)
    fees_cents: int = Field(ge=0)


class CreditCardSummary(BaseModel):
    """Sidecar summary for revolving credit accounts (credit cards)."""

    model_config = ConfigDict(frozen=True, strict=True)

    statement_id: UUID
    previous_balance_cents: int
    payments_credits_cents: int = Field(ge=0)
    purchases_cents: int = Field(ge=0)
    cash_advances_cents: int = Field(ge=0)
    balance_transfers_cents: int = Field(ge=0)
    fees_charged_cents: int = Field(ge=0)
    interest_charged_cents: int = Field(ge=0)
    new_balance_cents: int
    minimum_payment_due_cents: int | None = None
    payment_due_date: datetime.date | None = None


class BrokerageSummary(BaseModel):
    """Sidecar summary for custodial / brokerage accounts."""

    model_config = ConfigDict(frozen=True, strict=True)

    statement_id: UUID
    opening_cash_cents: int
    closing_cash_cents: int
    opening_portfolio_cents: int | None = None
    closing_portfolio_cents: int | None = None
    realized_gains_cents: int | None = None
    unrealized_gains_cents: int | None = None
    income_dividends_cents: int | None = None
    transfers_in_cents: int | None = None
    transfers_out_cents: int | None = None

    @model_validator(mode="after")
    def validate_portfolio_bounds(self) -> BrokerageSummary:
        has_opening = self.opening_portfolio_cents is not None
        has_closing = self.closing_portfolio_cents is not None
        if has_opening != has_closing:
            raise ValueError(
                "opening_portfolio_cents and closing_portfolio_cents must both be present or both be None"
            )
        return self


class CanonicalTransaction(BaseModel):
    """Canonical transaction row with signed amount_cents delta to domain primary balance."""

    model_config = ConfigDict(frozen=True, strict=True)

    transaction_id: UUID = Field(default_factory=uuid4)
    statement_id: UUID
    post_date: datetime.date
    transaction_date: datetime.date | None = None
    amount_cents: int
    description: str = Field(min_length=1)
    transaction_category: TransactionCategory
    balance_after_cents: int | None = None

    @model_validator(mode="after")
    def validate_non_zero_amount(self) -> CanonicalTransaction:
        if self.amount_cents == 0:
            raise ValueError("amount_cents must be strictly non-zero")
        return self


# ── Legacy models (for backwards compatibility with existing Schwab tests) ──


class TransactionType(StrEnum):
    """Direction of a cash movement (legacy)."""

    CREDIT = "credit"
    DEBIT = "debit"


class CashTransaction(BaseModel):
    """A single cash-ledger entry on a brokerage statement (legacy)."""

    model_config = ConfigDict(frozen=True, strict=True)

    date: datetime.date
    description: str = Field(min_length=1)
    transaction_type: TransactionType
    amount_cents: int = Field(gt=0)
    category: str | None = None


class StatementSummary(BaseModel):
    """Aggregate envelope that bookends the transactions on a statement (legacy)."""

    model_config = ConfigDict(frozen=True, strict=True)

    account_number_masked: str = Field(min_length=1)
    period_start: datetime.date
    period_end: datetime.date
    start_balance_cents: int
    end_balance_cents: int


class RawStatement(BaseModel):
    """Complete parsed output for a single brokerage statement (legacy)."""

    model_config = ConfigDict(frozen=True, strict=True)

    source_file: str = Field(min_length=1)
    broker: str = Field(min_length=1)
    summary: StatementSummary
    transactions: list[CashTransaction]
