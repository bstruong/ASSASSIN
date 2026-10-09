#!/usr/bin/env python3
"""QA: Schwab cash book versus an optional printed portfolio bridge.

Run with: uv run python scripts/qa_schwab_portfolio_bridge.py

Manual UI QA: not applicable (no user-visible UI).
"""

from __future__ import annotations

import sys

from app.adapters.schwab import SchwabAdapter
from app.models.enums import RunStatus
from app.models.exceptions import InvariantError, MissingSectionError
from app.models.raw import ExtractionRun, RawExtraction, RawPage, RawPayload

_CASH_ONLY = """CHARLES SCHWAB
Account Number: *0000
Statement Period: 2025-08-01 to 2025-08-31

ACCOUNT SUMMARY
Starting Cash Balance: $1,000.00
Ending Cash Balance: $1,100.00

TRANSACTION ACTIVITY
Date    Description    Amount
2025-08-10    SYNTHETIC Dividend    +$100.00
END OF STATEMENT
"""

_BRIDGE = """CHARLES SCHWAB
Account Number: *0000
Statement Period: 2025-08-01 to 2025-08-31

ACCOUNT SUMMARY
Starting Cash Balance: $1,000.00
Ending Cash Balance: $1,100.00

PORTFOLIO SUMMARY
Starting Portfolio Value: $5,000.00
Transfers In: $100.00
Transfers Out: $0.00
Income and Dividends: $100.00
Realized Gain/Loss: -$50.00
Unrealized Gain/Loss: -$25.00
Ending Portfolio Value: $5,125.00

TRANSACTION ACTIVITY
Date    Description    Amount
2025-08-10    SYNTHETIC Dividend    +$100.00
END OF STATEMENT
"""


class QAReport:
    """Collect [PASS]/[FAIL] lines for the operator."""

    def __init__(self) -> None:
        self.failed = 0

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        mark = "PASS" if ok else "FAIL"
        suffix = f": {detail}" if detail else ""
        print(f"[{mark}] {name}{suffix}")
        if not ok:
            self.failed += 1


def _extraction(text: str) -> RawExtraction:
    payload = RawPayload(
        content_sha256="d" * 64,
        byte_length=len(text.encode("utf-8")),
        original_basename="synthetic_schwab.pdf",
    )
    run = ExtractionRun(
        raw_payload_id=payload.raw_payload_id,
        adapter_id="schwab_brokerage",
        adapter_version="1.0.0",
        status=RunStatus.EXTRACTED,
    )
    page = RawPage(run_id=run.run_id, page_number=1, page_text=text, tokens=[])
    return RawExtraction(payload=payload, run=run, pages=[page])


def _cash_identity(statement, txns) -> bool:
    signed = sum(txn.amount_cents for txn in txns)
    return statement.opening_balance_cents + signed == statement.closing_balance_cents


def main() -> int:
    print("Feature: Schwab cash-primary statements and an optional portfolio bridge.")
    print("Cash identity uses signed cash deltas. The portfolio equation is separate.")
    print(
        "Synthetic descriptions only. Manual UI QA: not applicable (no user-visible UI)."
    )
    print()
    report = QAReport()
    adapter = SchwabAdapter()

    _account, statement, summary, txns, _holdings = adapter.parse_canonical(
        _extraction(_CASH_ONLY)
    )
    portfolio_unset = all(
        value is None
        for value in (
            summary.opening_portfolio_cents,
            summary.closing_portfolio_cents,
            summary.transfers_in_cents,
            summary.transfers_out_cents,
            summary.income_dividends_cents,
            summary.realized_gains_cents,
            summary.unrealized_gains_cents,
        )
    )
    report.record(
        "cash-only happy path",
        portfolio_unset and _cash_identity(statement, txns) and len(txns) == 1,
        "portfolio cents stayed unset and cash identity held",
    )

    _account, statement, summary, txns, _holdings = adapter.parse_canonical(
        _extraction(_BRIDGE)
    )
    opening = summary.opening_portfolio_cents
    closing = summary.closing_portfolio_cents
    transfers_in = summary.transfers_in_cents
    transfers_out = summary.transfers_out_cents
    income = summary.income_dividends_cents
    realized = summary.realized_gains_cents
    unrealized = summary.unrealized_gains_cents
    bridge_ok = False
    if None not in (
        opening,
        closing,
        transfers_in,
        transfers_out,
        income,
        realized,
        unrealized,
    ):
        computed = (
            opening + transfers_in - transfers_out + income + realized + unrealized
        )
        cash_delta = sum(txn.amount_cents for txn in txns)
        bridge_ok = (
            computed == closing
            and realized < 0
            and unrealized < 0
            and transfers_in >= 0
            and transfers_out >= 0
            and _cash_identity(statement, txns)
            and closing != statement.closing_balance_cents
            and cash_delta != closing - opening
        )
    report.record(
        "full bridge happy path",
        bridge_ok,
        "signed gains reconciled and cash equation was not applied to portfolio value",
    )

    missing = _BRIDGE.replace("Unrealized Gain/Loss: -$25.00\n", "")
    try:
        adapter.parse_canonical(_extraction(missing))
    except MissingSectionError:
        report.record(
            "missing bridge term",
            True,
            "MissingSectionError",
        )
    except Exception as exc:  # noqa: BLE001 — QA must show the unexpected type
        report.record("missing bridge term", False, type(exc).__name__)
    else:
        report.record("missing bridge term", False, "accepted a missing printed term")

    mismatch = _BRIDGE.replace(
        "Ending Portfolio Value: $5,125.00",
        "Ending Portfolio Value: $5,125.01",
    )
    try:
        adapter.parse_canonical(_extraction(mismatch))
    except InvariantError as exc:
        report.record(
            "1-cent bridge mismatch",
            "Portfolio bridge mismatch" in str(exc),
            "InvariantError",
        )
    except Exception as exc:  # noqa: BLE001 — QA must show the unexpected type
        report.record("1-cent bridge mismatch", False, type(exc).__name__)
    else:
        report.record("1-cent bridge mismatch", False, "accepted a 1-cent miss")

    negative_in = _BRIDGE.replace("Transfers In: $100.00", "Transfers In: ($100.00)")
    try:
        adapter.parse_summary(_extraction(negative_in))
    except InvariantError as exc:
        report.record(
            "printed negative transfer in",
            str(exc) == "transfers_in_cents must be >= 0",
            "InvariantError",
        )
    except Exception as exc:  # noqa: BLE001 — QA must show the unexpected type
        report.record("printed negative transfer in", False, type(exc).__name__)
    else:
        report.record(
            "printed negative transfer in",
            False,
            "accepted a printed negative transfer",
        )

    print()
    if report.failed:
        print(f"[FAIL] {report.failed} check(s) failed")
        return 1
    print("[PASS] Schwab portfolio bridge checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
