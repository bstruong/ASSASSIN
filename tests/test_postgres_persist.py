"""Unit and integration tests for PostgreSQL persistence layer (Step 5)."""

from __future__ import annotations

import ast
import datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from pydantic import ValidationError

from app.db.repository import (
    get_canonical_statement,
    get_extraction_run,
    get_raw_pages,
    get_raw_payload_by_sha256,
    get_statement_holdings,
    persist_canonical_statement,
    persist_raw_extraction,
    update_run_status,
)
from app.models.canonical import (
    Account,
    BrokerageSummary,
    CanonicalStatement,
    CanonicalTransaction,
    CreditCardSummary,
    DepositorySummary,
    Holding,
)
from app.models.enums import (
    AccountDomain,
    AccountType,
    RunStatus,
    TransactionCategory,
)
from app.models.exceptions import PersistenceError
from app.models.raw import (
    ExtractionRun,
    RawExtraction,
    RawPage,
    RawPayload,
    RawToken,
)


class TestPostgresPersistASTInvariants:
    """Static AST assertions verifying append-only raw database guarantees."""

    def test_no_update_or_delete_on_raw_tables(self) -> None:
        """Verify repository.py contains zero UPDATE or DELETE queries targeting raw tables."""
        repo_path = Path(__file__).parents[1] / "app" / "db" / "repository.py"
        source = repo_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        string_constants: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                string_constants.append(node.value)

        for text in string_constants:
            upper = text.upper()
            if (
                "UPDATE RAW_PAYLOADS" in upper
                or "UPDATE RAW_PAGES" in upper
                or "UPDATE RAW_TOKENS" in upper
            ):
                raise AssertionError(f"Forbidden UPDATE query on raw table: {text}")
            if (
                "DELETE FROM RAW_PAYLOADS" in upper
                or "DELETE FROM RAW_PAGES" in upper
                or "DELETE FROM RAW_TOKENS" in upper
            ):
                raise AssertionError(f"Forbidden DELETE query on raw table: {text}")


