"""Statement adapters for multi-domain financial document extraction."""

from app.adapters.base import (
    CreditCardStatementAdapter,
    DepositoryStatementAdapter,
    InvestmentStatementAdapter,
    StatementAdapter,
)
from app.adapters.credit_card import StandardCreditCardAdapter
from app.adapters.depository import StandardDepositoryAdapter
from app.adapters.schwab import SchwabAdapter

__all__ = [
    "CreditCardStatementAdapter",
    "DepositoryStatementAdapter",
    "InvestmentStatementAdapter",
    "SchwabAdapter",
    "StandardCreditCardAdapter",
    "StandardDepositoryAdapter",
    "StatementAdapter",
]
