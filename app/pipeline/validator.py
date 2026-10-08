"""Post-extraction validation functions for parsed statement data.

Every validator in this module operates on canonical models and raises InvariantError
on the first violation it encounters.  Validators never silently skip or coerce data.
All calculations use integer cents.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence

from app.models.canonical import (
    BrokerageSummary,
    CanonicalStatement,
    CanonicalTransaction,
    CreditCardSummary,
    DepositorySummary,
    Holding,
    RawStatement,
    TransactionType,
)
from app.models.enums import AccountDomain, TransactionCategory, validate_category_sign
from app.models.exceptions import InvariantError

# ── Universal Reconciliation (All Domains) ──────────────────────────────


def validate_universal_reconciliation(
    statement: CanonicalStatement,
    transactions: Sequence[CanonicalTransaction],
    domain: AccountDomain,
    allow_out_of_cycle: bool = False,
) -> None:
    """Validate universal balance identities and invariants for any domain.

    Invariants:
    1. net_change_cents == closing_balance_cents - opening_balance_cents
    2. closing_balance_cents == opening_balance_cents + sum(transactions.amount_cents)
    3. sum(transactions.amount_cents) == net_change_cents
    4. If any balance_after_cents is present, all transactions must have it and
       match cumulative running sum.
    5. Dates: post_date must fall within [statement_start_date, statement_end_date]
       unless allow_out_of_cycle is True.
    6. Category and sign consistency: validate_category_sign for every transaction.

    Args:
        statement: Canonical statement record.
        transactions: Ordered list of canonical transactions.
        domain: Account domain for sign rules.
        allow_out_of_cycle: Whether out-of-cycle post dates are allowed by schema.

    Raises:
        InvariantError: On any arithmetic or invariant violation.
    """
    expected_net = statement.closing_balance_cents - statement.opening_balance_cents
    if statement.net_change_cents != expected_net:
        raise InvariantError(
            f"net_change_cents ({statement.net_change_cents}) does not match "
            f"closing ({statement.closing_balance_cents}) - opening ({statement.opening_balance_cents}) = {expected_net}"
        )

    sum_deltas = sum(txn.amount_cents for txn in transactions)
    expected_closing = statement.opening_balance_cents + sum_deltas

    if statement.closing_balance_cents != expected_closing:
        raise InvariantError(
            f"Balance reconciliation failure: closing_balance_cents ({statement.closing_balance_cents}) "
            f"!= opening_balance_cents ({statement.opening_balance_cents}) + sum(amount_cents) ({sum_deltas}) = {expected_closing}"
        )

    # Running balance validation
    has_any_running = any(t.balance_after_cents is not None for t in transactions)
    if has_any_running:
        running_sum = statement.opening_balance_cents
        for idx, txn in enumerate(transactions):
            if txn.balance_after_cents is None:
                raise InvariantError(
                    f"Partial running balances detected: transaction {idx} is missing balance_after_cents."
                )
            running_sum += txn.amount_cents
            if txn.balance_after_cents != running_sum:
                raise InvariantError(
                    f"Running balance mismatch at transaction {idx} ({txn.description!r}): "
                    f"expected {running_sum}, statement reports {txn.balance_after_cents}"
                )

    # Date ranges and category sign checks
    for idx, txn in enumerate(transactions):
        if not allow_out_of_cycle and not (
            statement.statement_start_date
            <= txn.post_date
            <= statement.statement_end_date
        ):
            raise InvariantError(
                f"Transaction {idx} post_date {txn.post_date} is outside statement period "
                f"[{statement.statement_start_date}, {statement.statement_end_date}]: {txn.description!r}"
            )
        validate_category_sign(domain, txn.transaction_category, txn.amount_cents)


# ── Depository Validation ───────────────────────────────────────────────


def validate_depository_reconciliation(
    statement: CanonicalStatement,
    summary: DepositorySummary,
    transactions: Sequence[CanonicalTransaction],
) -> None:
    """Assert depository-specific bucket equations and line item sums.

    Equations:
    1. opening + deposits + interest_paid - withdrawals - fees == closing
    2. deposits_cents + interest_paid_cents == sum of positive lines in credit categories
    3. withdrawals_cents + fees_cents == sum of absolute debits in debit categories
    4. Exact bucket matching for interest_paid and fees (net of fee_reversals).

    Args:
        statement: Canonical statement record.
        summary: Depository sidecar summary.
        transactions: Canonical transactions.

    Raises:
        InvariantError: If any bucket equation or line sum fails.
    """
    computed_closing = (
        statement.opening_balance_cents
        + summary.deposits_cents
        + summary.interest_paid_cents
        - summary.withdrawals_cents
        - summary.fees_cents
    )
    if computed_closing != statement.closing_balance_cents:
        raise InvariantError(
            f"Depository bucket equation failed: opening ({statement.opening_balance_cents}) "
            f"+ deposits ({summary.deposits_cents}) + interest ({summary.interest_paid_cents}) "
            f"- withdrawals ({summary.withdrawals_cents}) - fees ({summary.fees_cents}) "
            f"= {computed_closing}, statement reports {statement.closing_balance_cents}"
        )

    # Credit line sum vs bucket
    credit_cats = {
        TransactionCategory.DEPOSIT,
        TransactionCategory.INTEREST_PAID,
        TransactionCategory.OTHER_CREDIT,
    }
    actual_credits = sum(
        t.amount_cents for t in transactions if t.transaction_category in credit_cats
    )
    expected_credits = summary.deposits_cents + summary.interest_paid_cents
    if actual_credits != expected_credits:
        raise InvariantError(
            f"Depository deposits + interest bucket ({expected_credits}) does not match "
            f"credit lines sum ({actual_credits})"
        )

    # Debit line sum vs bucket (withdrawal, fee, fee_reversal, other_debit)
    actual_withdrawals = sum(
        -t.amount_cents
        for t in transactions
        if t.transaction_category
        in {TransactionCategory.WITHDRAWAL, TransactionCategory.OTHER_DEBIT}
    )
    actual_net_fees = sum(
        -t.amount_cents
        for t in transactions
        if t.transaction_category
        in {TransactionCategory.FEE, TransactionCategory.FEE_REVERSAL}
    )

    if actual_withdrawals != summary.withdrawals_cents:
        raise InvariantError(
            f"Withdrawals bucket ({summary.withdrawals_cents}) does not match "
            f"withdrawal lines sum ({actual_withdrawals})"
        )

    if actual_net_fees != summary.fees_cents:
        raise InvariantError(
            f"Fees bucket ({summary.fees_cents}) does not match net fee lines sum ({actual_net_fees})"
        )


# ── Credit Card Validation ──────────────────────────────────────────────


def validate_credit_card_reconciliation(
    statement: CanonicalStatement,
    summary: CreditCardSummary,
    transactions: Sequence[CanonicalTransaction],
) -> None:
    """Assert revolving credit equation and line bucket matches.

    Equations:
    1. new_balance == previous_balance + purchases + advances + balance_transfers + fees + interest - payments_credits
    2. opening_balance_cents == previous_balance_cents
    3. closing_balance_cents == new_balance_cents
    4. Each bucket matches the sum of corresponding transaction lines.

    Args:
        statement: Canonical statement record.
        summary: Credit card sidecar summary.
        transactions: Canonical transactions.

    Raises:
        InvariantError: If any equation or bucket sum fails.
    """
    if statement.opening_balance_cents != summary.previous_balance_cents:
        raise InvariantError(
            f"Statement opening balance ({statement.opening_balance_cents}) does not match "
            f"credit summary previous_balance_cents ({summary.previous_balance_cents})"
        )

    if statement.closing_balance_cents != summary.new_balance_cents:
        raise InvariantError(
            f"Statement closing balance ({statement.closing_balance_cents}) does not match "
            f"credit summary new_balance_cents ({summary.new_balance_cents})"
        )

    computed_new = (
        summary.previous_balance_cents
        + summary.purchases_cents
        + summary.cash_advances_cents
        + summary.balance_transfers_cents
        + summary.fees_charged_cents
        + summary.interest_charged_cents
        - summary.payments_credits_cents
    )
    if computed_new != summary.new_balance_cents:
        raise InvariantError(
            f"Credit card balance equation failed: previous ({summary.previous_balance_cents}) "
            f"+ purchases ({summary.purchases_cents}) + advances ({summary.cash_advances_cents}) "
            f"+ transfers ({summary.balance_transfers_cents}) + fees ({summary.fees_charged_cents}) "
            f"+ interest ({summary.interest_charged_cents}) - payments ({summary.payments_credits_cents}) "
            f"= {computed_new}, statement reports {summary.new_balance_cents}"
        )

    # Line item bucket matching
    actual_purchases = sum(
        t.amount_cents
        for t in transactions
        if t.transaction_category == TransactionCategory.PURCHASE
    )
    if actual_purchases != summary.purchases_cents:
        raise InvariantError(
            f"Purchases bucket ({summary.purchases_cents}) != purchase lines ({actual_purchases})"
        )

    actual_payments = sum(
        -t.amount_cents
        for t in transactions
        if t.transaction_category
        in {TransactionCategory.PAYMENT, TransactionCategory.CREDIT}
    )
    if actual_payments != summary.payments_credits_cents:
        raise InvariantError(
            f"Payments/credits bucket ({summary.payments_credits_cents}) != payment lines ({actual_payments})"
        )

    actual_advances = sum(
        t.amount_cents
        for t in transactions
        if t.transaction_category == TransactionCategory.CASH_ADVANCE
    )
    if actual_advances != summary.cash_advances_cents:
        raise InvariantError(
            f"Cash advances bucket ({summary.cash_advances_cents}) != advance lines ({actual_advances})"
        )

    actual_transfers = sum(
        t.amount_cents
        for t in transactions
        if t.transaction_category == TransactionCategory.BALANCE_TRANSFER
    )
    if actual_transfers != summary.balance_transfers_cents:
        raise InvariantError(
            f"Balance transfers bucket ({summary.balance_transfers_cents}) != transfer lines ({actual_transfers})"
        )

    actual_net_fees = sum(
        t.amount_cents
        for t in transactions
        if t.transaction_category
        in {TransactionCategory.FEE, TransactionCategory.FEE_REVERSAL}
    )
    if actual_net_fees != summary.fees_charged_cents:
        raise InvariantError(
            f"Fees charged bucket ({summary.fees_charged_cents}) != net fee lines ({actual_net_fees})"
        )

    actual_interest = sum(
        t.amount_cents
        for t in transactions
        if t.transaction_category == TransactionCategory.INTEREST_CHARGED
    )
    if actual_interest != summary.interest_charged_cents:
        raise InvariantError(
            f"Interest charged bucket ({summary.interest_charged_cents}) != interest lines ({actual_interest})"
        )


# ── Brokerage Validation ────────────────────────────────────────────────


def validate_brokerage_reconciliation(
    statement: CanonicalStatement,
    summary: BrokerageSummary,
    transactions: Sequence[CanonicalTransaction],
) -> None:
    """Assert brokerage cash identity and optional portfolio bridge.

    Equations:
    1. opening_cash + sum(cash deltas) == closing_cash
    2. Portfolio bridge (iff opening_portfolio is present):
       opening_portfolio + transfers_in - transfers_out + income_dividends + realized_gains + unrealized_gains == closing_portfolio

    Args:
        statement: Canonical statement record.
        summary: Brokerage sidecar summary.
        transactions: Canonical transactions.

    Raises:
        InvariantError: If cash identity or portfolio bridge fails.
    """
    if statement.opening_balance_cents != summary.opening_cash_cents:
        raise InvariantError(
            f"Statement opening balance ({statement.opening_balance_cents}) != opening_cash_cents ({summary.opening_cash_cents})"
        )

    if statement.closing_balance_cents != summary.closing_cash_cents:
        raise InvariantError(
            f"Statement closing balance ({statement.closing_balance_cents}) != closing_cash_cents ({summary.closing_cash_cents})"
        )

    # Optional portfolio bridge validation
    if summary.opening_portfolio_cents is not None:
        components = [
            summary.transfers_in_cents,
            summary.transfers_out_cents,
            summary.income_dividends_cents,
            summary.realized_gains_cents,
            summary.unrealized_gains_cents,
        ]
        if any(c is None for c in components):
            raise InvariantError(
                "Portfolio bridge is present but contains null components. All components must be signed cents."
            )

        computed_closing_portfolio = (
            summary.opening_portfolio_cents
            + (summary.transfers_in_cents or 0)
            - (summary.transfers_out_cents or 0)
            + (summary.income_dividends_cents or 0)
            + (summary.realized_gains_cents or 0)
            + (summary.unrealized_gains_cents or 0)
        )
        if computed_closing_portfolio != summary.closing_portfolio_cents:
            raise InvariantError(
                f"Portfolio bridge mismatch: computed closing {computed_closing_portfolio} "
                f"!= stated closing {summary.closing_portfolio_cents}"
            )


def validate_holdings_valuation(
    holdings: Sequence[Holding],
    stated_total_cents: int,
    period_start: datetime.date,
    period_end: datetime.date,
) -> None:
    """Assert printed holdings total equals the sum of position market values.

    Each ``as_of_date`` must fall inside the statement period. The first
    failure raises ``InvariantError``. Amounts stay integer cents.
    """
    total = 0
    for holding in holdings:
        if holding.as_of_date < period_start or holding.as_of_date > period_end:
            raise InvariantError(
                "Holding as_of_date "
                f"{holding.as_of_date.isoformat()} is outside the statement period."
            )
        total += holding.market_value_cents
    if total != stated_total_cents:
        raise InvariantError(
            f"Holdings valuation mismatch: sum {total} != stated total {stated_total_cents}"
        )


# ── Legacy Validators ───────────────────────────────────────────────────


def validate_date_continuity(statement: RawStatement) -> None:
    """Assert date ordering and range on legacy RawStatement."""
    period_start: datetime.date = statement.summary.period_start
    period_end: datetime.date = statement.summary.period_end

    if period_start > period_end:
        raise ValueError(
            f"Statement period is inverted: period_start={period_start} > period_end={period_end}"
        )

    previous_date: datetime.date | None = None
    for idx, txn in enumerate(statement.transactions):
        if txn.date < period_start or txn.date > period_end:
            raise ValueError(
                f"Transaction {idx} date {txn.date} is outside the statement period [{period_start}, {period_end}]: {txn.description!r}"
            )

        if previous_date is not None and txn.date < previous_date:
            raise ValueError(
                f"Transaction {idx} date {txn.date} precedes prior transaction date {previous_date} - records are not in chronological order: {txn.description!r}"
            )

        previous_date = txn.date


def validate_cash_balance(statement: RawStatement) -> None:
    """Assert cash balance on legacy RawStatement."""
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
            f"Cash balance mismatch: start ({statement.summary.start_balance_cents}) "
            f"+ credits ({total_credits}) - debits ({total_debits}) = {computed_end}, "
            f"but statement reports {stated_end} (difference: {difference} cents)"
        )
