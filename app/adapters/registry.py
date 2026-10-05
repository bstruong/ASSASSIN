"""Exclusive StatementAdapter registry across financial domains.

Enforces strict exclusivity across adapters during file matching:
- Exactly 1 registered adapter must claim a document.
- Zero matches fail loudly with AdapterRegistryError.
- Overlapping matches (2+ adapters returning True) fail loudly with AdapterRegistryError.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from app.adapters.base import StatementAdapter
from app.adapters.combined import StandardCombinedDepositoryAdapter
from app.adapters.credit_card import StandardCreditCardAdapter
from app.adapters.depository import StandardDepositoryAdapter
from app.adapters.schwab import SchwabAdapter
from app.models.exceptions import AdapterRegistryError

logger = logging.getLogger(__name__)


class AdapterRegistry:
    """Registry managing statement adapters and enforcing exclusive document matching."""

    def __init__(self, adapters: Sequence[StatementAdapter] | None = None) -> None:
        self._adapters: dict[str, StatementAdapter] = {}
        if adapters:
            for adapter in adapters:
                self.register(adapter)

    def register(self, adapter: StatementAdapter) -> None:
        """Register an adapter instance. Fails loudly on duplicates or invalid types."""
        if not isinstance(adapter, StatementAdapter):
            raise TypeError(
                f"Adapter must be an instance of StatementAdapter, got {type(adapter).__name__}"
            )
        if adapter.adapter_id in self._adapters:
            logger.error(
                "Attempted duplicate adapter registration",
                extra={"adapter_id": adapter.adapter_id},
            )
            raise AdapterRegistryError(
                f"Adapter '{adapter.adapter_id}' is already registered."
            )

        self._adapters[adapter.adapter_id] = adapter
        logger.info(
            "Registered statement adapter",
            extra={
                "adapter_id": adapter.adapter_id,
                "version": adapter.adapter_version,
                "institution": adapter.institution_id,
                "domain": adapter.account_domain.value,
            },
        )

    def unregister(self, adapter_id: str) -> None:
        """Unregister an adapter by its ID. Fails loudly if not found."""
        if adapter_id not in self._adapters:
            logger.error(
                "Attempted to unregister unknown adapter",
                extra={"adapter_id": adapter_id},
            )
            raise AdapterRegistryError(f"Adapter not found in registry: '{adapter_id}'")
        del self._adapters[adapter_id]
        logger.info("Unregistered statement adapter", extra={"adapter_id": adapter_id})

    def get(self, adapter_id: str) -> StatementAdapter:
        """Retrieve an adapter by ID. Fails loudly if not found."""
        if adapter_id not in self._adapters:
            raise AdapterRegistryError(f"Adapter not found in registry: '{adapter_id}'")
        return self._adapters[adapter_id]

    def list_adapters(self) -> list[StatementAdapter]:
        """Return a list of all currently registered adapters in registration order."""
        return list(self._adapters.values())

    def clear(self) -> None:
        """Clear all registered adapters."""
        self._adapters.clear()
        logger.debug("Cleared adapter registry")

    def match(self, file_path: Path | str) -> StatementAdapter:
        """Find the exclusive adapter that matches the given file.

        Enforces that exactly one adapter claims the file. Zero or multiple matches
        fail loudly with AdapterRegistryError.

        Args:
            file_path: Path to the statement PDF file.

        Returns:
            The single matching StatementAdapter instance.

        Raises:
            FileNotFoundError: If the statement file does not exist.
            AdapterRegistryError: If zero or multiple adapters claim the file.
        """
        resolved_path = Path(file_path).resolve()
        if not resolved_path.is_file():
            logger.error(
                "Statement file not found during adapter matching",
                extra={"file_path": str(resolved_path)},
            )
            raise FileNotFoundError(f"Statement file not found: {resolved_path}")

        logger.info(
            "Evaluating document against registered adapters",
            extra={
                "file_path": str(resolved_path),
                "adapter_count": len(self._adapters),
            },
        )

        matched: list[StatementAdapter] = []
        for adapter in self._adapters.values():
            try:
                if adapter.matches(resolved_path):
                    matched.append(adapter)
            except Exception as exc:
                logger.error(
                    "Error executing adapter matches() heuristic",
                    extra={
                        "adapter_id": adapter.adapter_id,
                        "file_path": str(resolved_path),
                        "error": str(exc),
                    },
                )
                raise

        if len(matched) == 0:
            available_ids = list(self._adapters.keys())
            logger.error(
                "No registered adapter matched document",
                extra={
                    "file_path": str(resolved_path),
                    "registered_adapters": available_ids,
                },
            )
            raise AdapterRegistryError(
                f"No registered adapter matched document: {resolved_path}. "
                f"Available adapters: {available_ids}"
            )

        if len(matched) > 1:
            matching_ids = [a.adapter_id for a in matched]
            logger.error(
                "Ambiguous adapter match: multiple adapters claimed document",
                extra={
                    "file_path": str(resolved_path),
                    "matching_adapters": matching_ids,
                },
            )
            raise AdapterRegistryError(
                f"Ambiguous adapter match: multiple adapters matched document '{resolved_path}': "
                f"{matching_ids}. Adapter matches must be strictly mutually exclusive."
            )

        selected = matched[0]
        logger.info(
            "Exclusive adapter match resolved successfully",
            extra={
                "file_path": str(resolved_path),
                "adapter_id": selected.adapter_id,
                "domain": selected.account_domain.value,
            },
        )
        return selected


def get_default_registry() -> AdapterRegistry:
    """Create a new AdapterRegistry pre-populated with standard domain adapters."""
    return AdapterRegistry(
        adapters=[
            StandardDepositoryAdapter(),
            StandardCreditCardAdapter(),
            SchwabAdapter(),
            StandardCombinedDepositoryAdapter(),
        ]
    )


# Module-level default registry instance
default_registry: AdapterRegistry = get_default_registry()


def match_adapter(
    file_path: Path | str, registry: AdapterRegistry | None = None
) -> StatementAdapter:
    """Convenience helper to match a file using the provided or default registry."""
    active_registry = registry or default_registry
    return active_registry.match(file_path)
