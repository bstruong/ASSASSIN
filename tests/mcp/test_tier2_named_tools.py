"""Tests for named Tier-2 tools: monthly aggregates and portfolio bridge."""

from __future__ import annotations

import datetime
import logging
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import psycopg
import pytest

from app.mcp import cloud_tools, local_tools
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


def _assert_no_forbidden_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            assert key not in _FORBIDDEN_KEYS
            _assert_no_forbidden_keys(child)
    elif isinstance(value, Sequence) and not isinstance(value, str):
        for child in value:
            _assert_no_forbidden_keys(child)


def _assert_int(value: object) -> None:
    assert type(value) is int


def _reset(conn: psycopg.Connection) -> None:
    conn.execute(_TRUNCATE_SQL)
    conn.commit()


def _insert_chain(
    conn: psycopg.Connection,
    *,
    domain: str,
    account_type: str,
    opening_balance_cents: int,
    closing_balance_cents: int,
    account_mask: str = "*0000",
) -> uuid.UUID:
    payload_id = uuid.uuid4()
    run_id = uuid.uuid4()
    account_id = uuid.uuid4()
    statement_id = uuid.uuid4()
    digest = uuid.uuid4().hex + uuid.uuid4().hex
    net_change = closing_balance_cents - opening_balance_cents
    conn.execute(
        """
        INSERT INTO raw_payloads (
            raw_payload_id, content_sha256, byte_length,
            original_basename, ingested_at
        ) VALUES (%s, %s, %s, %s, NOW())
        """,
        (payload_id, digest, 128, "synthetic.pdf"),
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
            net_change,
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
    opening_cash_cents: int = 100_000,
    closing_cash_cents: int = 100_000,
    opening_portfolio_cents: int | None,
    closing_portfolio_cents: int | None,
    transfers_in_cents: int | None,
    transfers_out_cents: int | None,
    income_dividends_cents: int | None,
    realized_gains_cents: int | None,
    unrealized_gains_cents: int | None,
    account_mask: str = "*0000",
) -> None:
    statement_id = _insert_chain(
        conn,
        domain="custodial_brokerage",
        account_type="brokerage_cash",
        opening_balance_cents=opening_cash_cents,
        closing_balance_cents=closing_cash_cents,
        account_mask=account_mask,
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
            opening_cash_cents,
            closing_cash_cents,
            opening_portfolio_cents,
            closing_portfolio_cents,
            realized_gains_cents,
            unrealized_gains_cents,
            income_dividends_cents,
            transfers_in_cents,
            transfers_out_cents,
        ),
    )


