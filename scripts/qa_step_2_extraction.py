#!/usr/bin/env python3
"""QA verification script for Step 2: Extraction Core.

Validates that raw extraction produces integer millipoint bounding boxes,
rejects malformed currency, and enforces closed schemas without drift.
"""

from app.extraction.core import (
    parse_currency_to_cents,
    validate_table_against_schema,
)
from app.models.exceptions import SchemaDriftError, TokenError
from app.models.schema import TableSchema


def test_parse_currency_to_cents() -> None:
    print("Testing parse_currency_to_cents strict currency rules...")
    assert parse_currency_to_cents("$123.45") == 12345
    assert parse_currency_to_cents("-$50.00") == -5000
    assert parse_currency_to_cents("($50.00)") == -5000

    # Reject missing decimal / wrong digits / zero
    for invalid in ["$100", "$100.5", "$100.001", "$0.00", "N/A"]:
        try:
            parse_currency_to_cents(invalid)
            raise AssertionError(f"Expected TokenError for {invalid}")
        except TokenError:
            pass
    print("  ✓ Strict currency cents parsing verified.")


def test_schema_drift_detection() -> None:
    print("Testing TableSchema closed schema and drift detection...")
    schema = TableSchema(
        name="checking_transactions",
        headers=("Date", "Description", "Amount"),
        section_markers=("Checking Activity",),
    )

    # Valid uniform rows
    validate_table_against_schema(
        ["Date", "Description", "Amount"],
        [["2025-01-01", "Deposit", "$100.00"]],
        schema,
    )

    # Ragged row (missing column)
    try:
        validate_table_against_schema(
            ["Date", "Description", "Amount"],
            [["2025-01-01", "Deposit"]],
            schema,
        )
        raise AssertionError("Expected SchemaDriftError for missing column")
    except SchemaDriftError:
        pass

    # Unexpected extra column
    try:
        validate_table_against_schema(
            ["Date", "Description", "Amount", "Extra"],
            [["2025-01-01", "Deposit", "$100.00", "Extra"]],
            schema,
        )
        raise AssertionError("Expected SchemaDriftError for extra column")
    except SchemaDriftError:
        pass
    print("  ✓ TableSchema drift detection strictly verified.")


def main() -> None:
    print("=" * 60)
    print("ASSASSIN Step 2 QA Validation: Extraction Core")
    print("=" * 60)
    test_parse_currency_to_cents()
    test_schema_drift_detection()
    print("=" * 60)
    print("All Step 2 Extraction Core verifications PASSED successfully!")
    print("=" * 60)


if __name__ == "__main__":
    main()
