#!/usr/bin/env python3
"""QA verification for named Tier-2 cloud tools.

Checks get_monthly_aggregates and verify_portfolio_bridge:
  - Happy path: calendar-month SUM/COUNT in integer cents, no transaction rows.
  - Happy path: aggregate portfolio bridge equation holds.
  - Loud failure: bad month key, null bridge, and a 1-cent mismatch.

Manual UI QA: not applicable (no user-visible UI).

Run with: uv run python scripts/qa_tier2_named_tools.py

Uses postgresql://postgres:assassin@localhost:54329/assassin_test and resets
canonical tables in that test database before and after the checks.
"""

from __future__ import annotations

import datetime
import os
import sys
import uuid
from collections.abc import Mapping, Sequence

import psycopg

from app.db.connection import init_db
from app.mcp.cloud_tools import (
    CloudToolError,
    get_monthly_aggregates,
    verify_portfolio_bridge,
)

_FORBIDDEN_KEYS = frozenset(
    {
        "description",
        "account_mask",
        "page_text",
        "original_basename",
        "content_sha256",
        "results",
    }
)

_TRUNCATE_SQL = """
TRUNCATE TABLE raw_tokens, raw_pages, transactions, holdings,
               statement_depository_summaries, statement_credit_summaries,
               statement_brokerage_summaries, statements, accounts,
               extraction_runs, raw_payloads CASCADE;
"""


class QAReport:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[str] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        label = "[PASS]" if ok else "[FAIL]"
        suffix = f": {detail}" if detail else ""
        print(f"  {label} {name}{suffix}")
        if ok:
            self.passed.append(name)
        else:
            self.failed.append(name)

    def ok(self) -> bool:
        return not self.failed


def _database_url() -> str:
    return (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or "postgresql://postgres:assassin@localhost:54329/assassin_test"
    )


def _reset(conn: psycopg.Connection, attempts: int = 3) -> None:
    """Truncate canonical tables, retrying if another session holds a lock."""
    last_error: psycopg.Error | None = None
    for _ in range(attempts):
        try:
            conn.execute(_TRUNCATE_SQL)
            conn.commit()
            return
        except psycopg.Error as exc:
            last_error = exc
            conn.rollback()
    if last_error is not None:
        raise last_error


def _has_forbidden_key(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            key in _FORBIDDEN_KEYS or _has_forbidden_key(child)
            for key, child in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, str):
        return any(_has_forbidden_key(child) for child in value)
    return False


def _insert_statement(
    conn: psycopg.Connection,
    *,
    domain: str,
    account_type: str,
    account_mask: str,
    opening_balance_cents: int,
    closing_balance_cents: int,
) -> uuid.UUID:
    payload_id = uuid.uuid4()
    run_id = uuid.uuid4()
    account_id = uuid.uuid4()
    statement_id = uuid.uuid4()
    digest = uuid.uuid4().hex + uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO raw_payloads (
            raw_payload_id, content_sha256, byte_length,
            original_basename, ingested_at
        ) VALUES (%s, %s, %s, %s, NOW())
        """,
        (payload_id, digest, 64, "synthetic.pdf"),
    )
    conn.execute(
        """
        INSERT INTO extraction_runs (
            run_id, raw_payload_id, adapter_id, adapter_version, started_at, status
        ) VALUES (%s, %s, %s, %s, NOW(), %s)
        """,
        (run_id, payload_id, "synthetic", "0.0.0", "validated"),
    )
    conn.execute(
        """
        INSERT INTO accounts (
            account_id, institution, account_mask, account_domain, account_type, currency
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (account_id, "SYNTHETIC Bank", account_mask, domain, account_type, "USD"),
    )
    conn.execute(
        """
        INSERT INTO statements (
            statement_id, account_id, run_id, raw_payload_id,
            statement_start_date, statement_end_date,
            opening_balance_cents, closing_balance_cents, net_change_cents
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            statement_id,
            account_id,
            run_id,
            payload_id,
            datetime.date(2026, 1, 1),
            datetime.date(2026, 2, 28),
            opening_balance_cents,
            closing_balance_cents,
            closing_balance_cents - opening_balance_cents,
        ),
    )
    return statement_id


def _insert_txn(
    conn: psycopg.Connection,
    statement_id: uuid.UUID,
    post_date: datetime.date,
    amount_cents: int,
    category: str,
) -> None:
    conn.execute(
        """
        INSERT INTO transactions (
            transaction_id, statement_id, post_date, amount_cents,
            description, transaction_category
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            uuid.uuid4(),
            statement_id,
            post_date,
            amount_cents,
            "SYNTHETIC Payroll",
            category,
        ),
    )


