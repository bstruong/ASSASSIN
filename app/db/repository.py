"""Repository for append-only raw extraction and canonical persistence in PostgreSQL."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from uuid import UUID

import psycopg

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
    CurrencyCode,
    RunStatus,
    TransactionCategory,
)
from app.models.exceptions import InvariantError, PersistenceError
from app.models.raw import ExtractionRun, RawExtraction, RawPage, RawPayload, RawToken

logger = logging.getLogger(__name__)


# ── Raw Extraction Persistence (Append-Only) ──────────────────────────


def persist_raw_payload(conn: psycopg.Connection, payload: RawPayload) -> UUID:
    """Insert raw payload or return existing raw_payload_id if sha256 exists (idempotent blob)."""
    logger.info(
        "Persisting raw payload blob",
        extra={"sha256": payload.content_sha256, "byte_length": payload.byte_length},
    )

    query = """
    INSERT INTO raw_payloads (raw_payload_id, content_sha256, byte_length, original_basename, ingested_at)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT (content_sha256) DO NOTHING
    RETURNING raw_payload_id;
    """
    with conn.cursor() as cur:
        cur.execute(
            query,
            (
                str(payload.raw_payload_id),
                payload.content_sha256,
                payload.byte_length,
                payload.original_basename,
                payload.ingested_at,
            ),
        )
        row = cur.fetchone()
        if row:
            return UUID(str(row[0]))

        # Blob exists; retrieve existing raw_payload_id
        cur.execute(
            "SELECT raw_payload_id FROM raw_payloads WHERE content_sha256 = %s;",
            (payload.content_sha256,),
        )
        existing_row = cur.fetchone()
        if not existing_row:
            raise PersistenceError(
                f"Failed to resolve raw_payload_id for sha256 {payload.content_sha256}"
            )
        return UUID(str(existing_row[0]))


def persist_extraction_run(conn: psycopg.Connection, run: ExtractionRun) -> UUID:
    """Insert a new extraction run record for an execution lifecycle."""
    logger.info(
        "Persisting extraction run",
        extra={
            "run_id": str(run.run_id),
            "adapter_id": run.adapter_id,
            "status": run.status.value,
        },
    )
    query = """
    INSERT INTO extraction_runs (
        run_id, raw_payload_id, adapter_id, adapter_version, started_at, status, error_code, error_message
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
    """
    with conn.cursor() as cur:
        cur.execute(
            query,
            (
                str(run.run_id),
                str(run.raw_payload_id),
                run.adapter_id,
                run.adapter_version,
                run.started_at,
                run.status.value,
                run.error_code,
                run.error_message,
            ),
        )
    return run.run_id


def update_run_status(
    conn: psycopg.Connection,
    run_id: UUID,
    status: RunStatus,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    """Update lifecycle status and error fields of an extraction run."""
    is_failed = status == RunStatus.FAILED
    has_error = error_code is not None
    if is_failed != has_error:
        raise PersistenceError(
            f"status='failed' must correlate exactly with error_code presence. "
            f"status={status.value!r}, error_code={error_code!r}"
        )

    logger.info(
        "Updating extraction run status",
        extra={"run_id": str(run_id), "status": status.value},
    )
    query = """
    UPDATE extraction_runs
    SET status = %s, error_code = %s, error_message = %s
    WHERE run_id = %s;
    """
    with conn.cursor() as cur:
        cur.execute(query, (status.value, error_code, error_message, str(run_id)))


def persist_raw_pages(conn: psycopg.Connection, pages: Sequence[RawPage]) -> None:
    """Insert raw pages and their tokens (strictly append-only)."""
    page_query = """
    INSERT INTO raw_pages (raw_page_id, run_id, page_number, page_text)
    VALUES (%s, %s, %s, %s);
    """
    token_query = """
    INSERT INTO raw_tokens (
        raw_token_id, raw_page_id, token_kind, token_text,
        x0_mp, y0_mp, x1_mp, y1_mp, table_index, row_index, col_index
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
    """
    with conn.cursor() as cur:
        for page in pages:
            cur.execute(
                page_query,
                (
                    str(page.raw_page_id),
                    str(page.run_id),
                    page.page_number,
                    page.page_text,
                ),
            )
            for token in page.tokens:
                cur.execute(
                    token_query,
                    (
                        str(token.raw_token_id),
                        str(page.raw_page_id),
                        token.token_kind,
                        token.token_text,
                        token.x0_mp,
                        token.y0_mp,
                        token.x1_mp,
                        token.y1_mp,
                        token.table_index,
                        token.row_index,
                        token.col_index,
                    ),
                )


def persist_raw_extraction(conn: psycopg.Connection, extraction: RawExtraction) -> UUID:
    """Atomically persist raw payload, run, pages, and tokens without monetary interpretation."""
    actual_payload_id = persist_raw_payload(conn, extraction.payload)

    # If payload previously existed under another ID, ensure run links to actual canonical payload ID
    run = extraction.run
    if run.raw_payload_id != actual_payload_id:
        run = ExtractionRun(
            run_id=run.run_id,
            raw_payload_id=actual_payload_id,
            adapter_id=run.adapter_id,
            adapter_version=run.adapter_version,
            started_at=run.started_at,
            status=run.status,
            error_code=run.error_code,
            error_message=run.error_message,
        )

    persist_extraction_run(conn, run)
    persist_raw_pages(conn, extraction.pages)
    return run.run_id


# ── Canonical Domain Persistence ───────────────────────────────────────


def get_or_create_account(conn: psycopg.Connection, account: Account) -> Account:
    """Find existing account by (institution, account_mask, account_type) or insert new account."""
    select_query = """
    SELECT account_id, institution, account_mask, account_domain, account_type, currency
    FROM accounts
    WHERE institution = %s AND account_mask = %s AND account_type = %s;
    """
    with conn.cursor() as cur:
        cur.execute(
            select_query,
            (account.institution, account.account_mask, account.account_type.value),
        )
        row = cur.fetchone()
        if row:
            return Account(
                account_id=UUID(str(row[0])),
                institution=str(row[1]),
                account_mask=str(row[2]),
                account_domain=AccountDomain(str(row[3])),
                account_type=AccountType(str(row[4])),
                currency=CurrencyCode(str(row[5])),
            )

        insert_query = """
        INSERT INTO accounts (account_id, institution, account_mask, account_domain, account_type, currency)
        VALUES (%s, %s, %s, %s, %s, %s);
        """
        cur.execute(
            insert_query,
            (
                str(account.account_id),
                account.institution,
                account.account_mask,
                account.account_domain.value,
                account.account_type.value,
                account.currency.value,
            ),
        )
        return account


def persist_canonical_statement(
    conn: psycopg.Connection,
    account: Account,
    statement: CanonicalStatement,
    summary: DepositorySummary | CreditCardSummary | BrokerageSummary,
    transactions: Sequence[CanonicalTransaction],
    holdings: Sequence[Holding] = (),
) -> None:
    """Atomically persist statement, domain sidecar, transactions, and holdings."""
    # 1. Validate domain matching between account and sidecar
    if account.account_domain == AccountDomain.DEPOSITORY:
        if not isinstance(summary, DepositorySummary):
            raise PersistenceError(
                f"Account domain depository requires DepositorySummary, got {type(summary).__name__}"
            )
    elif account.account_domain == AccountDomain.REVOLVING_CREDIT:
        if not isinstance(summary, CreditCardSummary):
            raise PersistenceError(
                f"Account domain revolving_credit requires CreditCardSummary, got {type(summary).__name__}"
            )
        if statement.opening_balance_cents != summary.previous_balance_cents:
            raise PersistenceError(
                f"statement opening ({statement.opening_balance_cents}) != summary previous ({summary.previous_balance_cents})"
            )
        if statement.closing_balance_cents != summary.new_balance_cents:
            raise PersistenceError(
                f"statement closing ({statement.closing_balance_cents}) != summary new ({summary.new_balance_cents})"
            )
    elif account.account_domain == AccountDomain.CUSTODIAL_BROKERAGE:
        if not isinstance(summary, BrokerageSummary):
            raise PersistenceError(
                f"Account domain custodial_brokerage requires BrokerageSummary, got {type(summary).__name__}"
            )
        if statement.opening_balance_cents != summary.opening_cash_cents:
            raise PersistenceError(
                f"statement opening ({statement.opening_balance_cents}) != summary opening cash ({summary.opening_cash_cents})"
            )
        if statement.closing_balance_cents != summary.closing_cash_cents:
            raise PersistenceError(
                f"statement closing ({statement.closing_balance_cents}) != summary closing cash ({summary.closing_cash_cents})"
            )
    else:
        raise PersistenceError(f"Unsupported account domain: {account.account_domain}")

    if holdings and account.account_domain != AccountDomain.CUSTODIAL_BROKERAGE:
        raise PersistenceError(
            "Holdings are only valid for custodial brokerage statements."
        )

    # 2. Validate denormalized raw_payload_id consistency
    with conn.cursor() as cur:
        cur.execute(
            "SELECT raw_payload_id FROM extraction_runs WHERE run_id = %s;",
            (str(statement.run_id),),
        )
        run_row = cur.fetchone()
        if not run_row:
            raise PersistenceError(
                f"Extraction run {statement.run_id} not found in database."
            )
        db_payload_id = UUID(str(run_row[0]))
        if statement.raw_payload_id != db_payload_id:
            raise PersistenceError(
                f"statement.raw_payload_id ({statement.raw_payload_id}) does not match "
                f"extraction_runs.raw_payload_id ({db_payload_id})"
            )

    # 3. Validate transaction references and sidecar statement_id
    if summary.statement_id != statement.statement_id:
        raise PersistenceError(
            f"summary.statement_id ({summary.statement_id}) does not match statement.statement_id ({statement.statement_id})"
        )
    for txn in transactions:
        if txn.statement_id != statement.statement_id:
            raise PersistenceError(
                f"transaction.statement_id ({txn.statement_id}) does not match statement.statement_id ({statement.statement_id})"
            )

    # 4. Get or create canonical account entity
    persisted_account = get_or_create_account(conn, account)

    # 5. Insert statement
    statement_query = """
    INSERT INTO statements (
        statement_id, account_id, run_id, raw_payload_id,
        statement_start_date, statement_end_date,
        opening_balance_cents, closing_balance_cents, net_change_cents
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
    """
    with conn.cursor() as cur:
        cur.execute(
            statement_query,
            (
                str(statement.statement_id),
                str(persisted_account.account_id),
                str(statement.run_id),
                str(statement.raw_payload_id),
                statement.statement_start_date,
                statement.statement_end_date,
                statement.opening_balance_cents,
                statement.closing_balance_cents,
                statement.net_change_cents,
            ),
        )

        # 6. Insert sidecar
        if isinstance(summary, DepositorySummary):
            cur.execute(
                """
                INSERT INTO statement_depository_summaries (
                    statement_id, deposits_cents, withdrawals_cents, interest_paid_cents, fees_cents
                ) VALUES (%s, %s, %s, %s, %s);
                """,
                (
                    str(summary.statement_id),
                    summary.deposits_cents,
                    summary.withdrawals_cents,
                    summary.interest_paid_cents,
                    summary.fees_cents,
                ),
            )
        elif isinstance(summary, CreditCardSummary):
            if summary.minimum_payment_due_cents is None:
                raise InvariantError(
                    "minimum_payment_due_cents is required; refuse silent zero coercion"
                )
            cur.execute(
                """
                INSERT INTO statement_credit_summaries (
                    statement_id, previous_balance_cents, payments_credits_cents,
                    purchases_cents, cash_advances_cents, balance_transfers_cents,
                    fees_charged_cents, interest_charged_cents, new_balance_cents,
                    minimum_payment_due_cents, payment_due_date
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    str(summary.statement_id),
                    summary.previous_balance_cents,
                    summary.payments_credits_cents,
                    summary.purchases_cents,
                    summary.cash_advances_cents,
                    summary.balance_transfers_cents,
                    summary.fees_charged_cents,
                    summary.interest_charged_cents,
                    summary.new_balance_cents,
                    summary.minimum_payment_due_cents,
                    summary.payment_due_date,
                ),
            )
        elif isinstance(summary, BrokerageSummary):
            cur.execute(
                """
                INSERT INTO statement_brokerage_summaries (
                    statement_id, opening_cash_cents, closing_cash_cents,
                    opening_portfolio_cents, closing_portfolio_cents,
                    realized_gains_cents, unrealized_gains_cents,
                    income_dividends_cents, transfers_in_cents, transfers_out_cents
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    str(summary.statement_id),
                    summary.opening_cash_cents,
                    summary.closing_cash_cents,
                    summary.opening_portfolio_cents,
                    summary.closing_portfolio_cents,
                    summary.realized_gains_cents,
                    summary.unrealized_gains_cents,
                    summary.income_dividends_cents,
                    summary.transfers_in_cents,
                    summary.transfers_out_cents,
                ),
            )

        # 7. Insert transactions
        txn_query = """
        INSERT INTO transactions (
            transaction_id, statement_id, post_date, transaction_date,
            amount_cents, description, transaction_category, balance_after_cents
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
        """
        for txn in transactions:
            cur.execute(
                txn_query,
                (
                    str(txn.transaction_id),
                    str(txn.statement_id),
                    txn.post_date,
                    txn.transaction_date,
                    txn.amount_cents,
                    txn.description,
                    txn.transaction_category.value,
                    txn.balance_after_cents,
                ),
            )

        holding_query = """
        INSERT INTO holdings (
            holding_id, statement_id, as_of_date, symbol, description,
            quantity_nanos, market_value_cents, cost_basis_cents
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
        """
        for holding in holdings:
            cur.execute(
                holding_query,
                (
                    str(holding.holding_id),
                    str(holding.statement_id),
                    holding.as_of_date,
                    holding.symbol,
                    holding.description,
                    holding.quantity_nanos,
                    holding.market_value_cents,
                    holding.cost_basis_cents,
                ),
            )

    # 8. Mark extraction run as canonically persisted
    update_run_status(conn, statement.run_id, RunStatus.CANONICAL_PERSISTED)
    logger.info(
        "Canonical statement persisted successfully",
        extra={"statement_id": str(statement.statement_id)},
    )


# ── Query Utilities ────────────────────────────────────────────────────


def get_raw_payload_by_sha256(
    conn: psycopg.Connection, content_sha256: str
) -> RawPayload | None:
    """Retrieve raw payload by sha256."""
    query = """
    SELECT raw_payload_id, content_sha256, byte_length, original_basename, ingested_at
    FROM raw_payloads
    WHERE content_sha256 = %s;
    """
    with conn.cursor() as cur:
        cur.execute(query, (content_sha256,))
        row = cur.fetchone()
        if not row:
            return None
        return RawPayload(
            raw_payload_id=UUID(str(row[0])),
            content_sha256=str(row[1]),
            byte_length=int(row[2]),
            original_basename=str(row[3]),
            ingested_at=row[4],
        )


def get_extraction_run(conn: psycopg.Connection, run_id: UUID) -> ExtractionRun | None:
    """Retrieve extraction run by run_id."""
    query = """
    SELECT run_id, raw_payload_id, adapter_id, adapter_version, started_at, status, error_code, error_message
    FROM extraction_runs
    WHERE run_id = %s;
    """
    with conn.cursor() as cur:
        cur.execute(query, (str(run_id),))
        row = cur.fetchone()
        if not row:
            return None
        return ExtractionRun(
            run_id=UUID(str(row[0])),
            raw_payload_id=UUID(str(row[1])),
            adapter_id=str(row[2]),
            adapter_version=str(row[3]),
            started_at=row[4],
            status=RunStatus(str(row[5])),
            error_code=str(row[6]) if row[6] is not None else None,
            error_message=str(row[7]) if row[7] is not None else None,
        )


def get_raw_pages(conn: psycopg.Connection, run_id: UUID) -> list[RawPage]:
    """Retrieve raw pages and tokens for a given run_id."""
    page_query = """
    SELECT raw_page_id, run_id, page_number, page_text
    FROM raw_pages
    WHERE run_id = %s
    ORDER BY page_number ASC;
    """
    token_query = """
    SELECT raw_token_id, raw_page_id, token_kind, token_text,
           x0_mp, y0_mp, x1_mp, y1_mp, table_index, row_index, col_index
    FROM raw_tokens
    WHERE raw_page_id = %s;
    """
    pages: list[RawPage] = []
    with conn.cursor() as cur:
        cur.execute(page_query, (str(run_id),))
        page_rows = cur.fetchall()
        for p_row in page_rows:
            page_id = UUID(str(p_row[0]))
            cur.execute(token_query, (str(page_id),))
            token_rows = cur.fetchall()
            tokens = [
                RawToken(
                    raw_token_id=UUID(str(t[0])),
                    raw_page_id=UUID(str(t[1])),
                    token_kind=t[2],
                    token_text=str(t[3]),
                    x0_mp=int(t[4]),
                    y0_mp=int(t[5]),
                    x1_mp=int(t[6]),
                    y1_mp=int(t[7]),
                    table_index=t[8],
                    row_index=t[9],
                    col_index=t[10],
                )
                for t in token_rows
            ]
            pages.append(
                RawPage(
                    raw_page_id=page_id,
                    run_id=UUID(str(p_row[1])),
                    page_number=int(p_row[2]),
                    page_text=str(p_row[3]),
                    tokens=tokens,
                )
            )
    return pages


def get_run_statements(
    conn: psycopg.Connection, run_id: UUID
) -> list[tuple[Account, CanonicalStatement, int]]:
    """Retrieve accounts, statements, and transaction counts for an extraction run."""
    query = """
    SELECT a.account_id, a.institution, a.account_mask, a.account_domain, a.account_type, a.currency,
           s.statement_id, s.raw_payload_id, s.statement_start_date, s.statement_end_date,
           s.opening_balance_cents, s.closing_balance_cents, s.net_change_cents,
           (SELECT COUNT(*) FROM transactions t WHERE t.statement_id = s.statement_id) AS txn_count
    FROM statements s
    JOIN accounts a ON s.account_id = a.account_id
    WHERE s.run_id = %s
    ORDER BY s.statement_start_date ASC;
    """
    results: list[tuple[Account, CanonicalStatement, int]] = []
    with conn.cursor() as cur:
        cur.execute(query, (str(run_id),))
        rows = cur.fetchall()
        for r in rows:
            acc = Account(
                account_id=UUID(str(r[0])),
                institution=str(r[1]),
                account_mask=str(r[2]),
                account_domain=AccountDomain(str(r[3])),
                account_type=AccountType(str(r[4])),
                currency=CurrencyCode(str(r[5])),
            )
            stmt = CanonicalStatement(
                statement_id=UUID(str(r[6])),
                account_id=UUID(str(r[0])),
                run_id=run_id,
                raw_payload_id=UUID(str(r[7])),
                statement_start_date=r[8],
                statement_end_date=r[9],
                opening_balance_cents=int(r[10]),
                closing_balance_cents=int(r[11]),
                net_change_cents=int(r[12]),
            )
            txn_count = int(r[13])
            results.append((acc, stmt, txn_count))
    return results


def get_canonical_statement(
    conn: psycopg.Connection, statement_id: UUID
) -> (
    tuple[
        CanonicalStatement,
        DepositorySummary | CreditCardSummary | BrokerageSummary,
        list[CanonicalTransaction],
    ]
    | None
):
    """Retrieve statement, matching sidecar, and transactions."""
    statement_query = """
    SELECT statement_id, account_id, run_id, raw_payload_id,
           statement_start_date, statement_end_date,
           opening_balance_cents, closing_balance_cents, net_change_cents
    FROM statements
    WHERE statement_id = %s;
    """
    with conn.cursor() as cur:
        cur.execute(statement_query, (str(statement_id),))
        s_row = cur.fetchone()
        if not s_row:
            return None
        statement = CanonicalStatement(
            statement_id=UUID(str(s_row[0])),
            account_id=UUID(str(s_row[1])),
            run_id=UUID(str(s_row[2])),
            raw_payload_id=UUID(str(s_row[3])),
            statement_start_date=s_row[4],
            statement_end_date=s_row[5],
            opening_balance_cents=int(s_row[6]),
            closing_balance_cents=int(s_row[7]),
            net_change_cents=int(s_row[8]),
        )

        # Check depository sidecar
        cur.execute(
            "SELECT statement_id, deposits_cents, withdrawals_cents, interest_paid_cents, fees_cents FROM statement_depository_summaries WHERE statement_id = %s;",
            (str(statement_id),),
        )
        dep_row = cur.fetchone()
        summary: DepositorySummary | CreditCardSummary | BrokerageSummary
        if dep_row:
            summary = DepositorySummary(
                statement_id=UUID(str(dep_row[0])),
                deposits_cents=int(dep_row[1]),
                withdrawals_cents=int(dep_row[2]),
                interest_paid_cents=int(dep_row[3]),
                fees_cents=int(dep_row[4]),
            )
        else:
            cur.execute(
                """
                SELECT statement_id, previous_balance_cents, payments_credits_cents,
                       purchases_cents, cash_advances_cents, balance_transfers_cents,
                       fees_charged_cents, interest_charged_cents, new_balance_cents,
                       minimum_payment_due_cents, payment_due_date
                FROM statement_credit_summaries WHERE statement_id = %s;
                """,
                (str(statement_id),),
            )
            card_row = cur.fetchone()
            if card_row:
                summary = CreditCardSummary(
                    statement_id=UUID(str(card_row[0])),
                    previous_balance_cents=int(card_row[1]),
                    payments_credits_cents=int(card_row[2]),
                    purchases_cents=int(card_row[3]),
                    cash_advances_cents=int(card_row[4]),
                    balance_transfers_cents=int(card_row[5]),
                    fees_charged_cents=int(card_row[6]),
                    interest_charged_cents=int(card_row[7]),
                    new_balance_cents=int(card_row[8]),
                    minimum_payment_due_cents=int(card_row[9]),
                    payment_due_date=card_row[10],
                )
            else:
                cur.execute(
                    """
                    SELECT statement_id, opening_cash_cents, closing_cash_cents,
                           opening_portfolio_cents, closing_portfolio_cents,
                           realized_gains_cents, unrealized_gains_cents,
                           income_dividends_cents, transfers_in_cents, transfers_out_cents
                    FROM statement_brokerage_summaries WHERE statement_id = %s;
                    """,
                    (str(statement_id),),
                )
                brok_row = cur.fetchone()
                if not brok_row:
                    raise PersistenceError(
                        f"No sidecar found for statement {statement_id}"
                    )
                summary = BrokerageSummary(
                    statement_id=UUID(str(brok_row[0])),
                    opening_cash_cents=int(brok_row[1]),
                    closing_cash_cents=int(brok_row[2]),
                    opening_portfolio_cents=int(brok_row[3])
                    if brok_row[3] is not None
                    else None,
                    closing_portfolio_cents=int(brok_row[4])
                    if brok_row[4] is not None
                    else None,
                    realized_gains_cents=int(brok_row[5])
                    if brok_row[5] is not None
                    else None,
                    unrealized_gains_cents=int(brok_row[6])
                    if brok_row[6] is not None
                    else None,
                    income_dividends_cents=int(brok_row[7])
                    if brok_row[7] is not None
                    else None,
                    transfers_in_cents=int(brok_row[8])
                    if brok_row[8] is not None
                    else None,
                    transfers_out_cents=int(brok_row[9])
                    if brok_row[9] is not None
                    else None,
                )

        # Retrieve transactions
        cur.execute(
            """
            SELECT transaction_id, statement_id, post_date, transaction_date,
                   amount_cents, description, transaction_category, balance_after_cents
            FROM transactions
            WHERE statement_id = %s
            ORDER BY post_date ASC;
            """,
            (str(statement_id),),
        )
        txn_rows = cur.fetchall()
        txns = [
            CanonicalTransaction(
                transaction_id=UUID(str(t[0])),
                statement_id=UUID(str(t[1])),
                post_date=t[2],
                transaction_date=t[3],
                amount_cents=int(t[4]),
                description=str(t[5]),
                transaction_category=TransactionCategory(str(t[6])),
                balance_after_cents=int(t[7]) if t[7] is not None else None,
            )
            for t in txn_rows
        ]
        return statement, summary, txns


def get_statement_holdings(
    conn: psycopg.Connection, statement_id: UUID
) -> list[Holding]:
    """Return persisted positions for a statement, ordered by symbol."""
    query = """
    SELECT holding_id, statement_id, as_of_date, symbol, description,
           quantity_nanos, market_value_cents, cost_basis_cents
    FROM holdings
    WHERE statement_id = %s
    ORDER BY symbol ASC;
    """
    with conn.cursor() as cur:
        cur.execute(query, (str(statement_id),))
        rows = cur.fetchall()
    return [
        Holding(
            holding_id=UUID(str(row[0])),
            statement_id=UUID(str(row[1])),
            as_of_date=row[2],
            symbol=str(row[3]),
            description=str(row[4]),
            quantity_nanos=int(row[5]),
            market_value_cents=int(row[6]),
            cost_basis_cents=int(row[7]) if row[7] is not None else None,
        )
        for row in rows
    ]
