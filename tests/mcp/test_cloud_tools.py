"""Tests for app.mcp.cloud_tools (Tier 2 MCP Tools)."""

from __future__ import annotations

import pytest

from app.mcp.cloud_tools import (
    CloudToolError,
    _validate_cloud_sql,
    _validate_parameters,
    execute_cloud_query,
    get_financial_summary,
    get_schema_info,
)

# ── _validate_cloud_sql ──────────────────────────────────────────────


class TestValidateCloudSql:
    def test_empty_string_raises(self):
        with pytest.raises(CloudToolError, match="must not be empty"):
            _validate_cloud_sql("")

    def test_select_allowed(self):
        result = _validate_cloud_sql("SELECT * FROM accounts;")
        assert "SELECT" in result

    def test_insert_denied(self):
        with pytest.raises(CloudToolError, match="only allow SELECT"):
            _validate_cloud_sql("INSERT INTO accounts VALUES (1);")

    def test_update_denied(self):
        with pytest.raises(CloudToolError, match="only allow SELECT"):
            _validate_cloud_sql("UPDATE accounts SET x = 1;")

    def test_delete_denied(self):
        with pytest.raises(CloudToolError, match="only allow SELECT"):
            _validate_cloud_sql("DELETE FROM accounts;")

    def test_drop_denied(self):
        with pytest.raises(CloudToolError, match="only allow SELECT"):
            _validate_cloud_sql("DROP TABLE accounts;")

    def test_allowed_table(self):
        result = _validate_cloud_sql("SELECT COUNT(*) FROM transactions;")
        assert "COUNT" in result

    def test_disallowed_table_raises(self):
        with pytest.raises(CloudToolError, match="cannot query table"):
            _validate_cloud_sql("SELECT * FROM raw_pages;")

    def test_injection_comment_raises(self):
        with pytest.raises(CloudToolError, match="injection"):
            _validate_cloud_sql("SELECT 1; -- malicious")

    def test_injection_drop_raises(self):
        with pytest.raises(CloudToolError, match="injection"):
            _validate_cloud_sql("SELECT 1; DROP TABLE foo;")

    def test_or1eq1_raises(self):
        with pytest.raises(CloudToolError, match="injection"):
            _validate_cloud_sql("SELECT * FROM accounts WHERE 1=1 OR 1 = 1")

    def test_count_aggregation_allowed(self):
        result = _validate_cloud_sql("SELECT COUNT(*) FROM transactions;")
        assert "COUNT" in result

    def test_sum_aggregation_allowed(self):
        result = _validate_cloud_sql("SELECT SUM(amount_cents) FROM transactions;")
        assert "SUM" in result

    def test_avg_aggregation_allowed(self):
        result = _validate_cloud_sql("SELECT AVG(amount_cents) FROM transactions;")
        assert "AVG" in result

    def test_min_aggregation_allowed(self):
        _validate_cloud_sql("SELECT MIN(amount_cents) FROM transactions;")

    def test_max_aggregation_allowed(self):
        _validate_cloud_sql("SELECT MAX(amount_cents) FROM transactions;")

    def test_group_by_allowed(self):
        result = _validate_cloud_sql(
            "SELECT transaction_category, COUNT(*) FROM transactions GROUP BY transaction_category;"
        )
        assert "GROUP BY" in result

    def test_join_allowed(self):
        result = _validate_cloud_sql(
            "SELECT a.account_mask, COUNT(*) FROM transactions t JOIN accounts a ON t.statement_id = a.account_id GROUP BY a.account_mask;"
        )
        assert "JOIN" in result


# ── _validate_parameters ─────────────────────────────────────────────


class TestValidateParametersCloud:
    def test_none_returns_empty_tuple(self):
        assert _validate_parameters(None) == ()

    def test_empty_list(self):
        assert len(_validate_parameters([])) == 0

    def test_valid_params(self):
        result = _validate_parameters([1, "text", None])
        assert len(result) == 3

    def test_non_sequence_raises(self):
        with pytest.raises(CloudToolError, match="must be a list"):
            _validate_parameters("not a sequence")

    def test_unsupported_type_raises(self):
        with pytest.raises(CloudToolError, match="unsupported type"):
            _validate_parameters([object()])

    def test_injection_in_parameter_raises(self):
        with pytest.raises(CloudToolError, match="injection"):
            _validate_parameters(["foo; DROP TABLE bar"])


# ── execute_cloud_query ──────────────────────────────────────────────


