"""Contract tests for the ASSASSIN taxonomy and exception hierarchy.

These tests verify the frozen contracts that every adapter, validator,
and persistence layer depends on.  They are intentionally exhaustive:
if an enum member is added or a sign rule changes, a test here must
break — forcing an explicit contract decision rather than a silent drift.

No pipeline logic, no adapters, no I/O.  Pure enum / mapping / exception
assertions.
"""

from __future__ import annotations

import pytest

from app.models.enums import (
    AccountDomain,
    AccountType,
    CurrencyCode,
    RunStatus,
    TransactionCategory,
    DOMAIN_ACCOUNT_TYPES,
    DOMAIN_CATEGORIES,
    NEGATIVE_DELTA_CATEGORIES,
    POSITIVE_DELTA_CATEGORIES,
    domain_for_account_type,
    validate_account_type_for_domain,
    validate_category_for_domain,
    validate_category_sign,
)
from app.models.exceptions import (
    AdapterRegistryError,
    AmbiguousAccountsError,
    AssassinError,
    InvariantError,
    MissingSectionError,
    SchemaDriftError,
    TokenError,
)


# =====================================================================
# §1  Enum membership — freeze the exact set of members
# =====================================================================


class TestAccountDomainMembers:
    """AccountDomain has exactly three members, no more, no fewer."""

    def test_member_count(self) -> None:
        assert len(AccountDomain) == 3

    @pytest.mark.parametrize(
        "member,value",
        [
            (AccountDomain.DEPOSITORY, "depository"),
            (AccountDomain.REVOLVING_CREDIT, "revolving_credit"),
            (AccountDomain.CUSTODIAL_BROKERAGE, "custodial_brokerage"),
        ],
    )
    def test_member_values(self, member: AccountDomain, value: str) -> None:
        assert member.value == value
        assert member == value  # StrEnum string equality


class TestAccountTypeMembers:
    """AccountType has exactly five members."""

    def test_member_count(self) -> None:
        assert len(AccountType) == 5

    @pytest.mark.parametrize(
        "member,value",
        [
            (AccountType.CHECKING, "checking"),
            (AccountType.SAVINGS, "savings"),
            (AccountType.CREDIT_CARD, "credit_card"),
            (AccountType.BROKERAGE_CASH, "brokerage_cash"),
            (AccountType.BROKERAGE_MARGIN, "brokerage_margin"),
        ],
    )
    def test_member_values(self, member: AccountType, value: str) -> None:
        assert member.value == value


class TestTransactionCategoryMembers:
    """TransactionCategory has exactly 18 members (union of all domains)."""

    EXPECTED_VALUES: frozenset[str] = frozenset({
        # Depository only
        "deposit", "withdrawal", "interest_paid",
        # Card only
        "purchase", "payment", "credit",
        "cash_advance", "balance_transfer", "interest_charged",
        # Brokerage only
        "dividend", "interest", "transfer_in",
        "transfer_out", "trade_cash",
        # Shared
        "fee", "fee_reversal", "other_credit", "other_debit",
    })

    def test_member_count(self) -> None:
        assert len(TransactionCategory) == 18

    def test_all_expected_values_present(self) -> None:
        actual = {m.value for m in TransactionCategory}
        assert actual == self.EXPECTED_VALUES

    def test_no_unexpected_values(self) -> None:
        actual = {m.value for m in TransactionCategory}
        unexpected = actual - self.EXPECTED_VALUES
        assert unexpected == set(), f"Unexpected categories: {unexpected}"


class TestCurrencyCodeMembers:
    """CurrencyCode has exactly one member: USD."""

    def test_member_count(self) -> None:
        assert len(CurrencyCode) == 1

    def test_usd_value(self) -> None:
        assert CurrencyCode.USD.value == "USD"


class TestRunStatusMembers:
    """RunStatus has exactly five lifecycle states."""

    def test_member_count(self) -> None:
        assert len(RunStatus) == 5

    @pytest.mark.parametrize(
        "member,value",
        [
            (RunStatus.RAW_STORED, "raw_stored"),
            (RunStatus.EXTRACTED, "extracted"),
            (RunStatus.VALIDATED, "validated"),
            (RunStatus.CANONICAL_PERSISTED, "canonical_persisted"),
            (RunStatus.FAILED, "failed"),
        ],
    )
    def test_member_values(self, member: RunStatus, value: str) -> None:
        assert member.value == value


