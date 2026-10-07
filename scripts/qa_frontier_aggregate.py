#!/usr/bin/env python
"""QA Verification Script: Frontier aggregate-only handoff + PII middleware.

Verifies Phase 2 frontier reintroduction without Tier-1 row egress:
  1. Production create_app mounts /chat/frontier/audit and PII middleware.
  2. Happy path: group_by / aggregate SQL return integer-cent aggregates only.
  3. Negative: row-level SQL, SELECT *, description/account_mask columns fail loud.
  4. Tier-1 /v1/orchestrator/execute_sql remains HTTP 501 quarantined.
  5. Frontier responses scrub description / account_mask / page_text keys.

Run with: uv run python scripts/qa_frontier_aggregate.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.api.app import create_app
from app.middleware.sanitization import PiiSanitizationMiddleware


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
            print(f"Failed tests ({len(self.failed)}):")
            for name in self.failed:
                print(f"  - {name}")
            return False
        print("All tests passed!")
        return True


def main() -> int:
    report = QAReport()
    app = create_app()
    client = TestClient(app)

    print("\n" + "=" * 60)
    print("Feature: Production mounts (frontier + PII middleware)")
    print("=" * 60)

    paths = set(app.openapi()["paths"])
    report.record(
        "Production mounts /chat/frontier/audit",
        "/chat/frontier/audit" in paths,
    )
    report.record(
        "Production mounts /v1/orchestrator/execute_sql (quarantine)",
        "/v1/orchestrator/execute_sql" in paths,
    )
    report.record(
        "PiiSanitizationMiddleware mounted on create_app",
        any(
            getattr(m, "cls", None) is PiiSanitizationMiddleware
            for m in app.user_middleware
        ),
    )

    print("\n" + "=" * 60)
    print("Feature: Aggregate-only happy path")
    print("=" * 60)

    resp = client.post(
        "/chat/frontier/audit",
        json={"group_by": "transaction_category", "plan_id": "qa-frontier-1"},
    )
    body = resp.json() if resp.status_code == 200 else {}
    report.record(
        "group_by audit returns 200 completed",
        resp.status_code == 200 and body.get("status") == "completed",
        f"status={resp.status_code}",
    )
    if resp.status_code == 200:
        cents = body.get("summary", {}).get("totals", {}).get("total_deposits_cents")
        report.record(
            "Aggregates use integer cents",
            isinstance(cents, int),
            f"total_deposits_cents={cents!r}",
        )
        report.record(
            "No Tier-1 results field on audit response",
            "results" not in body,
        )

    resp = client.post(
        "/chat/frontier/audit",
        json={
            "aggregate_sql": "SELECT COUNT(*) AS txn_count FROM transactions;",
        },
    )
    body = resp.json() if resp.status_code == 200 else {}
    report.record(
        "aggregate_sql audit returns aggregates list",
        resp.status_code == 200 and isinstance(body.get("aggregates"), list),
        f"status={resp.status_code}",
    )

    print("\n" + "=" * 60)
    print("Feature: Negative / fail-loud boundaries")
    print("=" * 60)

    resp = client.post(
        "/chat/frontier/audit",
        json={"aggregate_sql": "SELECT description FROM transactions;"},
    )
    detail = resp.json().get("detail", {})
    report.record(
        "Row-level description SQL rejected",
        resp.status_code == 400
        and detail.get("error_code") == "CLOUD_TOOL_VALIDATION_ERROR",
        f"status={resp.status_code} code={detail.get('error_code')}",
    )

    resp = client.post(
        "/chat/frontier/audit",
        json={"aggregate_sql": "SELECT * FROM transactions;"},
    )
    detail = resp.json().get("detail", {})
    report.record(
        "SELECT * rejected",
        resp.status_code == 400
        and detail.get("error_code") == "CLOUD_TOOL_VALIDATION_ERROR",
    )

    resp = client.post(
        "/chat/frontier/audit",
        json={"group_by": "description"},
    )
    detail = resp.json().get("detail", {})
    report.record(
        "group_by=description rejected",
        resp.status_code == 400
        and detail.get("error_code") == "CLOUD_TOOL_VALIDATION_ERROR",
    )

    resp = client.post("/chat/frontier/audit", json={})
    report.record(
        "Empty audit payload returns 422",
        resp.status_code == 422,
    )

    print("\n" + "=" * 60)
    print("Feature: Tier-1 quarantine retained")
    print("=" * 60)

    resp = client.post(
        "/v1/orchestrator/execute_sql",
        json={"sql": "SELECT 1 AS val;"},
    )
    detail = resp.json().get("detail", {})
    report.record(
        "execute_sql remains HTTP 501 FRONTIER_QUARANTINED",
        resp.status_code == 501
        and detail.get("error_code") == "FRONTIER_QUARANTINED"
        and "results" not in detail,
        f"status={resp.status_code} code={detail.get('error_code')}",
    )

    print("\n" + "=" * 60)
    print("Feature: Frontier row-PII key scrubbing")
    print("=" * 60)

    @app.get("/chat/frontier/leak-probe")
    async def leak_probe():
        return {
            "description": "SECRET PAYEE",
            "account_mask": "*9999",
            "page_text": "ocr dump",
            "total_deposits_cents": 42,
        }

    probe_client = TestClient(app)
    resp = probe_client.get("/chat/frontier/leak-probe")
    body = resp.json()
    report.record(
        "description scrubbed on frontier path",
        body.get("description") == "[REDACTED]",
    )
    report.record(
        "account_mask scrubbed on frontier path",
        body.get("account_mask") == "[REDACTED]",
    )
    report.record(
        "page_text scrubbed on frontier path",
        body.get("page_text") == "[REDACTED]",
    )
    report.record(
        "integer cents preserved on frontier path",
        body.get("total_deposits_cents") == 42,
    )

    print("\n" + "=" * 60)
    all_passed = report.summary()
    print("=" * 60)
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
