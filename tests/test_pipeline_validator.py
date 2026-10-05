"""Tests for pipeline validator invariant checks across all domains."""

from __future__ import annotations

import datetime
from uuid import uuid4

import pytest

from app.models.canonical import (
    BrokerageSummary,
    CanonicalStatement,
    CanonicalTransaction,
    CreditCardSummary,
    DepositorySummary,
)
from app.models.enums import AccountDomain, TransactionCategory
from app.models.exceptions import InvariantError, TokenError
from app.pipeline.validator import (
    validate_brokerage_reconciliation,
    validate_credit_card_reconciliation,
    validate_depository_reconciliation,
    validate_universal_reconciliation,
)


def make_statement(
    opening: int = 1000,
    closing: int = 1500,
    start: datetime.date = datetime.date(2025, 1, 1),
    end: datetime.date = datetime.date(2025, 1, 31),
) -> CanonicalStatement:
    return CanonicalStatement(
        account_id=uuid4(),
        run_id=uuid4(),
        raw_payload_id=uuid4(),
        statement_start_date=start,
        statement_end_date=end,
        opening_balance_cents=opening,
        closing_balance_cents=closing,
        net_change_cents=closing - opening,
    )


class TestUniversalReconciliation:
    def test_net_change_mismatch_raises(self) -> None:
        stmt = CanonicalStatement.model_construct(
            account_id=uuid4(),
            run_id=uuid4(),
            raw_payload_id=uuid4(),
            statement_start_date=datetime.date(2025, 1, 1),
            statement_end_date=datetime.date(2025, 1, 31),
            opening_balance_cents=1000,
            closing_balance_cents=1500,
            net_change_cents=600,  # Corrupted net change
        )
        txns = [
            CanonicalTransaction(
                statement_id=stmt.statement_id,
                post_date=datetime.date(2025, 1, 15),
                amount_cents=500,
                description="Deposit",
                transaction_category=TransactionCategory.DEPOSIT,
            )
        ]
        with pytest.raises(InvariantError, match="net_change_cents"):
            validate_universal_reconciliation(stmt, txns, AccountDomain.DEPOSITORY)

    def test_sum_deltas_mismatch_raises(self) -> None:
        stmt = make_statement(opening=1000, closing=1500)
        # Transactions sum to 400 != 500
        txns = [
            CanonicalTransaction(
                statement_id=stmt.statement_id,
                post_date=datetime.date(2025, 1, 15),
                amount_cents=400,
                description="Deposit",
                transaction_category=TransactionCategory.DEPOSIT,
            )
        ]
        with pytest.raises(InvariantError, match="Balance reconciliation failure"):
            validate_universal_reconciliation(stmt, txns, AccountDomain.DEPOSITORY)

    def test_partial_running_balances_raises(self) -> None:
        stmt = make_statement(opening=1000, closing=2000)
        txns = [
            CanonicalTransaction(
                statement_id=stmt.statement_id,
                post_date=datetime.date(2025, 1, 10),
                amount_cents=500,
                description="Deposit 1",
                transaction_category=TransactionCategory.DEPOSIT,
                balance_after_cents=1500,
            ),
            CanonicalTransaction(
                statement_id=stmt.statement_id,
                post_date=datetime.date(2025, 1, 20),
                amount_cents=500,
                description="Deposit 2",
                transaction_category=TransactionCategory.DEPOSIT,
                balance_after_cents=None,  # Missing running balance!
            ),
        ]
        with pytest.raises(InvariantError, match="Partial running balances detected"):
            validate_universal_reconciliation(stmt, txns, AccountDomain.DEPOSITORY)

    def test_transaction_date_outside_period_raises(self) -> None:
        stmt = make_statement(
            opening=1000,
            closing=1500,
            start=datetime.date(2025, 1, 1),
            end=datetime.date(2025, 1, 31),
        )
        txns = [
            CanonicalTransaction(
                statement_id=stmt.statement_id,
                post_date=datetime.date(2025, 2, 5),
                amount_cents=500,
                description="Late Deposit",
                transaction_category=TransactionCategory.DEPOSIT,
            )
        ]
        with pytest.raises(InvariantError, match="outside statement period"):
            validate_universal_reconciliation(stmt, txns, AccountDomain.DEPOSITORY)

    def test_universal_reconciliation_happy_path(self) -> None:
        start_date = datetime.date(2025, 1, 1)
        end_date = datetime.date(2025, 1, 31)
        stmt = make_statement(
            opening=1000, closing=2500, start=start_date, end=end_date
        )
        txns = [
            CanonicalTransaction(
                statement_id=stmt.statement_id,
                post_date=start_date,
                amount_cents=500,
                description="Deposit 1",
                transaction_category=TransactionCategory.DEPOSIT,
                balance_after_cents=1500,
            ),
            CanonicalTransaction(
                statement_id=stmt.statement_id,
                post_date=end_date,
                amount_cents=1000,
                description="Deposit 2",
                transaction_category=TransactionCategory.DEPOSIT,
                balance_after_cents=2500,
            ),
        ]
        validate_universal_reconciliation(stmt, txns, AccountDomain.DEPOSITORY)

    def test_universal_reconciliation_allow_out_of_cycle(self) -> None:
        stmt = make_statement(
            opening=1000,
            closing=1500,
            start=datetime.date(2025, 1, 1),
            end=datetime.date(2025, 1, 31),
        )
        txns = [
            CanonicalTransaction(
                statement_id=stmt.statement_id,
                post_date=datetime.date(2025, 2, 5),
                amount_cents=500,
                description="Out of cycle Deposit",
                transaction_category=TransactionCategory.DEPOSIT,
            )
        ]
        validate_universal_reconciliation(
            stmt, txns, AccountDomain.DEPOSITORY, allow_out_of_cycle=True
        )

    def test_universal_reconciliation_running_balance_mismatch_raises(self) -> None:
        stmt = make_statement(opening=1000, closing=2000)
        txns = [
            CanonicalTransaction(
                statement_id=stmt.statement_id,
                post_date=datetime.date(2025, 1, 10),
                amount_cents=1000,
                description="Deposit",
                transaction_category=TransactionCategory.DEPOSIT,
                balance_after_cents=1999,
            )
        ]
        with pytest.raises(InvariantError, match="Running balance mismatch"):
            validate_universal_reconciliation(stmt, txns, AccountDomain.DEPOSITORY)

    def test_universal_reconciliation_invalid_category_sign_raises(self) -> None:
        stmt = make_statement(opening=1000, closing=500)
        txn = CanonicalTransaction.model_construct(
            transaction_id=uuid4(),
            statement_id=stmt.statement_id,
            post_date=datetime.date(2025, 1, 15),
            amount_cents=-500,
            description="Bad Deposit",
            transaction_category=TransactionCategory.DEPOSIT,
            balance_after_cents=None,
        )
        with pytest.raises(TokenError, match="requires positive amount_cents"):
            validate_universal_reconciliation(stmt, [txn], AccountDomain.DEPOSITORY)


