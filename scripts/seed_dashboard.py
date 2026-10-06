#!/usr/bin/env python3
"""Seed the local database with raw SQL."""

import uuid
from datetime import UTC, date, datetime

from app.db.connection import get_db_connection


def seed():
    with get_db_connection() as conn, conn.cursor() as cur:
        acc_id = uuid.uuid4()
        run_id = uuid.uuid4()
        payload_id = uuid.uuid4()
        stmt_id = uuid.uuid4()
        now = datetime.now(tz=UTC)

        cur.execute(
            """
                INSERT INTO raw_payloads (raw_payload_id, content_sha256, byte_length, original_basename, ingested_at)
                VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
            """,
            (payload_id, "a" * 64, 100, "dummy.pdf", now),
        )

        cur.execute(
            """
                INSERT INTO extraction_runs (run_id, raw_payload_id, adapter_id, adapter_version, started_at, status)
                VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
            """,
            (run_id, payload_id, "dummy", "1.0", now, "canonical_persisted"),
        )

        cur.execute(
            """
                INSERT INTO accounts (account_id, institution, account_mask, account_domain, account_type, currency)
                VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (acc_id, "Metropolitan Bank", "*4567", "depository", "checking", "USD"),
        )

        cur.execute(
            """
                INSERT INTO statements (statement_id, account_id, run_id, raw_payload_id, statement_start_date, statement_end_date, opening_balance_cents, closing_balance_cents, net_change_cents)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                stmt_id,
                acc_id,
                run_id,
                payload_id,
                date(2025, 1, 1),
                date(2025, 1, 31),
                150000,
                249500,
                99500,
            ),
        )

        txns = [
            (
                uuid.uuid4(),
                stmt_id,
                date(2025, 1, 5),
                300000,
                "Payroll Direct Deposit",
                "deposit",
            ),
            (
                uuid.uuid4(),
                stmt_id,
                date(2025, 1, 12),
                -150000,
                "Rent Payment",
                "withdrawal",
            ),
            (
                uuid.uuid4(),
                stmt_id,
                date(2025, 1, 15),
                -20000,
                "Grocery Store",
                "withdrawal",
            ),
            (
                uuid.uuid4(),
                stmt_id,
                date(2025, 1, 20),
                -15000,
                "Electric Bill",
                "withdrawal",
            ),
            (
                uuid.uuid4(),
                stmt_id,
                date(2025, 1, 31),
                500,
                "Interest Earned",
                "interest_paid",
            ),
        ]
        for t in txns:
            cur.execute(
                """
                    INSERT INTO transactions (transaction_id, statement_id, post_date, amount_cents, description, transaction_category)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """,
                t,
            )

        print(
            "✅ Database successfully seeded with 1 checking account and 5 transactions!"
        )


if __name__ == "__main__":
    seed()