class TestExecuteCloudQuery:
    def test_select_returns_rows(self, postgres_url):
        result = execute_cloud_query(
            "SELECT 1 AS val;",
            database_url=postgres_url,
        )
        assert isinstance(result, list)
        assert result[0]["val"] == 1

    def test_count_query(self, postgres_url):
        result = execute_cloud_query(
            "SELECT COUNT(*) AS cnt FROM extraction_runs;",
            database_url=postgres_url,
        )
        assert isinstance(result, list)
        assert "cnt" in result[0]

    def test_parameterized_query(self, postgres_url):
        result = execute_cloud_query(
            "SELECT %s AS val;",
            parameters=[42],
            database_url=postgres_url,
        )
        assert result[0]["val"] == 42

    def test_denied_insert_raises(self, postgres_url):
        with pytest.raises(CloudToolError, match="only allow SELECT"):
            execute_cloud_query(
                "INSERT INTO accounts VALUES (1);", database_url=postgres_url
            )

    def test_denied_update_raises(self, postgres_url):
        with pytest.raises(CloudToolError, match="only allow SELECT"):
            execute_cloud_query("UPDATE accounts SET x = 1;", database_url=postgres_url)

    def test_denied_table_raises(self, postgres_url):
        with pytest.raises(CloudToolError, match="cannot query table"):
            execute_cloud_query("SELECT * FROM raw_pages;", database_url=postgres_url)

    def test_injection_in_sql_raises(self, postgres_url):
        with pytest.raises(CloudToolError, match="injection"):
            execute_cloud_query("SELECT 1; DROP TABLE foo;", database_url=postgres_url)

    def test_injection_in_parameter_raises(self, postgres_url):
        with pytest.raises(CloudToolError, match="injection"):
            execute_cloud_query(
                "SELECT %s;",
                parameters=["foo; DROP TABLE bar"],
                database_url=postgres_url,
            )

    def test_invalid_parameter_type_raises(self, postgres_url):
        with pytest.raises(CloudToolError, match="unsupported type"):
            execute_cloud_query(
                "SELECT %s;",
                parameters=[object()],
                database_url=postgres_url,
            )


# ── get_schema_info ──────────────────────────────────────────────────


class TestGetSchemaInfo:
    def test_all_tables(self, postgres_url):
        result = get_schema_info(database_url=postgres_url)
        assert "tables" in result
        assert isinstance(result["tables"], list)
        assert "accounts" in result["tables"]
        assert "transactions" in result["tables"]
        assert "statements" in result["tables"]

    def test_specific_table(self, postgres_url):
        result = get_schema_info(table_name="transactions", database_url=postgres_url)
        assert result["table"] == "transactions"
        assert "columns" in result
        assert isinstance(result["columns"], list)
        col_names = [c["name"] for c in result["columns"]]
        assert "transaction_id" in col_names
        assert "amount_cents" in col_names

    def test_disallowed_table_raises(self):
        with pytest.raises(CloudToolError, match="cannot introspect table"):
            get_schema_info(table_name="raw_pages")

    def test_disallowed_table_case_insensitive(self):
        with pytest.raises(CloudToolError, match="cannot introspect table"):
            get_schema_info(table_name="RAW_PAGES")


# ── get_financial_summary ────────────────────────────────────────────


class TestGetFinancialSummary:
    def test_valid_group_by(self, postgres_url):
        result = get_financial_summary(
            group_by="transaction_category",
            database_url=postgres_url,
        )
        assert "group_by_column" in result
        assert result["group_by_column"] == "transaction_category"
        assert "totals" in result
        assert "per_group" in result
        totals = result["totals"]
        assert "total_transactions" in totals
        assert "total_deposits_cents" in totals
        assert "total_purchases_cents" in totals
        assert "total_withdrawals_cents" in totals

    def test_invalid_group_by_raises(self):
        with pytest.raises(CloudToolError, match="Invalid group_by column"):
            get_financial_summary(group_by="nonexistent_column")

    def test_disallowed_group_by_raises(self):
        with pytest.raises(CloudToolError, match="Invalid group_by column"):
            get_financial_summary(group_by="raw_page_id")

    def test_group_by_description(self, postgres_url):
        result = get_financial_summary(
            group_by="description",
            database_url=postgres_url,
        )
        assert result["group_by_column"] == "description"
        assert "totals" in result
        assert "per_group" in result

    def test_group_by_statement_id(self, postgres_url):
        result = get_financial_summary(
            group_by="statement_id",
            database_url=postgres_url,
        )
        assert result["group_by_column"] == "statement_id"

    def test_per_group_structure(self, postgres_url):
        result = get_financial_summary(
            group_by="transaction_category",
            database_url=postgres_url,
        )
        for group in result["per_group"]:
            assert "txn_count" in group
            assert "deposits_cents" in group
            assert "purchases_cents" in group
            assert "withdrawals_cents" in group
