"""Tier 2 MCP Tools: Aggregation queries and schema introspection.

These tools are accessible to cloud models with restricted capabilities.
They provide:
- Safe aggregation queries (SUM, COUNT, GROUP BY) only.
- Database schema introspection (table/column listings).
- Aggregated financial summaries without raw transaction details.

All tools validate inputs and fail loudly on invalid payloads.
No PII or unmasked financial amounts are logged.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Any

from app.db.connection import get_connection
from app.models.exceptions import PipelineError

logger = logging.getLogger("app.mcp.cloud_tools")

_ALLOWED_CLOUD_PREFIXES = frozenset({"SELECT"})

_ALLOWED_AGGREGATIONS = frozenset(
    {
        "SUM",
        "COUNT",
        "AVG",
        "MIN",
        "MAX",
        "SUM_DISTINCT",
        "COUNT_DISTINCT",
    }
)

_ALLOWED_TABLES = frozenset(
    {
        "statements",
        "accounts",
        "extraction_runs",
        "transactions",
        "statement_depository_summaries",
        "statement_credit_summaries",
        "statement_brokerage_summaries",
    }
)

_INJECTION_PATTERNS = re.compile(
    r"(?:--\s|;\s*DROP\s|;\s*ALTER\s|;\s*DELETE\s|;\s*INSERT\s|;\s*UPDATE\s|\bOR\b\s+\d+\s*=.*\b\d+)",
    re.IGNORECASE,
)

_FORBIDDEN_ROW_COLUMNS = frozenset(
    {
        "description",
        "account_mask",
        "page_text",
        "original_basename",
        "content_sha256",
    }
)

_RAW_TABLE_PATTERN = re.compile(r"\braw_\w+\b", re.IGNORECASE)


class CloudToolError(PipelineError):
    """Raised when a cloud tool input validation or execution fails."""


def _validate_cloud_sql(sql: str) -> str:
    """Validate a cloud-tier SQL statement.

    Enforces: SELECT only, required aggregations, no SELECT *, no raw_* JOINs,
    no row-level PII columns, allowed tables only, no injection.
    Raises CloudToolError if validation fails.
    """
    stripped = sql.strip()
    if not stripped:
        raise CloudToolError("SQL statement must not be empty.")
    first_word = stripped.split()[0].upper()
    if first_word not in _ALLOWED_CLOUD_PREFIXES:
        raise CloudToolError(
            f"Cloud tools only allow SELECT statements. Got: {first_word!r}"
        )
    if _INJECTION_PATTERNS.search(stripped):
        raise CloudToolError("SQL statement contains potential injection pattern.")

    if re.search(r"SELECT\s+\*", stripped, re.IGNORECASE):
        raise CloudToolError(
            "Cloud tools forbid SELECT *; use explicit aggregate expressions."
        )

    # Ban JOIN (and comma-join) to any raw_* table
    if _RAW_TABLE_PATTERN.search(stripped):
        raise CloudToolError(
            "Cloud tools forbid referencing raw_* tables (including JOINs)."
        )

    join_targets = re.findall(
        r"\bJOIN\s+(\w+)",
        stripped,
        re.IGNORECASE,
    )
    for table in join_targets:
        if table.lower().startswith("raw_"):
            raise CloudToolError(f"Cloud tools forbid JOIN to raw table '{table}'.")
        if table.lower() not in {t.lower() for t in _ALLOWED_TABLES}:
            raise CloudToolError(
                f"Cloud tools cannot join table '{table}'. "
                f"Allowed tables: {sorted(_ALLOWED_TABLES)}"
            )

    from_tables = re.findall(r"\bFROM\s+(\w+)", stripped, re.IGNORECASE)
    for table in from_tables:
        if table.lower() not in {t.lower() for t in _ALLOWED_TABLES}:
            raise CloudToolError(
                f"Cloud tools cannot query table '{table}'. "
                f"Allowed tables: {sorted(_ALLOWED_TABLES)}"
            )

    # Comma joins: FROM a, b
    comma_join = re.search(
        r"\bFROM\s+\w+\s*,\s*(\w+)",
        stripped,
        re.IGNORECASE,
    )
    if comma_join:
        joined = comma_join.group(1)
        if joined.lower().startswith("raw_") or joined.lower() not in {
            t.lower() for t in _ALLOWED_TABLES
        }:
            raise CloudToolError(f"Cloud tools forbid comma-join to table '{joined}'.")

    for col in _FORBIDDEN_ROW_COLUMNS:
        if re.search(rf"\b{col}\b", stripped, re.IGNORECASE):
            raise CloudToolError(
                f"Cloud tools forbid selecting row-level column '{col}'."
            )

    aggs_found = re.findall(
        r"\b(SUM|COUNT|AVG|MIN|MAX|SUM_DISTINCT|COUNT_DISTINCT)\s*\(",
        stripped,
        re.IGNORECASE,
    )
    if not aggs_found:
        raise CloudToolError(
            "Cloud tools require at least one aggregation "
            f"({sorted(_ALLOWED_AGGREGATIONS)})."
        )
    for agg in aggs_found:
        if agg.upper() not in _ALLOWED_AGGREGATIONS:
            raise CloudToolError(
                f"Cloud tools only allow aggregations: {sorted(_ALLOWED_AGGREGATIONS)}. Got: {agg!r}"
            )
    return stripped


def _validate_parameters(params: Sequence[Any] | None) -> Sequence[Any]:
    """Validate that query parameters are a simple sequence of basic types."""
    if params is None:
        return ()
    if not isinstance(params, (list, tuple)):
        raise CloudToolError("Query parameters must be a list or tuple.")
    for idx, param in enumerate(params):
        if not isinstance(param, (str, int, float, bool, type(None))):
            raise CloudToolError(
                f"Parameter at index {idx} has unsupported type: {type(param).__name__}"
            )
        if isinstance(param, str) and _INJECTION_PATTERNS.search(param):
            raise CloudToolError(
                f"Parameter at index {idx} contains potential injection pattern."
            )
    return params


def execute_cloud_query(
    sql: str,
    parameters: Sequence[Any] | None = None,
    database_url: str | None = None,
) -> list[dict[str, Any]]:
    """Execute a validated aggregation SQL query and return results as list of dicts.

    Tier 2 tool accessible to cloud models with restricted capabilities.

    Args:
        sql: A SELECT-only SQL statement with allowed aggregations.
        parameters: Optional sequence of parameter values.
        database_url: Optional override for the database connection URL.

    Returns:
        List of dicts, one per row, with column names as keys.

    Raises:
        CloudToolError: If SQL validation fails.
        psycopg.Error: If a database error occurs.
    """
    validated_sql = _validate_cloud_sql(sql)
    validated_params = _validate_parameters(parameters)

    logger.info(
        "Executing cloud query (Tier 2)",
        extra={
            "sql_prefix": validated_sql[:80],
            "param_count": len(validated_params),
        },
    )

    conn = get_connection(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(validated_sql, validated_params)
            if not cur.description:
                return [{"status": "success"}]
            columns = [desc.name for desc in cur.description]
            rows = cur.fetchall()
            result = [dict(zip(columns, row)) for row in rows]
            logger.info("Cloud query returned rows", extra={"row_count": len(result)})
            return result
    finally:
        conn.close()


def get_schema_info(
    table_name: str | None = None,
    database_url: str | None = None,
) -> dict[str, Any]:
    """Return database schema introspection results.

    Tier 2 tool accessible to cloud models. Returns table and column metadata
    without exposing any data or PII.

    Args:
        table_name: Optional specific table to introspect. If None, returns all.

    Returns:
        Dict with:
        - ``tables``: List of table names (and columns if table_name specified).

    Raises:
        CloudToolError: If table_name references a disallowed table.
    """
    if table_name is not None and table_name.lower() not in {
        t.lower() for t in _ALLOWED_TABLES
    }:
        raise CloudToolError(
            f"Cloud tools cannot introspect table '{table_name}'. "
            f"Allowed tables: {sorted(_ALLOWED_TABLES)}"
        )

    logger.info(
        "Schema introspection requested (Tier 2)",
        extra={
            "table_name": table_name,
        },
    )

    conn = get_connection(database_url)
    try:
        with conn.cursor() as cur:
            if table_name is None:
                cur.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' "
                    "AND table_type = 'BASE TABLE' "
                    "ORDER BY table_name;"
                )
                tables = [row[0] for row in cur.fetchall()]
                return {"tables": tables}
            else:
                cur.execute(
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_name = %s "
                    "ORDER BY ordinal_position;",
                    (table_name,),
                )
                columns = [
                    {
                        "name": row[0],
                        "data_type": row[1],
                        "nullable": row[2] == "YES",
                    }
                    for row in cur.fetchall()
                ]
                return {"table": table_name, "columns": columns}
    finally:
        conn.close()


def get_financial_summary(
    group_by: str,
    database_url: str | None = None,
) -> dict[str, Any]:
    """Return aggregated financial summaries grouped by the specified column.

    Tier 2 tool accessible to cloud models. Returns high-level aggregates
    without raw transaction details or PII.

    Args:
        group_by: Column name to group by. Must reference an allowed column
                  from the transactions or statements tables.
        database_url: Optional override for the database connection URL.

    Returns:
        Dict with:
        - ``group_by_column``: The column used for grouping.
        - ``total_transactions``: COUNT of all transactions.
        - ``total_deposits_cents``: SUM of deposit amounts.
        - ``total_purchases_cents``: SUM of purchase amounts.
        - ``total_withdrawals_cents``: SUM of withdrawal amounts.
        - ``per_group``: Per-group aggregated stats.

    Raises:
        CloudToolError: If group_by references an invalid column or table.
    """
    allowed_group_columns = frozenset(
        {
            "transaction_category",
            "statement_id",
            "account_id",
        }
    )
    if group_by not in allowed_group_columns:
        raise CloudToolError(
            f"Invalid group_by column: {group_by!r}. "
            f"Allowed: {sorted(allowed_group_columns)}"
        )

    logger.info(
        "Financial summary requested (Tier 2)",
        extra={
            "group_by": group_by,
        },
    )

    conn = get_connection(database_url)
    try:
        with conn.cursor() as cur:
            # Total aggregates across all transactions
            cur.execute(
                "SELECT "
                "  COUNT(*) AS total_transactions, "
                "  COALESCE(SUM(CASE WHEN transaction_category = 'deposit' THEN amount_cents ELSE 0 END), 0) AS total_deposits_cents, "
                "  COALESCE(SUM(CASE WHEN transaction_category = 'purchase' THEN amount_cents ELSE 0 END), 0) AS total_purchases_cents, "
                "  COALESCE(SUM(CASE WHEN transaction_category = 'withdrawal' THEN amount_cents ELSE 0 END), 0) AS total_withdrawals_cents "
                "FROM transactions;"
            )
            totals_row = cur.fetchone()
            totals = {
                "total_transactions": int(totals_row[0]),
                "total_deposits_cents": int(totals_row[1]),
                "total_purchases_cents": int(totals_row[2]),
                "total_withdrawals_cents": int(totals_row[3]),
            }

            # Per-group aggregates
            cur.execute(
                f"SELECT {group_by}, COUNT(*) AS txn_count, "
                "  COALESCE(SUM(CASE WHEN transaction_category = 'deposit' THEN amount_cents ELSE 0 END), 0) AS deposits_cents, "
                "  COALESCE(SUM(CASE WHEN transaction_category = 'purchase' THEN amount_cents ELSE 0 END), 0) AS purchases_cents, "
                "  COALESCE(SUM(CASE WHEN transaction_category = 'withdrawal' THEN amount_cents ELSE 0 END), 0) AS withdrawals_cents "
                f"FROM transactions GROUP BY {group_by} ORDER BY txn_count DESC;"
            )
            per_group = []
            for row in cur.fetchall():
                per_group.append(
                    {
                        group_by: row[0],
                        "txn_count": int(row[1]),
                        "deposits_cents": int(row[2]),
                        "purchases_cents": int(row[3]),
                        "withdrawals_cents": int(row[4]),
                    }
                )

            return {
                "group_by_column": group_by,
                "totals": totals,
                "per_group": per_group,
            }
    finally:
        conn.close()


_MONTH_KEY_PATTERN = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")

_PORTFOLIO_BRIDGE_COLUMNS = (
    "opening_portfolio_cents",
    "transfers_in_cents",
    "transfers_out_cents",
    "income_dividends_cents",
    "realized_gains_cents",
    "unrealized_gains_cents",
    "closing_portfolio_cents",
)


def _require_month_key(month: str | None) -> str | None:
    """Accept only a calendar month key (YYYY-MM) or no filter."""
    if month is None:
        return None
    if not isinstance(month, str) or _MONTH_KEY_PATTERN.fullmatch(month) is None:
        raise CloudToolError(
            f"Invalid month key: {month!r}. Expected calendar month YYYY-MM."
        )
    return month


def _require_int_cents(value: object, label: str) -> int:
    """Reject bools, floats, and nulls. Monetary totals stay integer cents."""
    if type(value) is not int:
        raise CloudToolError(f"{label} must be integer cents.")
    return value


def get_monthly_aggregates(
    month: str | None = None,
    database_url: str | None = None,
) -> dict[str, Any]:
    """Return SUM/COUNT of canonical categories grouped by calendar month.

    Tier 2 tool. Groups ``transactions.post_date`` by YYYY-MM and sums
    integer cents for deposit, purchase, and withdrawal. Other categories
    remain in ``txn_count`` only. No transaction rows and no row-level fields.

    Args:
        month: Optional YYYY-MM filter. Invalid keys fail loudly.
        database_url: Optional override for the database connection URL.

    Returns:
        Dict with ``group_by``, optional ``month`` filter, and ``months``.
        Each month entry has ``txn_count`` plus deposit, purchase, and
        withdrawal totals in integer cents.

    Raises:
        CloudToolError: If ``month`` is not a calendar YYYY-MM key.
    """
    month_key = _require_month_key(month)
    logger.info(
        "Monthly aggregates requested (Tier 2)",
        extra={"month": month_key},
    )

    conn = get_connection(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  to_char(post_date, 'YYYY-MM') AS month,
                  COUNT(*) AS txn_count,
                  (COALESCE(SUM(CASE WHEN transaction_category = 'deposit'
                    THEN amount_cents ELSE 0 END), 0))::bigint AS deposits_cents,
                  (COALESCE(SUM(CASE WHEN transaction_category = 'purchase'
                    THEN amount_cents ELSE 0 END), 0))::bigint AS purchases_cents,
                  (COALESCE(SUM(CASE WHEN transaction_category = 'withdrawal'
                    THEN amount_cents ELSE 0 END), 0))::bigint AS withdrawals_cents
                FROM transactions
                WHERE (%s::text IS NULL OR to_char(post_date, 'YYYY-MM') = %s)
                GROUP BY to_char(post_date, 'YYYY-MM')
                ORDER BY to_char(post_date, 'YYYY-MM')
                """,
                (month_key, month_key),
            )
            months: list[dict[str, Any]] = []
            txn_count = 0
            for row in cur.fetchall():
                entry = {
                    "month": row[0],
                    "txn_count": _require_int_cents(row[1], "txn_count"),
                    "deposits_cents": _require_int_cents(row[2], "deposits_cents"),
                    "purchases_cents": _require_int_cents(row[3], "purchases_cents"),
                    "withdrawals_cents": _require_int_cents(
                        row[4], "withdrawals_cents"
                    ),
                }
                txn_count += entry["txn_count"]
                months.append(entry)
        logger.info(
            "Monthly aggregates completed (Tier 2)",
            extra={
                "month": month_key,
                "month_count": len(months),
                "txn_count": txn_count,
            },
        )
        return {
            "group_by": "calendar_month",
            "month": month_key,
            "months": months,
        }
    finally:
        conn.close()


