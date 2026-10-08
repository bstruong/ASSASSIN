#!/usr/bin/env python3
"""QA: StatementAdapter extract forwarding and loud legacy-parse rejection.

The nightly mutation gate failed because these base methods were never executed.
This script checks the happy path and the loud failure without reading diffs.

Run with: uv run python scripts/qa_statement_adapter_contract.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from reportlab.pdfgen import canvas

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.adapters.base import StatementAdapter
from app.models.enums import AccountDomain, AccountType


class _ContractAdapter(StatementAdapter):
    adapter_id = "qa_contract"
    adapter_version = "9.9.9"
    institution_id = "QA Bank"
    account_domain = AccountDomain.DEPOSITORY
    account_types = frozenset({AccountType.CHECKING})

    def matches(self, file_path: Path) -> bool:
        return False


def main() -> int:
    print("QA: StatementAdapter base contract (extract + legacy parse)")
    print("Verifies raw extraction identity forwarding and loud parse rejection.")
    passed = 0
    failed = 0

    def record(name: str, ok: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if ok:
            passed += 1
            print(f"  [PASS] {name}" + (f": {detail}" if detail else ""))
        else:
            failed += 1
            print(f"  [FAIL] {name}" + (f": {detail}" if detail else ""))

    with tempfile.TemporaryDirectory(prefix="assassin-qa-adapter-") as tmp:
        pdf_path = Path(tmp) / "synthetic.pdf"
        pdf = canvas.Canvas(str(pdf_path))
        pdf.drawString(72, 720, "SYNTHETIC statement")
        pdf.save()

        try:
            extraction = _ContractAdapter().extract(pdf_path)
            record(
                "extract forwards adapter identity on a synthetic PDF",
                extraction.run.adapter_id == "qa_contract"
                and extraction.run.adapter_version == "9.9.9"
                and extraction.payload.original_basename == "synthetic.pdf"
                and len(extraction.pages) == 1,
                f"adapter_id={extraction.run.adapter_id} pages={len(extraction.pages)}",
            )
        except (OSError, ValueError, TypeError) as exc:
            record(
                "extract forwards adapter identity on a synthetic PDF",
                False,
                f"{type(exc).__name__}: {exc}",
            )

        try:
            _ContractAdapter().extract(Path(tmp) / "missing.pdf")
            record("extract rejects a missing PDF loudly", False, "no exception")
        except FileNotFoundError as exc:
            record(
                "extract rejects a missing PDF loudly",
                "PDF file not found" in str(exc),
                type(exc).__name__,
            )
        except (OSError, ValueError, TypeError) as exc:
            record(
                "extract rejects a missing PDF loudly",
                False,
                f"{type(exc).__name__}: {exc}",
            )

        try:
            _ContractAdapter().parse(pdf_path)
            record("legacy parse fails loudly", False, "no exception")
        except NotImplementedError as exc:
            message = str(exc)
            record(
                "legacy parse fails loudly",
                message == "Use parse_canonical or domain parse methods.",
                message,
            )
        except (TypeError, ValueError) as exc:
            record("legacy parse fails loudly", False, f"{type(exc).__name__}: {exc}")

    total = passed + failed
    print(f"\nQA Summary: {passed}/{total} passed")
    if failed:
        print(f"[FAIL] {failed} check(s) failed")
        return 1
    print("[PASS] StatementAdapter base contract verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