# =====================================================================
# §2  Domain ↔ AccountType consistency
# =====================================================================


class TestDomainAccountTypeMapping:
    """DOMAIN_ACCOUNT_TYPES must cover every domain and every type exactly once."""

    def test_all_domains_covered(self) -> None:
        assert set(DOMAIN_ACCOUNT_TYPES.keys()) == set(AccountDomain)

    def test_every_account_type_in_exactly_one_domain(self) -> None:
        seen: dict[AccountType, AccountDomain] = {}
        for domain, types in DOMAIN_ACCOUNT_TYPES.items():
            for at in types:
                assert at not in seen, (
                    f"{at!r} appears in both {seen[at]!r} and {domain!r}"
                )
                seen[at] = domain
        assert set(seen.keys()) == set(AccountType), (
            f"Missing types: {set(AccountType) - set(seen.keys())}"
        )

    @pytest.mark.parametrize(
        "domain,account_type",
        [
            (AccountDomain.DEPOSITORY, AccountType.CHECKING),
            (AccountDomain.DEPOSITORY, AccountType.SAVINGS),
            (AccountDomain.REVOLVING_CREDIT, AccountType.CREDIT_CARD),
            (AccountDomain.CUSTODIAL_BROKERAGE, AccountType.BROKERAGE_CASH),
            (AccountDomain.CUSTODIAL_BROKERAGE, AccountType.BROKERAGE_MARGIN),
        ],
    )
    def test_valid_pairings(
        self, domain: AccountDomain, account_type: AccountType
    ) -> None:
        validate_account_type_for_domain(domain, account_type)  # no raise

    @pytest.mark.parametrize(
        "domain,account_type",
        [
            (AccountDomain.DEPOSITORY, AccountType.CREDIT_CARD),
            (AccountDomain.DEPOSITORY, AccountType.BROKERAGE_CASH),
            (AccountDomain.REVOLVING_CREDIT, AccountType.CHECKING),
            (AccountDomain.REVOLVING_CREDIT, AccountType.BROKERAGE_MARGIN),
            (AccountDomain.CUSTODIAL_BROKERAGE, AccountType.CHECKING),
            (AccountDomain.CUSTODIAL_BROKERAGE, AccountType.CREDIT_CARD),
        ],
    )
    def test_invalid_pairings_raise(
        self, domain: AccountDomain, account_type: AccountType
    ) -> None:
        with pytest.raises(ValueError, match="not valid for domain"):
            validate_account_type_for_domain(domain, account_type)


class TestDomainForAccountType:
    """domain_for_account_type reverse lookup returns the correct domain."""

    @pytest.mark.parametrize(
        "account_type,expected_domain",
        [
            (AccountType.CHECKING, AccountDomain.DEPOSITORY),
            (AccountType.SAVINGS, AccountDomain.DEPOSITORY),
            (AccountType.CREDIT_CARD, AccountDomain.REVOLVING_CREDIT),
            (AccountType.BROKERAGE_CASH, AccountDomain.CUSTODIAL_BROKERAGE),
            (AccountType.BROKERAGE_MARGIN, AccountDomain.CUSTODIAL_BROKERAGE),
        ],
    )
    def test_reverse_lookup(
        self, account_type: AccountType, expected_domain: AccountDomain
    ) -> None:
        assert domain_for_account_type(account_type) == expected_domain


# =====================================================================
# §3  Domain ↔ TransactionCategory consistency
# =====================================================================


