"""Closed enums and domain classification rules for the statement pipeline.

These types are the taxonomic foundation of the multi-domain pipeline.
Every adapter, validator, and persistence layer references them.
Enums are closed: unrecognised values fail at the boundary, never
propagate silently.

All domain↔type, domain↔category, and category↔sign mappings are
defined here as frozen data structures.  No other module may invent
ad-hoc membership checks.
"""

from __future__ import annotations

from enum import StrEnum

# ── Core enums ───────────────────────────────────────────────────────────


class AccountDomain(StrEnum):
    """Top-level classification of a financial account.

    Every account belongs to exactly one domain, which determines:

    - Valid ``AccountType`` values (see :data:`DOMAIN_ACCOUNT_TYPES`).
    - Valid ``TransactionCategory`` values (see :data:`DOMAIN_CATEGORIES`).
    - Sign semantics for ``amount_cents`` (see
      :data:`POSITIVE_DELTA_CATEGORIES` and
      :data:`NEGATIVE_DELTA_CATEGORIES`).
    - Which sidecar summary table a statement row populates.
    """

    DEPOSITORY = "depository"
    REVOLVING_CREDIT = "revolving_credit"
    CUSTODIAL_BROKERAGE = "custodial_brokerage"


class AccountType(StrEnum):
    """Sub-classification within a domain.

    The ``accounts`` table enforces ``(account_domain, account_type)``
    consistency via a CHECK constraint.  See :data:`DOMAIN_ACCOUNT_TYPES`
    for the valid pairings.
    """

    CHECKING = "checking"
    SAVINGS = "savings"
    CREDIT_CARD = "credit_card"
    BROKERAGE_CASH = "brokerage_cash"
    BROKERAGE_MARGIN = "brokerage_margin"


class TransactionCategory(StrEnum):
    """Semantic category for a single transaction line.

    This is the **union** of all domain-specific category sets.  A given
    domain accepts only a subset (see :data:`DOMAIN_CATEGORIES`).

    Each category has a **fixed sign rule** within its domain:
    ``amount_cents`` must be positive, must be negative, or either is
    allowed.  See :data:`POSITIVE_DELTA_CATEGORIES` and
    :data:`NEGATIVE_DELTA_CATEGORIES`.
    """

    # --- Depository only --------------------------------------------------
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    INTEREST_PAID = "interest_paid"

    # --- Revolving-credit only --------------------------------------------
    PURCHASE = "purchase"
    PAYMENT = "payment"
    CREDIT = "credit"
    CASH_ADVANCE = "cash_advance"
    BALANCE_TRANSFER = "balance_transfer"
    INTEREST_CHARGED = "interest_charged"

    # --- Brokerage only ---------------------------------------------------
    DIVIDEND = "dividend"
    INTEREST = "interest"
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"
    TRADE_CASH = "trade_cash"

    # --- Shared across domains --------------------------------------------
    FEE = "fee"
    FEE_REVERSAL = "fee_reversal"
    OTHER_CREDIT = "other_credit"
    OTHER_DEBIT = "other_debit"


class CurrencyCode(StrEnum):
    """Supported ISO 4217 currency codes.

    Only USD is supported.  Non-USD or unknown codes fail at ingestion.
    USD always has exactly two minor digits (cents).
    """

    USD = "USD"


class RunStatus(StrEnum):
    """Lifecycle state of an extraction run.

    The pipeline advances through these states in order.  A run may
    transition to ``FAILED`` from any earlier state.  Once ``FAILED``
    or ``CANONICAL_PERSISTED``, the run is terminal.
    """

    RAW_STORED = "raw_stored"
    EXTRACTED = "extracted"
    VALIDATED = "validated"
    CANONICAL_PERSISTED = "canonical_persisted"
    FAILED = "failed"


# ── Domain ↔ AccountType ────────────────────────────────────────────────

DOMAIN_ACCOUNT_TYPES: dict[AccountDomain, frozenset[AccountType]] = {
    AccountDomain.DEPOSITORY: frozenset({
        AccountType.CHECKING,
        AccountType.SAVINGS,
    }),
    AccountDomain.REVOLVING_CREDIT: frozenset({
        AccountType.CREDIT_CARD,
    }),
    AccountDomain.CUSTODIAL_BROKERAGE: frozenset({
        AccountType.BROKERAGE_CASH,
        AccountType.BROKERAGE_MARGIN,
    }),
}


def domain_for_account_type(account_type: AccountType) -> AccountDomain:
    """Return the unique domain that owns *account_type*.

    Every ``AccountType`` belongs to exactly one ``AccountDomain``.

    Args:
        account_type: The account type to look up.

    Returns:
        The owning ``AccountDomain``.

    Raises:
        ValueError: If *account_type* is not found in any domain
            (should be unreachable for valid enum members).
    """
    for domain, types in DOMAIN_ACCOUNT_TYPES.items():
        if account_type in types:
            return domain
    raise ValueError(f"AccountType {account_type!r} not found in any domain")


def validate_account_type_for_domain(
    domain: AccountDomain,
    account_type: AccountType,
) -> None:
    """Raise ``ValueError`` if *account_type* is not valid for *domain*.

    Args:
        domain: The account domain.
        account_type: The account type to validate.

    Raises:
        ValueError: If the pairing is invalid.
    """
    valid = DOMAIN_ACCOUNT_TYPES[domain]
    if account_type not in valid:
        raise ValueError(
            f"Account type {account_type!r} is not valid for domain "
            f"{domain!r}. Valid types: {sorted(t.value for t in valid)}"
        )


# ── Domain ↔ TransactionCategory ────────────────────────────────────────

