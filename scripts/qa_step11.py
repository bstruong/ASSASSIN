#!/usr/bin/env python
"""QA Verification Script: Step 11 - Frontier quarantine + PII middleware.

This script verifies that Tier-1 frontier SQL egress remains quarantined,
aggregate-only handoff is mounted, and PII sanitization middleware is wired
into production create_app.

Run with: uv run python scripts/qa_step11.py

Features verified:
  1. Orchestrator execute_sql returns 501 (quarantined; no Tier-1 row sets).
  2. Production app mounts frontier audit + orchestrator quarantine routes.
  3. Production create_app mounts PiiSanitizationMiddleware.
  4. PII sanitization middleware redacts SSN, email, phone, full accounts.
  5. Financial amount strings are redacted; integer *_cents preserved.
  6. Schema introspection endpoint returns table metadata without PII.
  7. Quarantine status tracking records FRONTIER_QUARANTINED.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.api.app import create_app
from app.middleware.sanitization import PiiSanitizationMiddleware

# ── Test Harness ─────────────────────────────────────────────────────


class QAReport:
    """Tracks QA test results."""

    def __init__(self):
        self.passed: list[str] = []
        self.failed: list[str] = []

    def record(self, test_name: str, passed: bool, detail: str = ""):
        if passed:
            self.passed.append(test_name)
            print(f"  [PASS] {test_name}{f': {detail}' if detail else ''}")
        else:
            self.failed.append(test_name)
            print(f"  [FAIL] {test_name}{f': {detail}' if detail else ''}")

    def summary(self):
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


report = QAReport()

# ── App Setup ────────────────────────────────────────────────────────

client = TestClient(create_app())

# ── Verification: Feature 1 — Frontier Tier-1 Quarantine ─────────────

print("\n" + "=" * 60)
print("Feature 1: Frontier Tier-1 SQL Egress Quarantine")
print("=" * 60)

resp = client.post(
    "/v1/orchestrator/execute_sql",
    json={"sql": "SELECT 1 AS val;"},
)
detail = resp.json().get("detail", {})
report.record(
    "execute_sql returns HTTP 501 quarantine",
    resp.status_code == 501 and detail.get("error_code") == "FRONTIER_QUARANTINED",
    f"status={resp.status_code} code={detail.get('error_code')}",
)
report.record(
    "Quarantine response has no Tier-1 results",
    "results" not in detail and "results" not in resp.json(),
)

resp = client.post(
    "/v1/orchestrator/execute_sql",
    json={"sql": "SELECT 1;", "plan_id": "frontier-plan-abc"},
)
detail = resp.json().get("detail", {})
report.record(
    "Plan ID preserved on quarantine",
    detail.get("plan_id") == "frontier-plan-abc",
    f"plan_id={detail.get('plan_id')}",
)

# ── Verification: Feature 2 — Production mounts frontier + quarantine ─

print("\n" + "=" * 60)
print("Feature 2: Production App Mounts Frontier Aggregate Handoff")
print("=" * 60)

prod_app = create_app()
prod_paths = set(prod_app.openapi()["paths"])
report.record(
    "Production create_app mounts /chat/frontier/audit",
    "/chat/frontier/audit" in prod_paths,
)
report.record(
    "Production create_app mounts /v1/orchestrator quarantine",
    "/v1/orchestrator/execute_sql" in prod_paths,
)
report.record(
    "Production create_app mounts PiiSanitizationMiddleware",
    any(
        getattr(m, "cls", None) is PiiSanitizationMiddleware
        for m in prod_app.user_middleware
    ),
)

# ── Verification: Feature 3 — PII Sanitization ───────────────────────

print("\n" + "=" * 60)
print("Feature 3: PII Sanitization Middleware")
print("=" * 60)

pii_app = FastAPI()


@pii_app.post("/echo")
async def echo_endpoint(request):
    body = await request.json()
    return {"received": body}


@pii_app.get("/response")
async def response_endpoint():
    return {
        "ssn": "123-45-6789",
        "email": "user@example.com",
        "amount_detail": "amount: 1234.56",
        "account": "1234567890123456",
        "total_deposits_cents": 9999,
    }


pii_app.add_middleware(PiiSanitizationMiddleware)
pii_client = TestClient(pii_app)

# Test request body PII redaction
pii_payload = {
    "ssn": "123-45-6789",
    "email": "user@example.com",
    "phone": "(555) 123-4567",
    "account": "1234567890123456",
}
resp = pii_client.post("/echo", json=pii_payload)
result = resp.json()
received = result.get("received", {})
report.record(
    "SSN redacted from request",
    "123-45-6789" not in str(received),
    f"ssn field contains: {received.get('ssn', 'N/A')}",
)
report.record(
    "Email redacted from request",
    "user@example.com" not in str(received),
    f"email field contains: {received.get('email', 'N/A')}",
)
report.record(
    "Full account redacted from request",
    "1234567890123456" not in str(received),
    f"account field contains: {received.get('account', 'N/A')}",
)

# Test response body PII redaction
resp = pii_client.get("/response")
result = resp.json()
report.record(
    "SSN redacted from response",
    "123-45-6789" not in str(result),
)
report.record(
    "Email redacted from response",
    "user@example.com" not in str(result),
)
report.record(
    "Financial amount string redacted from response",
    "1234.56" not in str(result),
)
report.record(
    "Integer cents preserved through middleware",
    result.get("total_deposits_cents") == 9999,
)

# ── Verification: Feature 4 — Schema Introspection ───────────────────

print("\n" + "=" * 60)
print("Feature 4: Schema Introspection for Frontier Planning")
print("=" * 60)

resp = client.get("/v1/orchestrator/schemas")
# Schema introspection remains available when DB is up; 500 when unavailable.
report.record(
    "Schema endpoint returns 200 or 500 (no Tier-1 rows)",
    resp.status_code in (200, 500),
    f"status={resp.status_code}",
)

result = resp.json() if resp.status_code == 200 else {}
table_names = [t.get("table") for t in result.get("tables", [])]
if resp.status_code == 200:
    report.record(
        "Core tables present in schema",
        "accounts" in table_names and "transactions" in table_names,
        f"tables={table_names}",
    )
else:
    report.record(
        "Schema unavailable without leaking row data",
        "results" not in str(resp.json()),
        f"detail={resp.json()}",
    )

# Verify no PII in schema response
json_str = json.dumps(result if result else resp.json())
report.record(
    "No PII in schema response",
    "123-45-6789" not in json_str and "@example.com" not in json_str,
)

# Verify columns returned when DB is available
for table in result.get("tables", []):
    if table.get("table") == "accounts":
        col_names = [c["name"] for c in table.get("columns", [])]
        report.record(
            "Accounts table has columns",
            "account_id" in col_names and "institution" in col_names,
            f"columns={col_names[:5]}...",
        )
        break

# ── Verification: Feature 5 — Quarantine Status Tracking ─────────────

print("\n" + "=" * 60)
print("Feature 5: Quarantine Status Tracking")
print("=" * 60)

resp = client.post("/v1/orchestrator/execute_sql", json={"sql": "SELECT 1;"})
detail = resp.json().get("detail", {})
execution_id = detail.get("execution_id")

report.record(
    "Execution ID returned on quarantine",
    execution_id is not None and len(execution_id) > 0,
)

if execution_id:
    resp = client.get(f"/v1/orchestrator/status/{execution_id}")
    status_result = resp.json()
    report.record(
        "Status endpoint returns FRONTIER_QUARANTINED",
        resp.status_code == 200
        and status_result.get("status") == "failed"
        and status_result.get("error_code") == "FRONTIER_QUARANTINED",
        f"status={status_result.get('status')} code={status_result.get('error_code')}",
    )

    resp = client.get("/v1/orchestrator/status/nonexistent-id")
    report.record(
        "Unknown execution returns 404",
        resp.status_code == 404,
    )

# ── Verification: Feature 6 — Contract Enforcement ───────────────────

print("\n" + "=" * 60)
print("Feature 6: Strict Contract Enforcement")
print("=" * 60)

# Missing required field
resp = client.post("/v1/orchestrator/execute_sql", json={})
report.record(
    "Missing 'sql' field returns 422",
    resp.status_code == 422,
)

# Extra forbidden field
resp = client.post(
    "/v1/orchestrator/execute_sql",
    json={"sql": "SELECT 1;", "extra_field": "bad"},
)
report.record(
    "Extra field returns 422",
    resp.status_code == 422,
)

# Empty SQL
resp = client.post("/v1/orchestrator/execute_sql", json={"sql": ""})
report.record(
    "Empty SQL returns error",
    resp.status_code in (400, 422),
)

# ── Final Summary ────────────────────────────────────────────────────

print("\n" + "=" * 60)
all_passed = report.summary()
print("=" * 60)

sys.exit(0 if all_passed else 1)