class TestDepositoryReconciliation:
    def test_depository_credits_bucket_mismatch_raises(self) -> None:
        stmt = make_statement(opening=1000, closing=2000)
        # Transactions sum to 800, but summary claims 1000 deposits
        txns = [
            CanonicalTransaction(
                statement_id=stmt.statement_id,
                post_date=datetime.date(2025, 1, 10),
                amount_cents=800,
                description="Deposit",
                transaction_category=TransactionCategory.DEPOSIT,
            )
        ]
        summary = DepositorySummary(
            statement_id=stmt.statement_id,
            deposits_cents=1000,
            withdrawals_cents=0,
            interest_paid_cents=0,
            fees_cents=0,
        )
        with pytest.raises(InvariantError, match="deposits \\+ interest bucket"):
            validate_depository_reconciliation(stmt, summary, txns)

    def test_depository_withdrawals_bucket_mismatch_raises(self) -> None:
        stmt = make_statement(opening=2000, closing=1000)
        # Transactions sum to -800, but summary claims 1000 withdrawals
        txns = [
            CanonicalTransaction(
                statement_id=stmt.statement_id,
                post_date=datetime.date(2025, 1, 10),
                amount_cents=-800,
                description="ATM Withdrawal",
                transaction_category=TransactionCategory.WITHDRAWAL,
            )
        ]
        summary = DepositorySummary(
            statement_id=stmt.statement_id,
            deposits_cents=0,
            withdrawals_cents=1000,
            interest_paid_cents=0,
            fees_cents=0,
        )
        with pytest.raises(InvariantError, match="Withdrawals bucket"):
            validate_depository_reconciliation(stmt, summary, txns)

    def test_depository_fees_bucket_mismatch_raises(self) -> None:
        stmt = make_statement(opening=1000, closing=900)
        # Transactions sum to -50, but summary claims 100 fees
        txns = [
            CanonicalTransaction(
                statement_id=stmt.statement_id,
                post_date=datetime.date(2025, 1, 10),
                amount_cents=-50,
                description="Monthly Fee",
                transaction_category=TransactionCategory.FEE,
            )
        ]
        summary = DepositorySummary(
            statement_id=stmt.statement_id,
            deposits_cents=0,
            withdrawals_cents=0,
            interest_paid_cents=0,
            fees_cents=100,
        )
        with pytest.raises(InvariantError, match="Fees bucket"):
            validate_depository_reconciliation(stmt, summary, txns)


