"""Canonical Pydantic models for normalised financial statement data.

All monetary values are stored as **integer cents** to eliminate
floating-point arithmetic errors.  A value of ``1050`` represents $10.50.

These models are the single source of truth that every brokerage adapter
must emit.  Down-stream pipeline stages (validation, enrichment,
reconciliation) operate exclusively on these types.
"""

from __future__ import annotations

import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class TransactionType(StrEnum):
    """Direction of a cash movement."""

    CREDIT = "credit"
    DEBIT = "debit"


class CashTransaction(BaseModel):
    """A single cash-ledger entry on a brokerage statement.

    Attributes:
        date: Settlement or posting date of the transaction.
        description: Raw description text as it appears on the statement.
        transaction_type: Whether this entry adds to (credit) or reduces
            (debit) the cash balance.
        amount_cents: Unsigned magnitude of the transaction in integer cents.
            Must be strictly positive.
        category: Optional brokerage-defined categorisation
            (e.g. ``"Dividends & Interest"``).
    """

    model_config = ConfigDict(frozen=True, strict=True)

    date: datetime.date
    description: str = Field(min_length=1)
    transaction_type: TransactionType
    amount_cents: int = Field(gt=0)
    category: str | None = None


class StatementSummary(BaseModel):
    """Aggregate envelope that bookends the transactions on a statement.

    The fundamental invariant is::

        start_balance_cents
        + sum(t.amount_cents for t in txns if t.transaction_type == CREDIT)
        - sum(t.amount_cents for t in txns if t.transaction_type == DEBIT)
        == end_balance_cents

    Attributes:
        account_number_masked: Masked account identifier (e.g. ``"****1234"``).
        period_start: First calendar day of the statement period (inclusive).
        period_end: Last calendar day of the statement period (inclusive).
        start_balance_cents: Opening cash balance in integer cents.  May be
            negative (margin).
        end_balance_cents: Closing cash balance in integer cents.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    account_number_masked: str = Field(min_length=1)
    period_start: datetime.date
    period_end: datetime.date
    start_balance_cents: int
    end_balance_cents: int


class RawStatement(BaseModel):
    """Complete parsed output for a single brokerage statement.

    Every adapter's ``parse()`` method must return an instance of this model.

    Attributes:
        source_file: Basename of the original PDF (no directory components).
        broker: Human-readable broker name (e.g. ``"Charles Schwab"``).
        summary: High-level period and balance information.
        transactions: Ordered list of cash transactions extracted from the
            statement, sorted by date ascending.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    source_file: str = Field(min_length=1)
    broker: str = Field(min_length=1)
    summary: StatementSummary
    transactions: list[CashTransaction]
