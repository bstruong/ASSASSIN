"""Tests for exclusive adapter registry across all statement domains."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from app.adapters.base import StatementAdapter
from app.adapters.credit_card import StandardCreditCardAdapter
from app.adapters.depository import StandardDepositoryAdapter
from app.adapters.registry import (
    AdapterRegistry,
    default_registry,
    get_default_registry,
    match_adapter,
)
from app.adapters.schwab import SchwabAdapter
from app.models.enums import AccountDomain, AccountType
from app.models.exceptions import AdapterRegistryError


def create_test_pdf(path: Path, lines: list[str]) -> Path:
    """Helper to generate a minimal valid single-page PDF with given text lines."""
    c = canvas.Canvas(str(path))
    y = 750
    for line in lines:
        c.drawString(100, y, line)
        y -= 25
    c.save()
    return path


class TestAdapterRegistryRegistration:
    """Tests for registering and unregistering adapters."""

    def test_register_and_list_adapters(self) -> None:
        registry = AdapterRegistry()
        dep = StandardDepositoryAdapter()
        card = StandardCreditCardAdapter()

        registry.register(dep)
        registry.register(card)

        adapters = registry.list_adapters()
        assert len(adapters) == 2
        assert adapters[0].adapter_id == "standard_depository"
        assert adapters[1].adapter_id == "standard_credit_card"

    def test_duplicate_registration_fails_loudly(self) -> None:
        registry = AdapterRegistry()
        dep = StandardDepositoryAdapter()
        registry.register(dep)

        with pytest.raises(AdapterRegistryError, match="already registered"):
            registry.register(dep)

    def test_register_invalid_type_fails(self) -> None:
        registry = AdapterRegistry()
        with pytest.raises(TypeError, match="must be an instance of StatementAdapter"):
            registry.register("not an adapter")  # type: ignore[arg-type]

    def test_get_adapter_by_id_success(self) -> None:
        registry = AdapterRegistry()
        dep = StandardDepositoryAdapter()
        registry.register(dep)

        retrieved = registry.get("standard_depository")
        assert retrieved is dep

    def test_get_adapter_by_id_not_found_fails(self) -> None:
        registry = AdapterRegistry()
        with pytest.raises(AdapterRegistryError, match="Adapter not found"):
            registry.get("nonexistent")

    def test_unregister_adapter_success(self) -> None:
        registry = AdapterRegistry()
        dep = StandardDepositoryAdapter()
        registry.register(dep)
        assert len(registry.list_adapters()) == 1

        registry.unregister("standard_depository")
        assert len(registry.list_adapters()) == 0

    def test_unregister_not_found_fails(self) -> None:
        registry = AdapterRegistry()
        with pytest.raises(AdapterRegistryError, match="Adapter not found"):
            registry.unregister("nonexistent")

    def test_clear_adapters(self) -> None:
        registry = AdapterRegistry()
        registry.register(StandardDepositoryAdapter())
        registry.clear()
        assert len(registry.list_adapters()) == 0


class TestAdapterRegistryMatching:
    """Tests for exclusive matching across domains."""

    def test_match_depository_adapter(self, tmp_path: Path) -> None:
        pdf_path = create_test_pdf(
            tmp_path / "depository.pdf",
            ["STANDARD BANK", "CHECKING ACCOUNT STATEMENT", "Balance: $1,200.00"],
        )
        registry = get_default_registry()
        matched = registry.match(pdf_path)

        assert isinstance(matched, StandardDepositoryAdapter)
        assert matched.adapter_id == "standard_depository"

    def test_match_credit_card_adapter(self, tmp_path: Path) -> None:
        pdf_path = create_test_pdf(
            tmp_path / "card.pdf",
            ["STANDARD CARD", "CREDIT CARD STATEMENT", "New Balance: $450.00"],
        )
        registry = get_default_registry()
        matched = registry.match(pdf_path)

        assert isinstance(matched, StandardCreditCardAdapter)
        assert matched.adapter_id == "standard_credit_card"

    def test_match_schwab_adapter(self, tmp_path: Path) -> None:
        pdf_path = create_test_pdf(
            tmp_path / "schwab.pdf",
            ["CHARLES SCHWAB & CO., INC.", "Statement of Account"],
        )
        registry = get_default_registry()
        matched = registry.match(pdf_path)

        assert isinstance(matched, SchwabAdapter)
        assert matched.adapter_id == "schwab_brokerage"

    def test_zero_matches_fails_loudly(self, tmp_path: Path) -> None:
        pdf_path = create_test_pdf(
            tmp_path / "unknown_bank.pdf",
            ["MYSTERIOUS ACME BANK", "ACCOUNT STATEMENT"],
        )
        registry = get_default_registry()
        with pytest.raises(AdapterRegistryError, match="No registered adapter matched"):
            registry.match(pdf_path)

    def test_overlapping_matches_fails_loudly(self, tmp_path: Path) -> None:
        # Document containing markers for both Standard Bank and Charles Schwab
        pdf_path = create_test_pdf(
            tmp_path / "overlapping.pdf",
            [
                "STANDARD BANK CHECKING ACCOUNT",
                "CHARLES SCHWAB BROKERAGE LINK",
            ],
        )
        registry = get_default_registry()
        with pytest.raises(AdapterRegistryError, match="Ambiguous adapter match"):
            registry.match(pdf_path)

    def test_custom_overlapping_adapter_fails_loudly(self, tmp_path: Path) -> None:
        # Register a second adapter that also matches Standard Bank
        class CompetingDepositoryAdapter(StandardDepositoryAdapter):
            adapter_id = "competing_depository"
            adapter_version = "1.0.0"

        pdf_path = create_test_pdf(
            tmp_path / "dep.pdf",
            ["STANDARD BANK CHECKING ACCOUNT"],
        )
        registry = AdapterRegistry()
        registry.register(StandardDepositoryAdapter())
        registry.register(CompetingDepositoryAdapter())

        with pytest.raises(
            AdapterRegistryError,
            match="Ambiguous adapter match: multiple adapters matched",
        ):
            registry.match(pdf_path)

    def test_missing_file_raises_filenotfound(self, tmp_path: Path) -> None:
        registry = get_default_registry()
        with pytest.raises(FileNotFoundError):
            registry.match(tmp_path / "nonexistent.pdf")

    def test_adapter_exception_during_matches_propagates(self, tmp_path: Path) -> None:
        class FailingAdapter(StatementAdapter):
            adapter_id = "failing_adapter"
            adapter_version = "1.0.0"
            institution_id = "Fail Bank"
            account_domain = AccountDomain.DEPOSITORY
            account_types = frozenset({AccountType.CHECKING})

            def matches(self, file_path: Path) -> bool:
                raise RuntimeError("Unexpected failure inside matches()")

        pdf_path = create_test_pdf(tmp_path / "test.pdf", ["SOME TEXT"])
        registry = AdapterRegistry()
        registry.register(FailingAdapter())

        with pytest.raises(RuntimeError, match="Unexpected failure inside matches()"):
            registry.match(pdf_path)

    def test_convenience_match_adapter(self, tmp_path: Path) -> None:
        pdf_path = create_test_pdf(
            tmp_path / "card.pdf",
            ["STANDARD CARD", "CREDIT CARD STATEMENT"],
        )
        matched = match_adapter(pdf_path)
        assert isinstance(matched, StandardCreditCardAdapter)


class TestRegistryCodeInvariants:
    """Ensure registry adheres to ASSASSIN core invariants."""

    def test_no_dynamic_metaprogramming_in_registry(self) -> None:
        registry_file = Path(__file__).parents[1] / "app" / "adapters" / "registry.py"
        source = registry_file.read_text(encoding="utf-8")
        tree = ast.parse(source)

        forbidden_calls = {"eval", "exec", "__import__", "getattr", "setattr"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_calls, (
                    f"Forbidden dynamic call: {node.func.id}"
                )

    def test_default_registry_has_expected_domains(self) -> None:
        adapters = default_registry.list_adapters()
        domains = {a.account_domain for a in adapters}
        assert AccountDomain.DEPOSITORY in domains
        assert AccountDomain.REVOLVING_CREDIT in domains
        assert AccountDomain.CUSTODIAL_BROKERAGE in domains