class TestCreditCardReconciliation:
    def test_card_opening_balance_mismatch_raises(self) -> None:
        stmt = make_statement(opening=1000, closing=1500)
        summary = CreditCardSummary(
            statement_id=stmt.statement_id,
            previous_balance_cents=2000,  # Mismatch with opening
            payments_credits_cents=0,
            purchases_cents=500,
            cash_advances_cents=0,
            balance_transfers_cents=0,
            fees_charged_cents=0,
            interest_charged_cents=0,
            new_balance_cents=1500,
            minimum_payment_due_cents=25,
            payment_due_date=datetime.date(2025, 2, 15),
        )
        with pytest.raises(InvariantError, match="Statement opening balance"):
            validate_credit_card_reconciliation(stmt, summary, [])

    def test_card_closing_balance_mismatch_raises(self) -> None:
        stmt = make_statement(opening=1000, closing=1500)
        summary = CreditCardSummary(
            statement_id=stmt.statement_id,
            previous_balance_cents=1000,
            payments_credits_cents=0,
            purchases_cents=500,
            cash_advances_cents=0,
            balance_transfers_cents=0,
            fees_charged_cents=0,
            interest_charged_cents=0,
            new_balance_cents=1600,  # Mismatch with closing
            minimum_payment_due_cents=25,
            payment_due_date=datetime.date(2025, 2, 15),
        )
        with pytest.raises(InvariantError, match="Statement closing balance"):
            validate_credit_card_reconciliation(stmt, summary, [])

    def test_credit_card_reconciliation_happy_path(self) -> None:
        stmt = make_statement(opening=1000, closing=1345)
        txns = [
            CanonicalTransaction(
                statement_id=stmt.statement_id,
                post_date=datetime.date(2025, 1, 5),
                amount_cents=500,
                description="Groceries",
                transaction_category=TransactionCategory.PURCHASE,
            ),
            CanonicalTransaction(
                statement_id=stmt.statement_id,
                post_date=datetime.date(2025, 1, 6),
                amount_cents=100,
                description="ATM Advance",
                transaction_category=TransactionCategory.CASH_ADVANCE,
            ),
            CanonicalTransaction(
                statement_id=stmt.statement_id,
                post_date=datetime.date(2025, 1, 7),
                amount_cents=200,
                description="Card Transfer",
                transaction_category=TransactionCategory.BALANCE_TRANSFER,
            ),
            CanonicalTransaction(
                statement_id=stmt.statement_id,
                post_date=datetime.date(2025, 1, 8),
                amount_cents=30,
                description="Late Fee",
                transaction_category=TransactionCategory.FEE,
            ),
            CanonicalTransaction(
                statement_id=stmt.statement_id,
                post_date=datetime.date(2025, 1, 9),
                amount_cents=-10,
                description="Fee Reversal",
                transaction_category=TransactionCategory.FEE_REVERSAL,
            ),
            CanonicalTransaction(
                statement_id=stmt.statement_id,
                post_date=datetime.date(2025, 1, 10),
                amount_cents=15,
                description="Interest",
                transaction_category=TransactionCategory.INTEREST_CHARGED,
            ),
            CanonicalTransaction(
                statement_id=stmt.statement_id,
                post_date=datetime.date(2025, 1, 11),
                amount_cents=-440,
                description="Autopay",
                transaction_category=TransactionCategory.PAYMENT,
            ),
            CanonicalTransaction(
                statement_id=stmt.statement_id,
                post_date=datetime.date(2025, 1, 12),
                amount_cents=-50,
                description="Refund Credit",
                transaction_category=TransactionCategory.CREDIT,
            ),
        ]
        summary = CreditCardSummary(
            statement_id=stmt.statement_id,
            previous_balance_cents=1000,
            payments_credits_cents=490,
            purchases_cents=500,
            cash_advances_cents=100,
            balance_transfers_cents=200,
            fees_charged_cents=20,
            interest_charged_cents=15,
            new_balance_cents=1345,
            minimum_payment_due_cents=25,
            payment_due_date=datetime.date(2025, 2, 15),
        )
        validate_credit_card_reconciliation(stmt, summary, txns)

    def test_credit_card_balance_equation_mismatch_raises(self) -> None:
        stmt = make_statement(opening=1000, closing=1600)
        summary = CreditCardSummary(
            statement_id=stmt.statement_id,
            previous_balance_cents=1000,
            payments_credits_cents=0,
            purchases_cents=500,
            cash_advances_cents=0,
            balance_transfers_cents=0,
            fees_charged_cents=0,
            interest_charged_cents=0,
            new_balance_cents=1600,  # 1000 + 500 = 1500 != 1600
            minimum_payment_due_cents=25,
            payment_due_date=datetime.date(2025, 2, 15),
        )
        with pytest.raises(InvariantError, match="Credit card balance equation failed"):
            validate_credit_card_reconciliation(stmt, summary, [])

    def test_card_purchases_bucket_mismatch_raises(self) -> None:
        stmt = make_statement(opening=0, closing=500)
        # Summary equation balances: 0 + 500 = 500 == new_balance
        # But transaction line is 400 purchases != 500 bucket
        txns = [
            CanonicalTransaction(
                statement_id=stmt.statement_id,
                post_date=datetime.date(2025, 1, 10),
                amount_cents=400,
                description="Store Purchase",
                transaction_category=TransactionCategory.PURCHASE,
            )
        ]
        summary = CreditCardSummary(
            statement_id=stmt.statement_id,
            previous_balance_cents=0,
            payments_credits_cents=0,
            purchases_cents=500,
            cash_advances_cents=0,
            balance_transfers_cents=0,
            fees_charged_cents=0,
            interest_charged_cents=0,
            new_balance_cents=500,
            minimum_payment_due_cents=25,
            payment_due_date=datetime.date(2025, 2, 15),
        )
        with pytest.raises(InvariantError, match="Purchases bucket"):
            validate_credit_card_reconciliation(stmt, summary, txns)

    def test_card_advances_bucket_mismatch_raises(self) -> None:
        stmt = make_statement(opening=0, closing=500)
        # Summary balances: 0 + 500 advances = 500
        # Transaction line is 400 != 500
        txns = [
            CanonicalTransaction(
                statement_id=stmt.statement_id,
                post_date=datetime.date(2025, 1, 10),
                amount_cents=400,
                description="Cash Advance",
                transaction_category=TransactionCategory.CASH_ADVANCE,
            )
        ]
        summary = CreditCardSummary(
            statement_id=stmt.statement_id,
            previous_balance_cents=0,
            payments_credits_cents=0,
            purchases_cents=0,
            cash_advances_cents=500,
            balance_transfers_cents=0,
            fees_charged_cents=0,
            interest_charged_cents=0,
            new_balance_cents=500,
            minimum_payment_due_cents=25,
            payment_due_date=datetime.date(2025, 2, 15),
        )
        with pytest.raises(InvariantError, match="Cash advances bucket"):
            validate_credit_card_reconciliation(stmt, summary, txns)

    def test_card_balance_transfers_bucket_mismatch_raises(self) -> None:
        stmt = make_statement(opening=0, closing=500)
        # Summary balances: 0 + 500 BT = 500
        # Transaction line is 400 != 500
        txns = [
            CanonicalTransaction(
                statement_id=stmt.statement_id,
                post_date=datetime.date(2025, 1, 10),
                amount_cents=400,
                description="Balance Transfer",
                transaction_category=TransactionCategory.BALANCE_TRANSFER,
            )
        ]
        summary = CreditCardSummary(
            statement_id=stmt.statement_id,
            previous_balance_cents=0,
            payments_credits_cents=0,
            purchases_cents=0,
            cash_advances_cents=0,
            balance_transfers_cents=500,
            fees_charged_cents=0,
            interest_charged_cents=0,
            new_balance_cents=500,
            minimum_payment_due_cents=25,
            payment_due_date=datetime.date(2025, 2, 15),
        )
        with pytest.raises(InvariantError, match="Balance transfers bucket"):
            validate_credit_card_reconciliation(stmt, summary, txns)

    def test_card_fees_bucket_mismatch_raises(self) -> None:
        stmt = make_statement(opening=0, closing=100)
        txns = [
            CanonicalTransaction(
                statement_id=stmt.statement_id,
                post_date=datetime.date(2025, 1, 10),
                amount_cents=80,
                description="Late Fee",
                transaction_category=TransactionCategory.FEE,
            )
        ]
        summary = CreditCardSummary(
            statement_id=stmt.statement_id,
            previous_balance_cents=0,
            payments_credits_cents=0,
            purchases_cents=0,
            cash_advances_cents=0,
            balance_transfers_cents=0,
            fees_charged_cents=100,
            interest_charged_cents=0,
            new_balance_cents=100,
            minimum_payment_due_cents=25,
            payment_due_date=datetime.date(2025, 2, 15),
        )
        with pytest.raises(InvariantError, match="Fees charged bucket"):
            validate_credit_card_reconciliation(stmt, summary, txns)

    def test_card_interest_bucket_mismatch_raises(self) -> None:
        stmt = make_statement(opening=0, closing=100)
        txns = [
            CanonicalTransaction(
                statement_id=stmt.statement_id,
                post_date=datetime.date(2025, 1, 10),
                amount_cents=80,
                description="Interest Charge",
                transaction_category=TransactionCategory.INTEREST_CHARGED,
            )
        ]
        summary = CreditCardSummary(
            statement_id=stmt.statement_id,
            previous_balance_cents=0,
            payments_credits_cents=0,
            purchases_cents=0,
            cash_advances_cents=0,
            balance_transfers_cents=0,
            fees_charged_cents=0,
            interest_charged_cents=100,
            new_balance_cents=100,
            minimum_payment_due_cents=25,
            payment_due_date=datetime.date(2025, 2, 15),
        )
        with pytest.raises(InvariantError, match="Interest charged bucket"):
            validate_credit_card_reconciliation(stmt, summary, txns)

    def test_card_payments_bucket_mismatch_raises(self) -> None:
        stmt = make_statement(opening=1000, closing=500)
        txns = [
            CanonicalTransaction(
                statement_id=stmt.statement_id,
                post_date=datetime.date(2025, 1, 10),
                amount_cents=-400,
                description="Online Payment",
                transaction_category=TransactionCategory.PAYMENT,
            )
        ]
        summary = CreditCardSummary(
            statement_id=stmt.statement_id,
            previous_balance_cents=1000,
            payments_credits_cents=500,  # Line is 400 != 500
            purchases_cents=0,
            cash_advances_cents=0,
            balance_transfers_cents=0,
            fees_charged_cents=0,
            interest_charged_cents=0,
            new_balance_cents=500,
            minimum_payment_due_cents=25,
            payment_due_date=datetime.date(2025, 2, 15),
        )
        with pytest.raises(InvariantError, match="Payments/credits bucket"):
            validate_credit_card_reconciliation(stmt, summary, txns)