class TestDomainCategoryMapping:
    """DOMAIN_CATEGORIES must cover every domain; every category in ≥1 domain."""

    def test_all_domains_covered(self) -> None:
        assert set(DOMAIN_CATEGORIES.keys()) == set(AccountDomain)

    def test_every_category_in_at_least_one_domain(self) -> None:
        all_categorised: set[TransactionCategory] = set()
        for cats in DOMAIN_CATEGORIES.values():
            all_categorised |= cats
        uncovered = set(TransactionCategory) - all_categorised
        assert uncovered == set(), f"Categories in no domain: {uncovered}"

    # --- Depository -------------------------------------------------------

    def test_depository_categories(self) -> None:
        expected = frozenset({
            TransactionCategory.DEPOSIT,
            TransactionCategory.WITHDRAWAL,
            TransactionCategory.INTEREST_PAID,
            TransactionCategory.FEE,
            TransactionCategory.FEE_REVERSAL,
            TransactionCategory.OTHER_CREDIT,
            TransactionCategory.OTHER_DEBIT,
        })
        assert DOMAIN_CATEGORIES[AccountDomain.DEPOSITORY] == expected

    # --- Revolving credit -------------------------------------------------

    def test_revolving_credit_categories(self) -> None:
        expected = frozenset({
            TransactionCategory.PURCHASE,
            TransactionCategory.PAYMENT,
            TransactionCategory.CREDIT,
            TransactionCategory.CASH_ADVANCE,
            TransactionCategory.BALANCE_TRANSFER,
            TransactionCategory.FEE,
            TransactionCategory.INTEREST_CHARGED,
            TransactionCategory.FEE_REVERSAL,
        })
        assert DOMAIN_CATEGORIES[AccountDomain.REVOLVING_CREDIT] == expected

    # --- Brokerage --------------------------------------------------------

    def test_brokerage_categories(self) -> None:
        expected = frozenset({
            TransactionCategory.DIVIDEND,
            TransactionCategory.INTEREST,
            TransactionCategory.TRANSFER_IN,
            TransactionCategory.TRANSFER_OUT,
            TransactionCategory.FEE,
            TransactionCategory.TRADE_CASH,
            TransactionCategory.OTHER_CREDIT,
            TransactionCategory.OTHER_DEBIT,
        })
        assert DOMAIN_CATEGORIES[AccountDomain.CUSTODIAL_BROKERAGE] == expected

    # --- Cross-domain: fee is shared --------------------------------------

    def test_fee_valid_in_all_domains(self) -> None:
        for domain in AccountDomain:
            assert TransactionCategory.FEE in DOMAIN_CATEGORIES[domain]

    # --- Cross-domain: purchase is card only ------------------------------

    def test_purchase_only_in_revolving_credit(self) -> None:
        for domain in AccountDomain:
            if domain == AccountDomain.REVOLVING_CREDIT:
                assert TransactionCategory.PURCHASE in DOMAIN_CATEGORIES[domain]
            else:
                assert TransactionCategory.PURCHASE not in DOMAIN_CATEGORIES[domain]

    # --- validate_category_for_domain -------------------------------------

    def test_valid_category_passes(self) -> None:
        validate_category_for_domain(
            AccountDomain.DEPOSITORY, TransactionCategory.DEPOSIT
        )

    def test_invalid_category_raises(self) -> None:
        with pytest.raises(ValueError, match="not valid for domain"):
            validate_category_for_domain(
                AccountDomain.DEPOSITORY, TransactionCategory.PURCHASE
            )


# =====================================================================
# §4  Signed-delta rules
# =====================================================================


class TestSignRuleCompleteness:
    """For each domain, POSITIVE ∪ NEGATIVE ∪ EITHER = DOMAIN_CATEGORIES.

    Also: POSITIVE ∩ NEGATIVE must be empty (no contradictions).
    """

    @pytest.mark.parametrize("domain", list(AccountDomain))
    def test_positive_and_negative_are_disjoint(
        self, domain: AccountDomain
    ) -> None:
        pos = POSITIVE_DELTA_CATEGORIES[domain]
        neg = NEGATIVE_DELTA_CATEGORIES[domain]
        overlap = pos & neg
        assert overlap == frozenset(), (
            f"Domain {domain!r} has categories in both positive and "
            f"negative sets: {overlap}"
        )

    @pytest.mark.parametrize("domain", list(AccountDomain))
    def test_positive_negative_either_covers_domain(
        self, domain: AccountDomain
    ) -> None:
        pos = POSITIVE_DELTA_CATEGORIES[domain]
        neg = NEGATIVE_DELTA_CATEGORIES[domain]
        covered = pos | neg
        # "Either-sign" categories are in DOMAIN but not in pos or neg.
        # The union of all three must equal DOMAIN_CATEGORIES.
        either = DOMAIN_CATEGORIES[domain] - covered
        assert pos | neg | either == DOMAIN_CATEGORIES[domain]

    @pytest.mark.parametrize("domain", list(AccountDomain))
    def test_sign_sets_are_subsets_of_domain(
        self, domain: AccountDomain
    ) -> None:
        assert POSITIVE_DELTA_CATEGORIES[domain] <= DOMAIN_CATEGORIES[domain]
        assert NEGATIVE_DELTA_CATEGORIES[domain] <= DOMAIN_CATEGORIES[domain]


