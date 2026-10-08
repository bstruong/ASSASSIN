#!/usr/bin/env python3
"""QA: dashboard frontier verify button posts aggregates only.

Run with: uv run python scripts/qa_frontier_dashboard.py

The button path is also covered by Manual UI QA on the dashboard page.
This script checks the HTML contract without a browser.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.api.app import create_app


class QAReport:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[str] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed.append(name)
            print(f"  [PASS] {name}" + (f": {detail}" if detail else ""))
        else:
            self.failed.append(name)
            print(f"  [FAIL] {name}" + (f": {detail}" if detail else ""))

    def summary(self) -> bool:
        total = len(self.passed) + len(self.failed)
        print(f"\n{'=' * 60}")
        print(f"QA Summary: {len(self.passed)}/{total} passed")
        if self.failed:
            for name in self.failed:
                print(f"  - {name}")
            return False
        print("All tests passed!")
        return True


def main() -> int:
    report = QAReport()
    client = TestClient(create_app())

    print("\n" + "=" * 60)
    print("Feature: Frontier verify control on the dashboard")
    print("=" * 60)
    page = client.get("/dashboard")
    report.record(
        "Dashboard shows the frontier verify button",
        page.status_code == 200 and 'id="frontier-verify-btn"' in page.text,
    )

    print("\n" + "=" * 60)
    print("Feature: Aggregate happy path")
    print("=" * 60)
    resp = client.post(
        "/api/v1/dashboard/frontier-audit",
        data={"group_by": "transaction_category"},
        headers={"HX-Request": "true"},
    )
    report.record(
        "Frontier audit returns an HTML fragment",
        resp.status_code == 200 and "Frontier aggregate audit" in resp.text,
        f"status={resp.status_code}",
    )
    report.record(
        "Fragment shows integer-cent deposit total",
        "total_deposits_cents" not in resp.text and "Deposits" in resp.text,
    )
    report.record(
        "Fragment omits row-level fields",
        "account_mask" not in resp.text and "page_text" not in resp.text,
    )

    print("\n" + "=" * 60)
    print("Feature: Loud rejection")
    print("=" * 60)
    bad = client.post(
        "/api/v1/dashboard/frontier-audit",
        data={"group_by": "description"},
        headers={"HX-Request": "true"},
    )
    report.record(
        "group_by=description is rejected",
        bad.status_code == 400 and "Invalid group_by" in bad.text,
        f"status={bad.status_code}",
    )
    blank = client.post(
        "/api/v1/dashboard/frontier-audit",
        data={"group_by": " "},
        headers={"HX-Request": "true"},
    )
    report.record(
        "Blank group_by is rejected",
        blank.status_code == 400,
    )

    print("\n" + "=" * 60)
    return 0 if report.summary() else 1


if __name__ == "__main__":
    sys.exit(main())