class TestBrokerageReconciliation:
    def test_brokerage_opening_cash_mismatch_raises(self) -> None:
        stmt = make_statement(opening=1000, closing=1500)
        summary = BrokerageSummary(
            statement_id=stmt.statement_id,
            opening_cash_cents=2000,
            closing_cash_cents=1500,
        )
        with pytest.raises(InvariantError, match="Statement opening balance"):
            validate_brokerage_reconciliation(stmt, summary, [])

    def test_brokerage_closing_cash_mismatch_raises(self) -> None:
        stmt = make_statement(opening=1000, closing=1500)
        summary = BrokerageSummary(
            statement_id=stmt.statement_id,
            opening_cash_cents=1000,
            closing_cash_cents=2000,
        )
        with pytest.raises(InvariantError, match="Statement closing balance"):
            validate_brokerage_reconciliation(stmt, summary, [])

    def test_brokerage_portfolio_bridge_reconciliation_failure(self) -> None:
        stmt = make_statement(opening=1000, closing=1500)
        summary = BrokerageSummary(
            statement_id=stmt.statement_id,
            opening_cash_cents=1000,
            closing_cash_cents=1500,
            opening_portfolio_cents=10_000,
            closing_portfolio_cents=12_000,
            transfers_in_cents=1000,
            transfers_out_cents=0,
            income_dividends_cents=500,
            realized_gains_cents=100,
            unrealized_gains_cents=200,  # 10000 + 1000 - 0 + 500 + 100 + 200 = 11800 != 12000
        )
        with pytest.raises(InvariantError, match="Portfolio bridge mismatch"):
            validate_brokerage_reconciliation(stmt, summary, [])

    def test_brokerage_reconciliation_happy_path_no_portfolio(self) -> None:
        stmt = make_statement(opening=1000, closing=1500)
        summary = BrokerageSummary(
            statement_id=stmt.statement_id,
            opening_cash_cents=1000,
            closing_cash_cents=1500,
        )
        validate_brokerage_reconciliation(stmt, summary, [])

    def test_brokerage_reconciliation_happy_path_with_portfolio(self) -> None:
        stmt = make_statement(opening=1000, closing=1500)
        summary = BrokerageSummary(
            statement_id=stmt.statement_id,
            opening_cash_cents=1000,
            closing_cash_cents=1500,
            opening_portfolio_cents=10_000,
            transfers_in_cents=1000,
            transfers_out_cents=200,
            income_dividends_cents=500,
            realized_gains_cents=100,
            unrealized_gains_cents=200,
            closing_portfolio_cents=10000 + 1000 - 200 + 500 + 100 + 200,
        )
        validate_brokerage_reconciliation(stmt, summary, [])

    def test_brokerage_portfolio_bridge_all_zero_components(self) -> None:
        stmt = make_statement(opening=1000, closing=1500)
        summary = BrokerageSummary(
            statement_id=stmt.statement_id,
            opening_cash_cents=1000,
            closing_cash_cents=1500,
            opening_portfolio_cents=10_000,
            transfers_in_cents=0,
            transfers_out_cents=0,
            income_dividends_cents=0,
            realized_gains_cents=0,
            unrealized_gains_cents=0,
            closing_portfolio_cents=10_000,
        )
        validate_brokerage_reconciliation(stmt, summary, [])

    @pytest.mark.parametrize(
        ("delta_in", "delta_out", "delta_inc", "delta_real", "delta_unreal"),
        [
            (1, 0, 0, 0, 0),
            (0, 1, 0, 0, 0),
            (0, 0, 1, 0, 0),
            (0, 0, 0, 1, 0),
            (0, 0, 0, 0, 1),
        ],
    )
    def test_brokerage_portfolio_bridge_component_tamper(
        self, delta_in, delta_out, delta_inc, delta_real, delta_unreal
    ) -> None:
        stmt = make_statement(opening=1000, closing=1500)
        summary = BrokerageSummary(
            statement_id=stmt.statement_id,
            opening_cash_cents=1000,
            closing_cash_cents=1500,
            opening_portfolio_cents=10_000,
            transfers_in_cents=1000 + delta_in,
            transfers_out_cents=200 + delta_out,
            income_dividends_cents=500 + delta_inc,
            realized_gains_cents=100 + delta_real,
            unrealized_gains_cents=200 + delta_unreal,
            closing_portfolio_cents=11600,
        )
        with pytest.raises(InvariantError, match="Portfolio bridge mismatch"):
            validate_brokerage_reconciliation(stmt, summary, [])

    def test_brokerage_portfolio_bridge_partial_components_raises(self) -> None:
        stmt = make_statement(opening=1000, closing=1500)
        summary = BrokerageSummary(
            statement_id=stmt.statement_id,
            opening_cash_cents=1000,
            closing_cash_cents=1500,
            opening_portfolio_cents=10_000,
            closing_portfolio_cents=12_000,
            transfers_in_cents=1000,
            transfers_out_cents=None,
        )
        with pytest.raises(
            InvariantError,
            match=r"^Portfolio bridge is present but contains null components\. All components must be signed cents\.$",
        ):
            validate_brokerage_reconciliation(stmt, summary, [])