class TestPostgresRawPersistence:
    """Tests for append-only raw extraction storage."""

    def test_persist_raw_extraction_roundtrip(
        self, db_conn: psycopg.Connection
    ) -> None:
        payload = RawPayload(
            content_sha256="a" * 64,
            byte_length=12345,
            original_basename="statement.pdf",
        )
        run = ExtractionRun(
            raw_payload_id=payload.raw_payload_id,
            adapter_id="test_adapter",
            adapter_version="1.0.0",
            status=RunStatus.RAW_STORED,
        )
        page1 = RawPage(
            run_id=run.run_id,
            page_number=1,
            page_text="Page 1 sample text",
            tokens=[
                RawToken(
                    raw_page_id=uuid4(),
                    token_kind="word",
                    token_text="Hello",
                    x0_mp=10000,
                    y0_mp=20000,
                    x1_mp=50000,
                    y1_mp=30000,
                ),
                RawToken(
                    raw_page_id=uuid4(),
                    token_kind="cell",
                    token_text="$100.00",
                    x0_mp=60000,
                    y0_mp=20000,
                    x1_mp=90000,
                    y1_mp=30000,
                    table_index=0,
                    row_index=1,
                    col_index=2,
                ),
            ],
        )
        # Fix raw_page_id on tokens to match page
        for t in page1.tokens:
            object.__setattr__(t, "raw_page_id", page1.raw_page_id)

        extraction = RawExtraction(payload=payload, run=run, pages=[page1])
        persisted_run_id = persist_raw_extraction(db_conn, extraction)
        db_conn.commit()

        assert persisted_run_id == run.run_id

        # Verify raw_payloads row
        fetched_payload = get_raw_payload_by_sha256(db_conn, payload.content_sha256)
        assert fetched_payload is not None
        assert fetched_payload.raw_payload_id == payload.raw_payload_id
        assert fetched_payload.content_sha256 == payload.content_sha256
        assert fetched_payload.byte_length == 12345
        assert fetched_payload.original_basename == "statement.pdf"

        # Verify extraction_runs row
        fetched_run = get_extraction_run(db_conn, run.run_id)
        assert fetched_run is not None
        assert fetched_run.run_id == run.run_id
        assert fetched_run.status == RunStatus.RAW_STORED

        # Verify raw_pages and raw_tokens
        pages = get_raw_pages(db_conn, run.run_id)
        assert len(pages) == 1
        assert pages[0].page_number == 1
        assert pages[0].page_text == "Page 1 sample text"
        assert len(pages[0].tokens) == 2
        assert pages[0].tokens[0].token_text == "Hello"
        assert pages[0].tokens[0].x0_mp == 10000
        assert pages[0].tokens[1].token_kind == "cell"
        assert pages[0].tokens[1].token_text == "$100.00"
        assert pages[0].tokens[1].table_index == 0

    def test_reingest_same_payload_reuses_blob_and_creates_new_run(
        self, db_conn: psycopg.Connection
    ) -> None:
        content_sha = "b" * 64
        payload1 = RawPayload(
            content_sha256=content_sha,
            byte_length=500,
            original_basename="stmt_v1.pdf",
        )
        run1 = ExtractionRun(
            raw_payload_id=payload1.raw_payload_id,
            adapter_id="adapter_a",
            adapter_version="1.0.0",
            status=RunStatus.RAW_STORED,
        )
        page1 = RawPage(
            run_id=run1.run_id,
            page_number=1,
            page_text="First run text",
        )
        extraction1 = RawExtraction(payload=payload1, run=run1, pages=[page1])
        persist_raw_extraction(db_conn, extraction1)
        db_conn.commit()

        # Second ingestion with the same SHA256 bytes
        payload2 = RawPayload(
            content_sha256=content_sha,
            byte_length=500,
            original_basename="stmt_v2.pdf",
        )
        run2 = ExtractionRun(
            raw_payload_id=payload2.raw_payload_id,
            adapter_id="adapter_a",
            adapter_version="2.0.0",
            status=RunStatus.RAW_STORED,
        )
        page2 = RawPage(
            run_id=run2.run_id,
            page_number=1,
            page_text="Second run text",
        )
        extraction2 = RawExtraction(payload=payload2, run=run2, pages=[page2])
        persist_raw_extraction(db_conn, extraction2)
        db_conn.commit()

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM raw_payloads WHERE content_sha256 = %s;",
                (content_sha,),
            )
            assert cur.fetchone()[0] == 1

            cur.execute("SELECT COUNT(*) FROM extraction_runs;")
            assert cur.fetchone()[0] == 2

            cur.execute(
                "SELECT raw_payload_id FROM extraction_runs WHERE run_id = %s;",
                (str(run2.run_id),),
            )
            # Both runs link to the single canonical raw_payload_id
            assert UUID(str(cur.fetchone()[0])) == payload1.raw_payload_id