def _insert_brokerage(
    conn: psycopg.Connection,
    *,
    account_mask: str,
    opening_portfolio_cents: int | None,
    closing_portfolio_cents: int | None,
    transfers_in_cents: int | None,
    transfers_out_cents: int | None,
    income_dividends_cents: int | None,
    realized_gains_cents: int | None,
    unrealized_gains_cents: int | None,
) -> None:
    statement_id = _insert_statement(
        conn,
        domain="custodial_brokerage",
        account_type="brokerage_cash",
        account_mask=account_mask,
        opening_balance_cents=50_000,
        closing_balance_cents=50_000,
    )
    conn.execute(
        """
        INSERT INTO statement_brokerage_summaries (
            statement_id, opening_cash_cents, closing_cash_cents,
            opening_portfolio_cents, closing_portfolio_cents,
            realized_gains_cents, unrealized_gains_cents,
            income_dividends_cents, transfers_in_cents, transfers_out_cents
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            statement_id,
            50_000,
            50_000,
            opening_portfolio_cents,
            closing_portfolio_cents,
            realized_gains_cents,
            unrealized_gains_cents,
            income_dividends_cents,
            transfers_in_cents,
            transfers_out_cents,
        ),
    )


def _check_monthly(
    report: QAReport, database_url: str, conn: psycopg.Connection
) -> None:
    print("\nHappy path: monthly SUM/COUNT in integer cents")
    _reset(conn)
    statement_id = _insert_statement(
        conn,
        domain="depository",
        account_type="checking",
        account_mask="*0000",
        opening_balance_cents=0,
        closing_balance_cents=12_200,
    )
    _insert_txn(conn, statement_id, datetime.date(2026, 1, 15), 15_000, "deposit")
    _insert_txn(conn, statement_id, datetime.date(2026, 1, 20), 2_500, "purchase")
    _insert_txn(conn, statement_id, datetime.date(2026, 1, 21), 100, "fee")
    _insert_txn(conn, statement_id, datetime.date(2026, 2, 2), 800, "withdrawal")
    conn.commit()

    result = get_monthly_aggregates(database_url=database_url)
    months = {row["month"]: row for row in result["months"]}
    january = months.get("2026-01")
    february = months.get("2026-02")
    report.record(
        "Monthly aggregate returns two calendar months",
        january is not None and february is not None and "results" not in result,
    )
    report.record(
        "January deposits, purchases, and count are integer cents",
        january is not None
        and january["txn_count"] == 3
        and january["deposits_cents"] == 15_000
        and january["purchases_cents"] == 2_500
        and january["withdrawals_cents"] == 0
        and type(january["deposits_cents"]) is int,
    )
    report.record(
        "February withdrawal total is integer cents",
        february is not None
        and february["txn_count"] == 1
        and february["withdrawals_cents"] == 800
        and type(february["withdrawals_cents"]) is int,
    )
    report.record(
        "Aggregate omits description, account_mask, and row text",
        not _has_forbidden_key(result),
    )

    print("\nLoud failure: bad month key")
    try:
        get_monthly_aggregates(month="2026-13", database_url=database_url)
    except CloudToolError as exc:
        report.record("Bad month key 2026-13 raises CloudToolError", True, str(exc))
    else:
        report.record("Bad month key 2026-13 raises CloudToolError", False)


def _check_bridge(
    report: QAReport, database_url: str, conn: psycopg.Connection
) -> None:
    print("\nHappy path: aggregate portfolio bridge")
    _reset(conn)
    _insert_brokerage(
        conn,
        account_mask="*0000",
        opening_portfolio_cents=10_000,
        transfers_in_cents=1_000,
        transfers_out_cents=0,
        income_dividends_cents=500,
        realized_gains_cents=100,
        unrealized_gains_cents=200,
        closing_portfolio_cents=11_800,
    )
    conn.commit()
    result = verify_portfolio_bridge(database_url=database_url)
    computed = (
        result["opening_portfolio_cents"]
        + result["transfers_in_cents"]
        - result["transfers_out_cents"]
        + result["income_dividends_cents"]
        + result["realized_gains_cents"]
        + result["unrealized_gains_cents"]
    )
    report.record(
        "Portfolio bridge passes with integer component totals",
        result["passed"] is True
        and result["statement_count"] == 1
        and computed == 11_800
        and result["computed_closing_portfolio_cents"] == 11_800
        and result["closing_portfolio_cents"] == 11_800
        and type(result["opening_portfolio_cents"]) is int
        and "results" not in result,
    )

    print("\nLoud failure: null bridge")
    _reset(conn)
    _insert_brokerage(
        conn,
        account_mask="*0000",
        opening_portfolio_cents=None,
        closing_portfolio_cents=None,
        transfers_in_cents=None,
        transfers_out_cents=None,
        income_dividends_cents=None,
        realized_gains_cents=None,
        unrealized_gains_cents=None,
    )
    conn.commit()
    try:
        verify_portfolio_bridge(database_url=database_url)
    except CloudToolError as exc:
        report.record("Null bridge raises CloudToolError", True, str(exc))
    else:
        report.record("Null bridge raises CloudToolError", False)

    print("\nLoud failure: 1-cent mismatch")
    _reset(conn)
    _insert_brokerage(
        conn,
        account_mask="*0000",
        opening_portfolio_cents=10_000,
        transfers_in_cents=1_000,
        transfers_out_cents=0,
        income_dividends_cents=500,
        realized_gains_cents=100,
        unrealized_gains_cents=200,
        closing_portfolio_cents=11_801,
    )
    conn.commit()
    try:
        verify_portfolio_bridge(database_url=database_url)
    except CloudToolError as exc:
        report.record("1-cent mismatch raises CloudToolError", True, str(exc))
    else:
        report.record("1-cent mismatch raises CloudToolError", False)


def main() -> int:
    print("=" * 60)
    print("Feature: named Tier-2 cloud tools")
    print("get_monthly_aggregates and verify_portfolio_bridge")
    print("Manual UI QA: not applicable (no user-visible UI).")
    print("=" * 60)

    report = QAReport()
    database_url = _database_url()
    conn = psycopg.connect(database_url)
    try:
        init_db(conn)
        _check_monthly(report, database_url, conn)
        _check_bridge(report, database_url, conn)
    finally:
        try:
            _reset(conn)
        except psycopg.Error as exc:
            report.record("Test database cleanup", False, type(exc).__name__)
        conn.close()

    total = len(report.passed) + len(report.failed)
    print(f"\n{'=' * 60}")
    print(f"QA Summary: {len(report.passed)}/{total} passed")
    if report.failed:
        print("Failed checks:")
        for name in report.failed:
            print(f"  - {name}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