class TestGetMonthlyAggregates:
    def test_bad_month_key_raises(self) -> None:
        with pytest.raises(CloudToolError, match="Invalid month key"):
            get_monthly_aggregates(month="2026-13")

    @pytest.mark.parametrize(
        "month",
        ["", "2026-00", "2026-1", "2026/01", "not-a-month", "description"],
    )
    def test_malformed_month_keys_raise(self, month: str) -> None:
        with pytest.raises(CloudToolError, match="Invalid month key"):
            get_monthly_aggregates(month=month)

    def test_non_string_month_key_raises(self) -> None:
        with pytest.raises(CloudToolError, match="Invalid month key"):
            get_monthly_aggregates(month=202601)  # type: ignore[arg-type]

    def test_groups_deposits_purchases_withdrawals_by_calendar_month(
        self, db_conn: psycopg.Connection, postgres_url: str
    ) -> None:
        _reset(db_conn)
        statement_id = _insert_chain(
            db_conn,
            domain="depository",
            account_type="checking",
            opening_balance_cents=0,
            closing_balance_cents=7_100,
        )
        _insert_txn(
            db_conn,
            statement_id,
            datetime.date(2026, 1, 15),
            10_000,
            "deposit",
        )
        _insert_txn(
            db_conn,
            statement_id,
            datetime.date(2026, 1, 20),
            2_500,
            "purchase",
        )
        _insert_txn(
            db_conn,
            statement_id,
            datetime.date(2026, 1, 21),
            300,
            "fee",
        )
        _insert_txn(
            db_conn,
            statement_id,
            datetime.date(2026, 2, 2),
            400,
            "withdrawal",
        )
        db_conn.commit()

        result = get_monthly_aggregates(database_url=postgres_url)
        _assert_no_forbidden_keys(result)
        assert result["group_by"] == "calendar_month"
        assert result["month"] is None
        assert "results" not in result
        months = result["months"]
        assert [row["month"] for row in months] == ["2026-01", "2026-02"]
        january, february = months
        assert january == {
            "month": "2026-01",
            "txn_count": 3,
            "deposits_cents": 10_000,
            "purchases_cents": 2_500,
            "withdrawals_cents": 0,
        }
        assert february == {
            "month": "2026-02",
            "txn_count": 1,
            "deposits_cents": 0,
            "purchases_cents": 0,
            "withdrawals_cents": 400,
        }
        for row in months:
            for key in (
                "txn_count",
                "deposits_cents",
                "purchases_cents",
                "withdrawals_cents",
            ):
                _assert_int(row[key])

    def test_month_filter_returns_one_month(
        self, db_conn: psycopg.Connection, postgres_url: str
    ) -> None:
        _reset(db_conn)
        statement_id = _insert_chain(
            db_conn,
            domain="depository",
            account_type="checking",
            opening_balance_cents=0,
            closing_balance_cents=10_400,
        )
        _insert_txn(
            db_conn,
            statement_id,
            datetime.date(2026, 1, 4),
            10_000,
            "deposit",
        )
        _insert_txn(
            db_conn,
            statement_id,
            datetime.date(2026, 2, 4),
            400,
            "withdrawal",
        )
        db_conn.commit()

        result = get_monthly_aggregates(month="2026-01", database_url=postgres_url)
        assert result["month"] == "2026-01"
        assert len(result["months"]) == 1
        assert result["months"][0]["month"] == "2026-01"
        assert result["months"][0]["deposits_cents"] == 10_000
        assert result["months"][0]["withdrawals_cents"] == 0
        _assert_no_forbidden_keys(result)

    def test_valid_month_with_no_rows_is_empty(
        self, db_conn: psycopg.Connection, postgres_url: str
    ) -> None:
        _reset(db_conn)
        db_conn.commit()
        result = get_monthly_aggregates(month="2026-03", database_url=postgres_url)
        assert result["months"] == []
        assert result["month"] == "2026-03"

    def test_logs_month_and_counts_not_amounts(
        self,
        db_conn: psycopg.Connection,
        postgres_url: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _reset(db_conn)
        statement_id = _insert_chain(
            db_conn,
            domain="depository",
            account_type="checking",
            opening_balance_cents=0,
            closing_balance_cents=10_000,
        )
        _insert_txn(
            db_conn,
            statement_id,
            datetime.date(2026, 1, 9),
            10_000,
            "deposit",
        )
        db_conn.commit()

        caplog.set_level(logging.INFO, logger="app.mcp.cloud_tools")
        get_monthly_aggregates(month="2026-01", database_url=postgres_url)
        records = [
            record for record in caplog.records if record.name == "app.mcp.cloud_tools"
        ]
        assert any(getattr(record, "month", None) == "2026-01" for record in records)
        assert any(getattr(record, "txn_count", None) == 1 for record in records)
        rendered = caplog.text
        assert "10000" not in rendered
        assert "10_000" not in rendered
        assert "SYNTHETIC" not in rendered
        assert "*0000" not in rendered

    def test_does_not_call_tier1_sql(
        self,
        db_conn: psycopg.Connection,
        postgres_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def boom(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("Tier-1 or raw result SQL must not be called")

        monkeypatch.setattr(local_tools, "execute_raw_sql", boom)
        monkeypatch.setattr(cloud_tools, "execute_cloud_query", boom)
        _reset(db_conn)
        db_conn.commit()
        result = get_monthly_aggregates(database_url=postgres_url)
        assert isinstance(result, dict)
        assert "results" not in result


class TestVerifyPortfolioBridge:
    def test_aggregate_equation_passes(
        self, db_conn: psycopg.Connection, postgres_url: str
    ) -> None:
        _reset(db_conn)
        # Individually unbalanced rows that balance only in aggregate.
        _insert_brokerage(
            db_conn,
            opening_portfolio_cents=10_000,
            transfers_in_cents=5_000,
            transfers_out_cents=0,
            income_dividends_cents=0,
            realized_gains_cents=0,
            unrealized_gains_cents=0,
            closing_portfolio_cents=14_000,
        )
        _insert_brokerage(
            db_conn,
            account_mask="*0001",
            opening_portfolio_cents=20_000,
            transfers_in_cents=0,
            transfers_out_cents=100,
            income_dividends_cents=500,
            realized_gains_cents=-25,
            unrealized_gains_cents=1_625,
            closing_portfolio_cents=23_000,
        )
        db_conn.commit()

        result = verify_portfolio_bridge(database_url=postgres_url)
        _assert_no_forbidden_keys(result)
        assert result["passed"] is True
        assert result["statement_count"] == 2
        assert result["opening_portfolio_cents"] == 30_000
        assert result["transfers_in_cents"] == 5_000
        assert result["transfers_out_cents"] == 100
        assert result["income_dividends_cents"] == 500
        assert result["realized_gains_cents"] == -25
        assert result["unrealized_gains_cents"] == 1_625
        assert result["closing_portfolio_cents"] == 37_000
        computed = (
            result["opening_portfolio_cents"]
            + result["transfers_in_cents"]
            - result["transfers_out_cents"]
            + result["income_dividends_cents"]
            + result["realized_gains_cents"]
            + result["unrealized_gains_cents"]
        )
        assert computed == 37_000
        assert result["computed_closing_portfolio_cents"] == computed
        for key, value in result.items():
            if key == "passed":
                assert type(value) is bool
            else:
                _assert_int(value)

    def test_one_cent_mismatch_raises(
        self, db_conn: psycopg.Connection, postgres_url: str
    ) -> None:
        _reset(db_conn)
        _insert_brokerage(
            db_conn,
            opening_portfolio_cents=10_000,
            transfers_in_cents=1_000,
            transfers_out_cents=0,
            income_dividends_cents=500,
            realized_gains_cents=100,
            unrealized_gains_cents=200,
            closing_portfolio_cents=11_801,
        )
        db_conn.commit()
        visible = db_conn.execute(
            "SELECT COUNT(*) FROM statement_brokerage_summaries"
        ).fetchone()
        assert visible is not None
        assert visible[0] == 1
        with pytest.raises(CloudToolError, match="Portfolio bridge mismatch"):
            verify_portfolio_bridge(database_url=postgres_url)

    def test_null_bridge_raises(
        self, db_conn: psycopg.Connection, postgres_url: str
    ) -> None:
        _reset(db_conn)
        _insert_brokerage(
            db_conn,
            opening_portfolio_cents=None,
            closing_portfolio_cents=None,
            transfers_in_cents=None,
            transfers_out_cents=None,
            income_dividends_cents=None,
            realized_gains_cents=None,
            unrealized_gains_cents=None,
        )
        db_conn.commit()
        with pytest.raises(CloudToolError, match="Portfolio bridge is absent"):
            verify_portfolio_bridge(database_url=postgres_url)

    def test_null_component_is_not_treated_as_zero(
        self, db_conn: psycopg.Connection, postgres_url: str
    ) -> None:
        _reset(db_conn)
        # Treating transfers_in NULL as 0 would make this equation pass.
        _insert_brokerage(
            db_conn,
            opening_portfolio_cents=1_000,
            transfers_in_cents=None,
            transfers_out_cents=0,
            income_dividends_cents=0,
            realized_gains_cents=0,
            unrealized_gains_cents=0,
            closing_portfolio_cents=1_000,
        )
        db_conn.commit()
        with pytest.raises(CloudToolError, match="Portfolio bridge is absent"):
            verify_portfolio_bridge(database_url=postgres_url)

    def test_mixed_null_and_balanced_rows_fail(
        self, db_conn: psycopg.Connection, postgres_url: str
    ) -> None:
        _reset(db_conn)
        _insert_brokerage(
            db_conn,
            opening_portfolio_cents=5_000,
            transfers_in_cents=0,
            transfers_out_cents=0,
            income_dividends_cents=0,
            realized_gains_cents=0,
            unrealized_gains_cents=0,
            closing_portfolio_cents=5_000,
        )
        _insert_brokerage(
            db_conn,
            account_mask="*0001",
            opening_portfolio_cents=None,
            closing_portfolio_cents=None,
            transfers_in_cents=None,
            transfers_out_cents=None,
            income_dividends_cents=None,
            realized_gains_cents=None,
            unrealized_gains_cents=None,
        )
        db_conn.commit()
        with pytest.raises(CloudToolError, match="Portfolio bridge is absent"):
            verify_portfolio_bridge(database_url=postgres_url)

    def test_no_rows_means_bridge_absent(
        self, db_conn: psycopg.Connection, postgres_url: str
    ) -> None:
        _reset(db_conn)
        db_conn.commit()
        with pytest.raises(CloudToolError, match="Portfolio bridge is absent"):
            verify_portfolio_bridge(database_url=postgres_url)

    def test_does_not_call_tier1_sql(
        self,
        db_conn: psycopg.Connection,
        postgres_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def boom(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("Tier-1 or raw result SQL must not be called")

        monkeypatch.setattr(local_tools, "execute_raw_sql", boom)
        monkeypatch.setattr(cloud_tools, "execute_cloud_query", boom)
        _reset(db_conn)
        _insert_brokerage(
            db_conn,
            opening_portfolio_cents=100,
            transfers_in_cents=0,
            transfers_out_cents=0,
            income_dividends_cents=0,
            realized_gains_cents=0,
            unrealized_gains_cents=0,
            closing_portfolio_cents=100,
        )
        db_conn.commit()
        result = verify_portfolio_bridge(database_url=postgres_url)
        assert result["passed"] is True
        assert "results" not in result

    def test_logs_counts_not_amounts(
        self,
        db_conn: psycopg.Connection,
        postgres_url: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _reset(db_conn)
        _insert_brokerage(
            db_conn,
            opening_portfolio_cents=10_000,
            transfers_in_cents=0,
            transfers_out_cents=0,
            income_dividends_cents=0,
            realized_gains_cents=0,
            unrealized_gains_cents=0,
            closing_portfolio_cents=10_000,
        )
        db_conn.commit()
        caplog.set_level(logging.INFO, logger="app.mcp.cloud_tools")
        verify_portfolio_bridge(database_url=postgres_url)
        assert "10000" not in caplog.text
        assert "*0000" not in caplog.text
        assert "synthetic.pdf" not in caplog.text