def verify_portfolio_bridge(
    database_url: str | None = None,
) -> dict[str, Any]:
    """Check the portfolio bridge across statement_brokerage_summaries.

    Aggregate equation, integer cents only::

        opening_portfolio + transfers_in - transfers_out
            + income_dividends + realized_gains + unrealized_gains
            == closing_portfolio

    A null bridge column means the bridge is absent. Nulls are not treated
    as zero. A mismatch raises ``CloudToolError`` instead of returning a
    soft failure.

    Args:
        database_url: Optional override for the database connection URL.

    Returns:
        Pass structure with ``passed`` true, ``statement_count``, and the
        integer component totals including ``computed_closing_portfolio_cents``.

    Raises:
        CloudToolError: If no bridge rows exist, any bridge column is null,
            or the aggregate equation is off by any number of cents.
    """
    logger.info("Portfolio bridge verification requested (Tier 2)")

    conn = get_connection(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  COUNT(*) AS statement_count,
                  COUNT(*) FILTER (
                    WHERE opening_portfolio_cents IS NULL
                       OR closing_portfolio_cents IS NULL
                       OR transfers_in_cents IS NULL
                       OR transfers_out_cents IS NULL
                       OR income_dividends_cents IS NULL
                       OR realized_gains_cents IS NULL
                       OR unrealized_gains_cents IS NULL
                  ) AS null_bridge_count,
                  SUM(opening_portfolio_cents)::bigint AS opening_portfolio_cents,
                  SUM(transfers_in_cents)::bigint AS transfers_in_cents,
                  SUM(transfers_out_cents)::bigint AS transfers_out_cents,
                  SUM(income_dividends_cents)::bigint AS income_dividends_cents,
                  SUM(realized_gains_cents)::bigint AS realized_gains_cents,
                  SUM(unrealized_gains_cents)::bigint AS unrealized_gains_cents,
                  SUM(closing_portfolio_cents)::bigint AS closing_portfolio_cents
                FROM statement_brokerage_summaries
                """
            )
            row = cur.fetchone()
        if row is None:
            raise CloudToolError("Portfolio bridge is absent.")

        statement_count = _require_int_cents(row[0], "statement_count")
        null_bridge_count = _require_int_cents(row[1], "null_bridge_count")
        if statement_count == 0 or null_bridge_count > 0:
            logger.warning(
                "Portfolio bridge check failed (Tier 2)",
                extra={
                    "statement_count": statement_count,
                    "null_bridge_count": null_bridge_count,
                    "failure": "absent",
                },
            )
            raise CloudToolError("Portfolio bridge is absent.")

        components = {
            column: _require_int_cents(row[index + 2], column)
            for index, column in enumerate(_PORTFOLIO_BRIDGE_COLUMNS)
        }
        computed_closing = (
            components["opening_portfolio_cents"]
            + components["transfers_in_cents"]
            - components["transfers_out_cents"]
            + components["income_dividends_cents"]
            + components["realized_gains_cents"]
            + components["unrealized_gains_cents"]
        )
        if computed_closing != components["closing_portfolio_cents"]:
            logger.warning(
                "Portfolio bridge check failed (Tier 2)",
                extra={
                    "statement_count": statement_count,
                    "failure": "mismatch",
                },
            )
            raise CloudToolError("Portfolio bridge mismatch.")

        logger.info(
            "Portfolio bridge verified (Tier 2)",
            extra={"statement_count": statement_count, "passed": True},
        )
        return {
            "passed": True,
            "statement_count": statement_count,
            **components,
            "computed_closing_portfolio_cents": computed_closing,
        }
    finally:
        conn.close()