DOMAIN_CATEGORIES: dict[AccountDomain, frozenset[TransactionCategory]] = {
    AccountDomain.DEPOSITORY: frozenset({
        TransactionCategory.DEPOSIT,
        TransactionCategory.WITHDRAWAL,
        TransactionCategory.INTEREST_PAID,
        TransactionCategory.FEE,
        TransactionCategory.FEE_REVERSAL,
        TransactionCategory.OTHER_CREDIT,
        TransactionCategory.OTHER_DEBIT,
    }),
    AccountDomain.REVOLVING_CREDIT: frozenset({
        TransactionCategory.PURCHASE,
        TransactionCategory.PAYMENT,
        TransactionCategory.CREDIT,
        TransactionCategory.CASH_ADVANCE,
        TransactionCategory.BALANCE_TRANSFER,
        TransactionCategory.FEE,
        TransactionCategory.INTEREST_CHARGED,
        TransactionCategory.FEE_REVERSAL,
    }),
    AccountDomain.CUSTODIAL_BROKERAGE: frozenset({
        TransactionCategory.DIVIDEND,
        TransactionCategory.INTEREST,
        TransactionCategory.TRANSFER_IN,
        TransactionCategory.TRANSFER_OUT,
        TransactionCategory.FEE,
        TransactionCategory.TRADE_CASH,
        TransactionCategory.OTHER_CREDIT,
        TransactionCategory.OTHER_DEBIT,
    }),
}


def validate_category_for_domain(
    domain: AccountDomain,
    category: TransactionCategory,
) -> None:
    """Raise ``ValueError`` if *category* is not valid for *domain*.

    Args:
        domain: The account domain.
        category: The transaction category to validate.

    Raises:
        ValueError: If the category is not in the domain's closed set.
    """
    valid = DOMAIN_CATEGORIES[domain]
    if category not in valid:
        raise ValueError(
            f"Category {category!r} is not valid for domain {domain!r}. "
            f"Valid categories: {sorted(c.value for c in valid)}"
        )


# ── Signed-delta rules ──────────────────────────────────────────────────
#
# Sign is the delta to the **domain primary balance**:
#
# - Depository (asset):   deposit adds (+), withdrawal removes (−).
# - Card (liability):     purchase adds to owed (+), payment reduces (−).
# - Brokerage (cash):     dividend adds (+), fee removes (−).
#
# A fee always "costs" the customer, but its sign depends on the domain's
# balance direction:
#   Depository/Brokerage:  fee = −N  (balance/cash decreases)
#   Card:                  fee = +N  (owed increases)

POSITIVE_DELTA_CATEGORIES: dict[AccountDomain, frozenset[TransactionCategory]] = {
    AccountDomain.DEPOSITORY: frozenset({
        TransactionCategory.DEPOSIT,
        TransactionCategory.INTEREST_PAID,
        TransactionCategory.FEE_REVERSAL,
        TransactionCategory.OTHER_CREDIT,
    }),
    AccountDomain.REVOLVING_CREDIT: frozenset({
        TransactionCategory.PURCHASE,
        TransactionCategory.CASH_ADVANCE,
        TransactionCategory.BALANCE_TRANSFER,
        TransactionCategory.FEE,
        TransactionCategory.INTEREST_CHARGED,
    }),
    AccountDomain.CUSTODIAL_BROKERAGE: frozenset({
        TransactionCategory.DIVIDEND,
        TransactionCategory.INTEREST,
        TransactionCategory.TRANSFER_IN,
        TransactionCategory.OTHER_CREDIT,
    }),
}

NEGATIVE_DELTA_CATEGORIES: dict[AccountDomain, frozenset[TransactionCategory]] = {
    AccountDomain.DEPOSITORY: frozenset({
        TransactionCategory.WITHDRAWAL,
        TransactionCategory.FEE,
        TransactionCategory.OTHER_DEBIT,
    }),
    AccountDomain.REVOLVING_CREDIT: frozenset({
        TransactionCategory.PAYMENT,
        TransactionCategory.CREDIT,
        TransactionCategory.FEE_REVERSAL,
    }),
    AccountDomain.CUSTODIAL_BROKERAGE: frozenset({
        TransactionCategory.TRANSFER_OUT,
        TransactionCategory.FEE,
        TransactionCategory.OTHER_DEBIT,
    }),
}

# Categories NOT in either POSITIVE or NEGATIVE for a domain allow
# either sign.  Currently only ``TRADE_CASH`` in ``CUSTODIAL_BROKERAGE``
# (sell → +N, buy → −N).


def validate_category_sign(
    domain: AccountDomain,
    category: TransactionCategory,
    amount_cents: int,
) -> None:
    """Raise if *amount_cents* sign contradicts *category* in *domain*.

    This enforces the signed-delta convention: each category has a
    required sign direction within its domain.  A purchase with a
    negative amount, or a payment with a positive amount, is a data
    error — not something to silently flip.

    Args:
        domain: The account domain.
        category: The transaction category.
        amount_cents: Signed integer cents (must be non-zero).

    Raises:
        TokenError: If the sign contradicts the category rule.
        ValueError: If *amount_cents* is zero.
    """
    from app.models.exceptions import TokenError

    if amount_cents == 0:
        raise ValueError("amount_cents must be non-zero")

    positive_cats = POSITIVE_DELTA_CATEGORIES.get(domain, frozenset())
    negative_cats = NEGATIVE_DELTA_CATEGORIES.get(domain, frozenset())

    if category in positive_cats and amount_cents < 0:
        raise TokenError(
            f"Category {category!r} in domain {domain!r} requires "
            f"positive amount_cents, got {amount_cents}"
        )
    if category in negative_cats and amount_cents > 0:
        raise TokenError(
            f"Category {category!r} in domain {domain!r} requires "
            f"negative amount_cents, got {amount_cents}"
        )