class TestSignRuleSpecificCases:
    """Spot-check the sign semantics documented in the spec."""

    # --- Depository (asset): deposit +, withdrawal − ---------------------

    def test_depository_deposit_positive(self) -> None:
        validate_category_sign(
            AccountDomain.DEPOSITORY, TransactionCategory.DEPOSIT, 500
        )

    def test_depository_deposit_negative_fails(self) -> None:
        with pytest.raises(TokenError):
            validate_category_sign(
                AccountDomain.DEPOSITORY, TransactionCategory.DEPOSIT, -500
            )

    def test_depository_withdrawal_negative(self) -> None:
        validate_category_sign(
            AccountDomain.DEPOSITORY, TransactionCategory.WITHDRAWAL, -500
        )

    def test_depository_withdrawal_positive_fails(self) -> None:
        with pytest.raises(TokenError):
            validate_category_sign(
                AccountDomain.DEPOSITORY, TransactionCategory.WITHDRAWAL, 500
            )

    def test_depository_interest_paid_positive(self) -> None:
        validate_category_sign(
            AccountDomain.DEPOSITORY, TransactionCategory.INTEREST_PAID, 375
        )

    def test_depository_fee_negative(self) -> None:
        validate_category_sign(
            AccountDomain.DEPOSITORY, TransactionCategory.FEE, -2500
        )

    def test_depository_fee_reversal_positive(self) -> None:
        validate_category_sign(
            AccountDomain.DEPOSITORY, TransactionCategory.FEE_REVERSAL, 2500
        )

    # --- Card (liability): purchase +N (owed up), payment −N (owed down) -

    def test_card_purchase_positive(self) -> None:
        validate_category_sign(
            AccountDomain.REVOLVING_CREDIT, TransactionCategory.PURCHASE, 4999
        )

    def test_card_purchase_negative_fails(self) -> None:
        """THE key spec invariant: purchase with negative amount → TokenError."""
        with pytest.raises(TokenError):
            validate_category_sign(
                AccountDomain.REVOLVING_CREDIT,
                TransactionCategory.PURCHASE,
                -4999,
            )

    def test_card_payment_negative(self) -> None:
        validate_category_sign(
            AccountDomain.REVOLVING_CREDIT, TransactionCategory.PAYMENT, -10000
        )

    def test_card_payment_positive_fails(self) -> None:
        with pytest.raises(TokenError):
            validate_category_sign(
                AccountDomain.REVOLVING_CREDIT,
                TransactionCategory.PAYMENT,
                10000,
            )

    def test_card_fee_positive(self) -> None:
        """Card fee increases what's owed → positive."""
        validate_category_sign(
            AccountDomain.REVOLVING_CREDIT, TransactionCategory.FEE, 3900
        )

    def test_card_fee_reversal_negative(self) -> None:
        """Card fee reversal decreases what's owed → negative."""
        validate_category_sign(
            AccountDomain.REVOLVING_CREDIT,
            TransactionCategory.FEE_REVERSAL,
            -3900,
        )

    def test_card_interest_charged_positive(self) -> None:
        validate_category_sign(
            AccountDomain.REVOLVING_CREDIT,
            TransactionCategory.INTEREST_CHARGED,
            1523,
        )

    def test_card_cash_advance_positive(self) -> None:
        validate_category_sign(
            AccountDomain.REVOLVING_CREDIT,
            TransactionCategory.CASH_ADVANCE,
            50000,
        )

    def test_card_balance_transfer_positive(self) -> None:
        validate_category_sign(
            AccountDomain.REVOLVING_CREDIT,
            TransactionCategory.BALANCE_TRANSFER,
            200000,
        )

    def test_card_credit_negative(self) -> None:
        validate_category_sign(
            AccountDomain.REVOLVING_CREDIT, TransactionCategory.CREDIT, -1500
        )

    # --- Brokerage: fixed-sign categories --------------------------------

    def test_brokerage_dividend_positive(self) -> None:
        validate_category_sign(
            AccountDomain.CUSTODIAL_BROKERAGE,
            TransactionCategory.DIVIDEND,
            12550,
        )

    def test_brokerage_interest_positive(self) -> None:
        validate_category_sign(
            AccountDomain.CUSTODIAL_BROKERAGE,
            TransactionCategory.INTEREST,
            375,
        )

    def test_brokerage_transfer_in_positive(self) -> None:
        validate_category_sign(
            AccountDomain.CUSTODIAL_BROKERAGE,
            TransactionCategory.TRANSFER_IN,
            500000,
        )

    def test_brokerage_transfer_out_negative(self) -> None:
        validate_category_sign(
            AccountDomain.CUSTODIAL_BROKERAGE,
            TransactionCategory.TRANSFER_OUT,
            -500000,
        )

    def test_brokerage_fee_negative(self) -> None:
        validate_category_sign(
            AccountDomain.CUSTODIAL_BROKERAGE, TransactionCategory.FEE, -2500
        )

    # --- Brokerage: trade_cash allows either sign ------------------------

    def test_brokerage_trade_cash_positive(self) -> None:
        """Sell proceeds → positive cash delta."""
        validate_category_sign(
            AccountDomain.CUSTODIAL_BROKERAGE,
            TransactionCategory.TRADE_CASH,
            100000,
        )

    def test_brokerage_trade_cash_negative(self) -> None:
        """Buy cost → negative cash delta."""
        validate_category_sign(
            AccountDomain.CUSTODIAL_BROKERAGE,
            TransactionCategory.TRADE_CASH,
            -100000,
        )

    # --- Zero amount always fails ----------------------------------------

    @pytest.mark.parametrize("domain", list(AccountDomain))
    def test_zero_amount_raises_value_error(
        self, domain: AccountDomain
    ) -> None:
        # Pick any valid category for this domain.
        category = next(iter(DOMAIN_CATEGORIES[domain]))
        with pytest.raises(ValueError, match="non-zero"):
            validate_category_sign(domain, category, 0)