class TestPostgresCanonicalPersistence:
    """Tests for canonical statement, account, sidecars, and transaction persistence."""

    def test_persist_checking_statement_happy_path(
        self, db_conn: psycopg.Connection
    ) -> None:
        payload = RawPayload(
            content_sha256="c" * 64,
            byte_length=1000,
            original_basename="checking.pdf",
        )
        run = ExtractionRun(
            raw_payload_id=payload.raw_payload_id,
            adapter_id="standard_depository",
            adapter_version="1.0.0",
            status=RunStatus.VALIDATED,
        )
        page = RawPage(run_id=run.run_id, page_number=1, page_text="Checking statement")
        persist_raw_extraction(
            db_conn, RawExtraction(payload=payload, run=run, pages=[page])
        )

        account = Account(
            institution="Bank of Test",
            account_mask="*1234",
            account_domain=AccountDomain.DEPOSITORY,
            account_type=AccountType.CHECKING,
        )
        statement = CanonicalStatement(
            account_id=account.account_id,
            run_id=run.run_id,
            raw_payload_id=payload.raw_payload_id,
            statement_start_date=datetime.date(2025, 1, 1),
            statement_end_date=datetime.date(2025, 1, 31),
            opening_balance_cents=100000,
            closing_balance_cents=150000,
            net_change_cents=50000,
        )
        summary = DepositorySummary(
            statement_id=statement.statement_id,
            deposits_cents=60000,
            withdrawals_cents=10000,
            interest_paid_cents=0,
            fees_cents=0,
        )
        txns = [
            CanonicalTransaction(
                statement_id=statement.statement_id,
                post_date=datetime.date(2025, 1, 10),
                amount_cents=60000,
                description="Payroll Deposit",
                transaction_category=TransactionCategory.DEPOSIT,
            ),
            CanonicalTransaction(
                statement_id=statement.statement_id,
                post_date=datetime.date(2025, 1, 15),
                amount_cents=-10000,
                description="Groceries",
                transaction_category=TransactionCategory.WITHDRAWAL,
            ),
        ]

        persist_canonical_statement(db_conn, account, statement, summary, txns)
        db_conn.commit()

        # Check retrieval
        res = get_canonical_statement(db_conn, statement.statement_id)
        assert res is not None
        stmt_out, summary_out, txns_out = res

        assert stmt_out.opening_balance_cents == 100000
        assert stmt_out.closing_balance_cents == 150000
        assert stmt_out.net_change_cents == 50000
        assert isinstance(summary_out, DepositorySummary)
        assert summary_out.deposits_cents == 60000
        assert summary_out.withdrawals_cents == 10000
        assert len(txns_out) == 2
        assert txns_out[0].amount_cents == 60000
        assert txns_out[1].amount_cents == -10000

        # Check run status updated
        updated_run = get_extraction_run(db_conn, run.run_id)
        assert updated_run is not None
        assert updated_run.status == RunStatus.CANONICAL_PERSISTED

    def test_persist_credit_card_statement_happy_path(
        self, db_conn: psycopg.Connection
    ) -> None:
        payload = RawPayload(
            content_sha256="d" * 64,
            byte_length=1500,
            original_basename="card.pdf",
        )
        run = ExtractionRun(
            raw_payload_id=payload.raw_payload_id,
            adapter_id="standard_credit_card",
            adapter_version="1.0.0",
            status=RunStatus.VALIDATED,
        )
        page = RawPage(run_id=run.run_id, page_number=1, page_text="Card statement")
        persist_raw_extraction(
            db_conn, RawExtraction(payload=payload, run=run, pages=[page])
        )

        account = Account(
            institution="Card Bank",
            account_mask="*9999",
            account_domain=AccountDomain.REVOLVING_CREDIT,
            account_type=AccountType.CREDIT_CARD,
        )
        statement = CanonicalStatement(
            account_id=account.account_id,
            run_id=run.run_id,
            raw_payload_id=payload.raw_payload_id,
            statement_start_date=datetime.date(2025, 2, 1),
            statement_end_date=datetime.date(2025, 2, 28),
            opening_balance_cents=50000,
            closing_balance_cents=28000,
            net_change_cents=-22000,
        )
        summary = CreditCardSummary(
            statement_id=statement.statement_id,
            previous_balance_cents=50000,
            payments_credits_cents=50000,
            purchases_cents=25000,
            cash_advances_cents=0,
            balance_transfers_cents=0,
            fees_charged_cents=2500,
            interest_charged_cents=500,
            new_balance_cents=28000,
            minimum_payment_due_cents=3500,
            payment_due_date=datetime.date(2025, 3, 25),
        )
        txns = [
            CanonicalTransaction(
                statement_id=statement.statement_id,
                post_date=datetime.date(2025, 2, 5),
                amount_cents=-50000,
                description="Payment Received",
                transaction_category=TransactionCategory.PAYMENT,
            ),
            CanonicalTransaction(
                statement_id=statement.statement_id,
                post_date=datetime.date(2025, 2, 12),
                amount_cents=25000,
                description="Flight Ticket",
                transaction_category=TransactionCategory.PURCHASE,
            ),
        ]

        persist_canonical_statement(db_conn, account, statement, summary, txns)
        db_conn.commit()

        res = get_canonical_statement(db_conn, statement.statement_id)
        assert res is not None
        stmt_out, summary_out, txns_out = res
        assert stmt_out.opening_balance_cents == 50000
        assert stmt_out.closing_balance_cents == 28000
        assert len(txns_out) == 2
        assert isinstance(summary_out, CreditCardSummary)
        assert summary_out.previous_balance_cents == 50000
        assert summary_out.new_balance_cents == 28000
        assert summary_out.minimum_payment_due_cents == 3500
        assert summary_out.payment_due_date == datetime.date(2025, 3, 25)

    def test_persist_credit_card_missing_min_payment_fails_loud(self) -> None:
        """A card summary without a minimum payment cannot be constructed or persisted."""
        with pytest.raises(ValidationError):
            CreditCardSummary(
                statement_id=uuid4(),
                previous_balance_cents=50000,
                payments_credits_cents=0,
                purchases_cents=0,
                cash_advances_cents=0,
                balance_transfers_cents=0,
                fees_charged_cents=0,
                interest_charged_cents=0,
                new_balance_cents=50000,
                minimum_payment_due_cents=None,
            )

    def test_persist_brokerage_statement_happy_path(
        self, db_conn: psycopg.Connection
    ) -> None:
        payload = RawPayload(
            content_sha256="e" * 64,
            byte_length=2000,
            original_basename="brokerage.pdf",
        )
        run = ExtractionRun(
            raw_payload_id=payload.raw_payload_id,
            adapter_id="standard_brokerage",
            adapter_version="1.0.0",
            status=RunStatus.VALIDATED,
        )
        persist_raw_extraction(
            db_conn,
            RawExtraction(
                payload=payload,
                run=run,
                pages=[
                    RawPage(
                        run_id=run.run_id, page_number=1, page_text="Brokerage text"
                    )
                ],
            ),
        )

        account = Account(
            institution="Schwab",
            account_mask="*4321",
            account_domain=AccountDomain.CUSTODIAL_BROKERAGE,
            account_type=AccountType.BROKERAGE_CASH,
        )
        statement = CanonicalStatement(
            account_id=account.account_id,
            run_id=run.run_id,
            raw_payload_id=payload.raw_payload_id,
            statement_start_date=datetime.date(2025, 3, 1),
            statement_end_date=datetime.date(2025, 3, 31),
            opening_balance_cents=100000,
            closing_balance_cents=105000,
            net_change_cents=5000,
        )
        summary = BrokerageSummary(
            statement_id=statement.statement_id,
            opening_cash_cents=100000,
            closing_cash_cents=105000,
            opening_portfolio_cents=5000000,
            closing_portfolio_cents=5200000,
            realized_gains_cents=50000,
            unrealized_gains_cents=150000,
            income_dividends_cents=5000,
            transfers_in_cents=0,
            transfers_out_cents=0,
        )
        txns = [
            CanonicalTransaction(
                statement_id=statement.statement_id,
                post_date=datetime.date(2025, 3, 15),
                amount_cents=5000,
                description="Cash Dividend",
                transaction_category=TransactionCategory.DIVIDEND,
            )
        ]

        holdings = [
            Holding(
                statement_id=statement.statement_id,
                as_of_date=datetime.date(2025, 3, 31),
                symbol="SYN",
                description="SYNTHETIC Equity",
                quantity_nanos=10_000_000_000,
                market_value_cents=100000,
            )
        ]
        persist_canonical_statement(
            db_conn, account, statement, summary, txns, holdings=holdings
        )
        db_conn.commit()

        res = get_canonical_statement(db_conn, statement.statement_id)
        assert res is not None
        stmt_out, summary_out, txns_out = res
        assert stmt_out.opening_balance_cents == 100000
        assert stmt_out.closing_balance_cents == 105000
        assert len(txns_out) == 1
        assert isinstance(summary_out, BrokerageSummary)
        assert summary_out.opening_cash_cents == 100000
        assert summary_out.opening_portfolio_cents == 5000000
        assert summary_out.closing_portfolio_cents == 5200000
        stored = get_statement_holdings(db_conn, statement.statement_id)
        assert len(stored) == 1
        assert stored[0].symbol == "SYN"
        assert stored[0].quantity_nanos == 10_000_000_000
        assert stored[0].market_value_cents == 100000
        assert stored[0].description == "SYNTHETIC Equity"


