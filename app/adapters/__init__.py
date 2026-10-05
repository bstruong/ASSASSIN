"""Statement adapters for multi-domain financial document extraction."""

from app.adapters.base import (
    CreditCardStatementAdapter,
    DepositoryStatementAdapter,
    InvestmentStatementAdapter,
    StatementAdapter,
)
from app.adapters.credit_card import StandardCreditCardAdapter
from app.adapters.depository import StandardDepositoryAdapter
from app.adapters.registry import (
    AdapterRegistry,
    default_registry,
    get_default_registry,
    match_adapter,
)
from app.adapters.schwab import SchwabAdapter

__all__ = [
    "AdapterRegistry",
    "CreditCardStatementAdapter",
    "DepositoryStatementAdapter",
    "InvestmentStatementAdapter",
    "SchwabAdapter",
    "StandardCreditCardAdapter",
    "StandardDepositoryAdapter",
    "StatementAdapter",
    "default_registry",
    "get_default_registry",
    "match_adapter",
]
