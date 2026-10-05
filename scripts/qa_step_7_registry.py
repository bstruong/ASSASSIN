#!/usr/bin/env python3
"""QA verification script for Step 7: Exclusive Statement Adapter Registry.

Validates:
1. Dynamic multi-domain registry registration and discovery.
2. Exclusive document match for Standard Depository adapter.
3. Exclusive document match for Standard Credit Card adapter.
4. Exclusive document match for Schwab Investment adapter.
5. Fail-loud rejection (AdapterRegistryError) when 0 adapters match the document.
6. Fail-loud rejection (AdapterRegistryError) when multiple adapters match (ambiguous overlap).
7. Fail-loud rejection (AdapterRegistryError) on duplicate adapter registration.
8. Fail-loud rejection (FileNotFoundError) on nonexistent input documents.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from reportlab.pdfgen import canvas

from app.adapters.credit_card import StandardCreditCardAdapter
from app.adapters.depository import StandardDepositoryAdapter
from app.adapters.registry import (
    AdapterRegistry,
    get_default_registry,
    match_adapter,
)
from app.adapters.schwab import SchwabAdapter
from app.models.exceptions import AdapterRegistryError


def create_sample_pdf(path: Path, header_lines: list[str]) -> Path:
    """Generate a clean synthetic single-page PDF with specified header lines."""
    c = canvas.Canvas(str(path))
    y = 750
    for line in header_lines:
        c.drawString(100, y, line)
        y -= 25
    c.save()
    return path


def run_qa() -> None:
    print("=" * 80)
    print("ASSASSIN Step 7: Exclusive Adapter Registry QA Verification")
    print("=" * 80)

    with tempfile.TemporaryDirectory() as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        registry = get_default_registry()

        # 1. Registry Inspection
        print("\n[Step 1] Inspect Default Registered Adapters")
        registered = registry.list_adapters()
        print(f"Registered adapters count: {len(registered)}")
        for a in registered:
            print(
                f" - {a.adapter_id:<25} | Domain: {a.account_domain.value:<20} | Version: {a.adapter_version}"
            )
        assert len(registered) >= 3
        print("  -> PASS: All standard domain adapters loaded.")

        # 2. Exclusive Match: Depository
        print("\n[Step 2] Match Depository Statement (Standard Depository)")
        dep_pdf = create_sample_pdf(
            tmp_dir / "depository.pdf",
            ["STANDARD BANK", "CHECKING ACCOUNT STATEMENT", "Account #: 123456789"],
        )
        matched_dep = registry.match(dep_pdf)
        assert isinstance(matched_dep, StandardDepositoryAdapter)
        print(f"  -> Matched Adapter: {matched_dep.adapter_id}")
        print("  -> PASS: Standard Depository matched exclusively.")

        # 3. Exclusive Match: Revolving Credit
        print("\n[Step 3] Match Revolving Credit Statement (Standard Credit Card)")
        card_pdf = create_sample_pdf(
            tmp_dir / "credit_card.pdf",
            ["STANDARD CARD", "CREDIT CARD STATEMENT", "Account Ending In: 4321"],
        )
        matched_card = registry.match(card_pdf)
        assert isinstance(matched_card, StandardCreditCardAdapter)
        print(f"  -> Matched Adapter: {matched_card.adapter_id}")
        print("  -> PASS: Standard Credit Card matched exclusively.")

        # 4. Exclusive Match: Investment
        print("\n[Step 4] Match Custodial Brokerage Statement (Schwab Brokerage)")
        schwab_pdf = create_sample_pdf(
            tmp_dir / "schwab.pdf",
            [
                "CHARLES SCHWAB & CO., INC.",
                "Statement of Account",
                "Brokerage Services",
            ],
        )
        matched_schwab = registry.match(schwab_pdf)
        assert isinstance(matched_schwab, SchwabAdapter)
        print(f"  -> Matched Adapter: {matched_schwab.adapter_id}")
        print("  -> PASS: Schwab Brokerage matched exclusively.")

        # 5. Fail-Loud: Zero Matches (Unrecognized Document)
        print("\n[Step 5] Fail-Loud on Zero Matching Adapters")
        unknown_pdf = create_sample_pdf(
            tmp_dir / "unknown.pdf",
            ["ACME MYSTERY BANK", "MONTHLY TRANSACTION RECORD"],
        )
        try:
            registry.match(unknown_pdf)
            raise AssertionError("Expected AdapterRegistryError for zero matches!")
        except AdapterRegistryError as exc:
            print(f"  -> Caught expected AdapterRegistryError: {exc}")
            print("  -> PASS: Zero matches failed loudly.")

        # 6. Fail-Loud: Overlapping Matches (Ambiguous Document)
        print("\n[Step 6] Fail-Loud on Overlapping Matching Adapters")
        overlapping_pdf = create_sample_pdf(
            tmp_dir / "overlapping.pdf",
            [
                "STANDARD BANK CHECKING ACCOUNT STATEMENT",
                "CHARLES SCHWAB BROKERAGE CO-BRANDED PORTFOLIO",
            ],
        )
        try:
            registry.match(overlapping_pdf)
            raise AssertionError(
                "Expected AdapterRegistryError for overlapping matches!"
            )
        except AdapterRegistryError as exc:
            print(f"  -> Caught expected AdapterRegistryError: {exc}")
            print("  -> PASS: Ambiguous match failed loudly.")

        # 7. Fail-Loud: Duplicate Adapter Registration
        print("\n[Step 7] Fail-Loud on Duplicate Adapter Registration")
        duplicate_registry = AdapterRegistry()
        duplicate_registry.register(StandardDepositoryAdapter())
        try:
            duplicate_registry.register(StandardDepositoryAdapter())
            raise AssertionError(
                "Expected AdapterRegistryError for duplicate registration!"
            )
        except AdapterRegistryError as exc:
            print(f"  -> Caught expected AdapterRegistryError: {exc}")
            print("  -> PASS: Duplicate registration failed loudly.")

        # 8. Fail-Loud: Missing File
        print("\n[Step 8] Fail-Loud on Nonexistent File")
        try:
            match_adapter(tmp_dir / "nonexistent.pdf")
            raise AssertionError("Expected FileNotFoundError!")
        except FileNotFoundError as exc:
            print(f"  -> Caught expected FileNotFoundError: {exc}")
            print("  -> PASS: Nonexistent file failed loudly.")

    print("\n" + "=" * 80)
    print(
        "ALL QA CHECKS PASSED: Exclusive Registry is fully compliant with specifications."
    )
    print("=" * 80)


if __name__ == "__main__":
    run_qa()