class TestPostgresPersistenceFailures:
    """Tests asserting fail-loud contract enforcement and invariant validation."""

    def test_domain_mismatch_raises_persistence_error(
        self, db_conn: psycopg.Connection
    ) -> None:
        payload = RawPayload(
            content_sha256="f" * 64, byte_length=100, original_basename="mismatch.pdf"
        )
        run = ExtractionRun(
            raw_payload_id=payload.raw_payload_id,
            adapter_id="a",
            adapter_version="1",
            status=RunStatus.VALIDATED,
        )
        persist_raw_extraction(
            db_conn,
            RawExtraction(
                payload=payload,
                run=run,
                pages=[RawPage(run_id=run.run_id, page_number=1, page_text="t")],
            ),
        )

        account = Account(
            institution="Bank",
            account_mask="*1111",
            account_domain=AccountDomain.DEPOSITORY,
            account_type=AccountType.CHECKING,
        )
        statement = CanonicalStatement(
            account_id=account.account_id,
            run_id=run.run_id,
            raw_payload_id=payload.raw_payload_id,
            statement_start_date=datetime.date(2025, 1, 1),
            statement_end_date=datetime.date(2025, 1, 31),
            opening_balance_cents=100,
            closing_balance_cents=200,
            net_change_cents=100,
        )
        # Passing CreditCardSummary for depository account
        summary = CreditCardSummary(
            statement_id=statement.statement_id,
            previous_balance_cents=100,
            payments_credits_cents=0,
            purchases_cents=100,
            cash_advances_cents=0,
            balance_transfers_cents=0,
            fees_charged_cents=0,
            interest_charged_cents=0,
            new_balance_cents=200,
            minimum_payment_due_cents=0,
        )

        with pytest.raises(PersistenceError, match="requires DepositorySummary"):
            persist_canonical_statement(db_conn, account, statement, summary, [])

    def test_denormalized_payload_id_mismatch_raises_persistence_error(
        self, db_conn: psycopg.Connection
    ) -> None:
        payload = RawPayload(
            content_sha256="1" * 64, byte_length=100, original_basename="a.pdf"
        )
        run = ExtractionRun(
            raw_payload_id=payload.raw_payload_id,
            adapter_id="a",
            adapter_version="1",
            status=RunStatus.VALIDATED,
        )
        persist_raw_extraction(
            db_conn,
            RawExtraction(
                payload=payload,
                run=run,
                pages=[RawPage(run_id=run.run_id, page_number=1, page_text="t")],
            ),
        )

        account = Account(
            institution="Bank",
            account_mask="*1111",
            account_domain=AccountDomain.DEPOSITORY,
            account_type=AccountType.CHECKING,
        )
        statement = CanonicalStatement(
            account_id=account.account_id,
            run_id=run.run_id,
            raw_payload_id=uuid4(),  # Mismatched payload ID!
            statement_start_date=datetime.date(2025, 1, 1),
            statement_end_date=datetime.date(2025, 1, 31),
            opening_balance_cents=100,
            closing_balance_cents=200,
            net_change_cents=100,
        )
        summary = DepositorySummary(
            statement_id=statement.statement_id,
            deposits_cents=100,
            withdrawals_cents=0,
            interest_paid_cents=0,
            fees_cents=0,
        )

        with pytest.raises(
            PersistenceError, match="does not match extraction_runs.raw_payload_id"
        ):
            persist_canonical_statement(db_conn, account, statement, summary, [])

    def test_card_balance_mismatch_raises_persistence_error(
        self, db_conn: psycopg.Connection
    ) -> None:
        payload = RawPayload(
            content_sha256="2" * 64, byte_length=100, original_basename="b.pdf"
        )
        run = ExtractionRun(
            raw_payload_id=payload.raw_payload_id,
            adapter_id="a",
            adapter_version="1",
            status=RunStatus.VALIDATED,
        )
        persist_raw_extraction(
            db_conn,
            RawExtraction(
                payload=payload,
                run=run,
                pages=[RawPage(run_id=run.run_id, page_number=1, page_text="t")],
            ),
        )

        account = Account(
            institution="Bank",
            account_mask="*1111",
            account_domain=AccountDomain.REVOLVING_CREDIT,
            account_type=AccountType.CREDIT_CARD,
        )
        statement = CanonicalStatement(
            account_id=account.account_id,
            run_id=run.run_id,
            raw_payload_id=payload.raw_payload_id,
            statement_start_date=datetime.date(2025, 1, 1),
            statement_end_date=datetime.date(2025, 1, 31),
            opening_balance_cents=100,
            closing_balance_cents=200,
            net_change_cents=100,
        )
        summary = CreditCardSummary(
            statement_id=statement.statement_id,
            previous_balance_cents=150,  # Mismatch with statement opening (100)
            payments_credits_cents=0,
            purchases_cents=50,
            cash_advances_cents=0,
            balance_transfers_cents=0,
            fees_charged_cents=0,
            interest_charged_cents=0,
            new_balance_cents=200,
            minimum_payment_due_cents=0,
        )

        with pytest.raises(
            PersistenceError, match="statement opening .* != summary previous"
        ):
            persist_canonical_statement(db_conn, account, statement, summary, [])

    def test_transaction_statement_id_mismatch_raises_persistence_error(
        self, db_conn: psycopg.Connection
    ) -> None:
        payload = RawPayload(
            content_sha256="3" * 64, byte_length=100, original_basename="c.pdf"
        )
        run = ExtractionRun(
            raw_payload_id=payload.raw_payload_id,
            adapter_id="a",
            adapter_version="1",
            status=RunStatus.VALIDATED,
        )
        persist_raw_extraction(
            db_conn,
            RawExtraction(
                payload=payload,
                run=run,
                pages=[RawPage(run_id=run.run_id, page_number=1, page_text="t")],
            ),
        )

        account = Account(
            institution="Bank",
            account_mask="*1111",
            account_domain=AccountDomain.DEPOSITORY,
            account_type=AccountType.CHECKING,
        )
        statement = CanonicalStatement(
            account_id=account.account_id,
            run_id=run.run_id,
            raw_payload_id=payload.raw_payload_id,
            statement_start_date=datetime.date(2025, 1, 1),
            statement_end_date=datetime.date(2025, 1, 31),
            opening_balance_cents=100,
            closing_balance_cents=200,
            net_change_cents=100,
        )
        summary = DepositorySummary(
            statement_id=statement.statement_id,
            deposits_cents=100,
            withdrawals_cents=0,
            interest_paid_cents=0,
            fees_cents=0,
        )
        txn = CanonicalTransaction(
            statement_id=uuid4(),  # Mismatched statement ID!
            post_date=datetime.date(2025, 1, 15),
            amount_cents=100,
            description="Deposit",
            transaction_category=TransactionCategory.DEPOSIT,
        )

        with pytest.raises(
            PersistenceError, match="transaction.statement_id .* does not match"
        ):
            persist_canonical_statement(db_conn, account, statement, summary, [txn])

    def test_update_run_status_invalid_error_code_fails(
        self, db_conn: psycopg.Connection
    ) -> None:
        run_id = uuid4()
        with pytest.raises(
            PersistenceError, match="status='failed' must correlate exactly"
        ):
            update_run_status(db_conn, run_id, RunStatus.FAILED, error_code=None)