class TestFeeSignCrossDomain:
    """Fee has OPPOSITE sign in card vs depository/brokerage.

    This is the core asymmetry of liability vs asset balance domains:
    a fee always costs the customer, but the primary-balance delta
    direction depends on whether balance means "money I have" or
    "money I owe".
    """

    def test_depository_fee_must_be_negative(self) -> None:
        assert TransactionCategory.FEE in NEGATIVE_DELTA_CATEGORIES[
            AccountDomain.DEPOSITORY
        ]

    def test_card_fee_must_be_positive(self) -> None:
        assert TransactionCategory.FEE in POSITIVE_DELTA_CATEGORIES[
            AccountDomain.REVOLVING_CREDIT
        ]

    def test_brokerage_fee_must_be_negative(self) -> None:
        assert TransactionCategory.FEE in NEGATIVE_DELTA_CATEGORIES[
            AccountDomain.CUSTODIAL_BROKERAGE
        ]


# =====================================================================
# §5  Exception hierarchy
# =====================================================================


class TestExceptionHierarchy:
    """All pipeline exceptions descend from AssassinError."""

    ALL_EXCEPTIONS: list[type[AssassinError]] = [
        InvariantError,
        TokenError,
        SchemaDriftError,
        MissingSectionError,
        AmbiguousAccountsError,
        AdapterRegistryError,
    ]

    def test_assassin_error_is_exception(self) -> None:
        assert issubclass(AssassinError, Exception)

    @pytest.mark.parametrize("exc_cls", ALL_EXCEPTIONS)
    def test_subclass_of_assassin_error(
        self, exc_cls: type[AssassinError]
    ) -> None:
        assert issubclass(exc_cls, AssassinError)

    @pytest.mark.parametrize("exc_cls", ALL_EXCEPTIONS)
    def test_not_same_as_base(self, exc_cls: type[AssassinError]) -> None:
        assert exc_cls is not AssassinError

    @pytest.mark.parametrize("exc_cls", ALL_EXCEPTIONS)
    def test_can_raise_and_catch_as_assassin_error(
        self, exc_cls: type[AssassinError]
    ) -> None:
        with pytest.raises(AssassinError):
            raise exc_cls("test message")

    @pytest.mark.parametrize("exc_cls", ALL_EXCEPTIONS)
    def test_preserves_message(self, exc_cls: type[AssassinError]) -> None:
        msg = f"diagnostic detail for {exc_cls.__name__}"
        exc = exc_cls(msg)
        assert str(exc) == msg

    def test_all_subclasses_are_distinct(self) -> None:
        """No two exception classes are the same type."""
        assert len(set(self.ALL_EXCEPTIONS)) == len(self.ALL_EXCEPTIONS)

    def test_invariant_error_not_caught_as_token_error(self) -> None:
        """Sibling exceptions do not accidentally catch each other."""
        with pytest.raises(InvariantError):
            try:
                raise InvariantError("recon break")
            except TokenError:
                pytest.fail("InvariantError should not be caught as TokenError")


