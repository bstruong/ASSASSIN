"""Post-extraction validation functions for parsed statement data.

Every validator in this module operates on the canonical models and
raises ``ValueError`` on the **first** violation it encounters.
Validators never silently skip or coerce data.
"""

from __future__ import annotations

import datetime

from app.models.canonical import RawStatement, TransactionType


def validate_date_continuity(statement: RawStatement) -> None:
    """Assert that transactions are ordered by date and fall within the statement period.

    Checks performed (in order):

    1. Every transaction date ``t.date`` satisfies
       ``summary.period_start <= t.date <= summary.period_end``.
    2. Transactions are in non-descending date order.  Two transactions
       on the same date are permitted (intra-day ordering is
       indeterminate on most statements).

    Args:
        statement: A fully parsed ``RawStatement`` to validate.

    Raises:
        ValueError: On the first ordering or range violation found.
    """
    period_start: datetime.date = statement.summary.period_start
    period_end: datetime.date = statement.summary.period_end

    if period_start > period_end:
        raise ValueError(
            f"Statement period is inverted: "
            f"period_start={period_start} > period_end={period_end}"
        )

    previous_date: datetime.date | None = None

    for idx, txn in enumerate(statement.transactions):
        if txn.date < period_start or txn.date > period_end:
            raise ValueError(
                f"Transaction {idx} date {txn.date} is outside the "
                f"statement period [{period_start}, {period_end}]: "
                f"{txn.description!r}"
            )

        if previous_date is not None and txn.date < previous_date:
            raise ValueError(
                f"Transaction {idx} date {txn.date} precedes prior "
                f"transaction date {previous_date} - records are not in "
                f"chronological order: {txn.description!r}"
            )

        previous_date = txn.date


def validate_cash_balance(statement: RawStatement) -> None:
    """Assert the fundamental cash-ledger invariant.

    The invariant is::

        start_balance_cents
        + sum(t.amount_cents for t in txns if t.transaction_type == CREDIT)
        - sum(t.amount_cents for t in txns if t.transaction_type == DEBIT)
        == end_balance_cents

    All arithmetic uses Python ``int``, so there are **no** floating-point
    rounding concerns.

    Args:
        statement: A fully parsed ``RawStatement`` to validate.

    Raises:
        ValueError: When the computed ending balance does not match the
            stated ``end_balance_cents``.
    """
    total_credits: int = sum(
        txn.amount_cents
        for txn in statement.transactions
        if txn.transaction_type == TransactionType.CREDIT
    )
    total_debits: int = sum(
        txn.amount_cents
        for txn in statement.transactions
        if txn.transaction_type == TransactionType.DEBIT
    )

    computed_end: int = (
        statement.summary.start_balance_cents + total_credits - total_debits
    )
    stated_end: int = statement.summary.end_balance_cents

    if computed_end != stated_end:
        difference: int = computed_end - stated_end
        raise ValueError(
            f"Cash balance mismatch: "
            f"start ({statement.summary.start_balance_cents}) "
            f"+ credits ({total_credits}) "
            f"- debits ({total_debits}) "
            f"= {computed_end}, but statement reports {stated_end} "
            f"(difference: {difference} cents)"
        )