# =====================================================================
# §6  Exhaustive sign-rule parametrisation
# =====================================================================


def _all_positive_pairs() -> list[tuple[AccountDomain, TransactionCategory]]:
    """Every (domain, category) pair where amount_cents must be > 0."""
    pairs = []
    for domain, cats in POSITIVE_DELTA_CATEGORIES.items():
        for cat in cats:
            pairs.append((domain, cat))
    return pairs


def _all_negative_pairs() -> list[tuple[AccountDomain, TransactionCategory]]:
    """Every (domain, category) pair where amount_cents must be < 0."""
    pairs = []
    for domain, cats in NEGATIVE_DELTA_CATEGORIES.items():
        for cat in cats:
            pairs.append((domain, cat))
    return pairs


def _all_either_pairs() -> list[tuple[AccountDomain, TransactionCategory]]:
    """Every (domain, category) pair where either sign is valid."""
    pairs = []
    for domain in AccountDomain:
        pos = POSITIVE_DELTA_CATEGORIES[domain]
        neg = NEGATIVE_DELTA_CATEGORIES[domain]
        either = DOMAIN_CATEGORIES[domain] - pos - neg
        for cat in either:
            pairs.append((domain, cat))
    return pairs


class TestExhaustiveSignValidation:
    """Parametrise every (domain, category) pair against correct and wrong signs."""

    @pytest.mark.parametrize("domain,category", _all_positive_pairs())
    def test_positive_required_accepts_positive(
        self, domain: AccountDomain, category: TransactionCategory
    ) -> None:
        validate_category_sign(domain, category, 100)

    @pytest.mark.parametrize("domain,category", _all_positive_pairs())
    def test_positive_required_rejects_negative(
        self, domain: AccountDomain, category: TransactionCategory
    ) -> None:
        with pytest.raises(TokenError):
            validate_category_sign(domain, category, -100)

    @pytest.mark.parametrize("domain,category", _all_negative_pairs())
    def test_negative_required_accepts_negative(
        self, domain: AccountDomain, category: TransactionCategory
    ) -> None:
        validate_category_sign(domain, category, -100)

    @pytest.mark.parametrize("domain,category", _all_negative_pairs())
    def test_negative_required_rejects_positive(
        self, domain: AccountDomain, category: TransactionCategory
    ) -> None:
        with pytest.raises(TokenError):
            validate_category_sign(domain, category, 100)

    @pytest.mark.parametrize("domain,category", _all_either_pairs())
    def test_either_sign_accepts_positive(
        self, domain: AccountDomain, category: TransactionCategory
    ) -> None:
        validate_category_sign(domain, category, 100)

    @pytest.mark.parametrize("domain,category", _all_either_pairs())
    def test_either_sign_accepts_negative(
        self, domain: AccountDomain, category: TransactionCategory
    ) -> None:
        validate_category_sign(domain, category, -100)
