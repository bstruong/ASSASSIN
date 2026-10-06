"""QA Verification Script for Step 12: HTMX Dashboard.

This script verifies the dashboard feature without reading code or diffs.
It tests both positive (happy path) and negative (invariant enforcement) cases.

Usage:
    uv run python scripts/qa_step12.py
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Server startup
# ---------------------------------------------------------------------------
import subprocess
import sys
import time
from http.client import HTTPConnection


def start_server() -> subprocess.Popen:
    """Start the uvicorn server in the background and wait for readiness."""
    proc = subprocess.Popen(
        [
            "uv",
            "run",
            "uvicorn",
            "app.api.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
            "--log-level",
            "warning",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Wait up to 10 seconds for the server to be ready
    for _ in range(20):
        time.sleep(0.5)
        try:
            conn = HTTPConnection("127.0.0.1", 8765, timeout=2)
            conn.request("GET", "/api/v1/health")
            resp = conn.getresponse()
            resp.read()
            conn.close()
            if resp.status == 200:
                return proc
        except OSError:
            continue

    proc.terminate()
    proc.wait()
    print("[FAIL] Server failed to start within 10 seconds.")
    sys.exit(1)


def http_get(path: str) -> tuple[int, str]:
    """Perform a GET request and return (status_code, body)."""
    conn = HTTPConnection("127.0.0.1", 8765, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    conn.close()
    return resp.status, body


def http_post(path: str, data: dict[str, str]) -> tuple[int, str]:
    """Perform a POST request and return (status_code, body)."""
    conn = HTTPConnection("127.0.0.1", 8765, timeout=5)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    body_str = "&".join(f"{k}={v}" for k, v in data.items())
    conn.request("POST", path, body=body_str, headers=headers)
    resp = conn.getresponse()
    result = resp.read().decode("utf-8")
    conn.close()
    return resp.status, result


def http_post_htmx(path: str, data: dict[str, str]) -> tuple[int, str]:
    """Perform an HTMX POST request (with HX-Request header)."""
    conn = HTTPConnection("127.0.0.1", 8765, timeout=5)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "HX-Request": "true",
    }
    body_str = "&".join(f"{k}={v}" for k, v in data.items())
    conn.request("POST", path, body=body_str, headers=headers)
    resp = conn.getresponse()
    result = resp.read().decode("utf-8")
    conn.close()
    return resp.status, result


def http_get_htmx(path: str) -> tuple[int, str]:
    """Perform an HTMX GET request."""
    conn = HTTPConnection("127.0.0.1", 8765, timeout=5)
    conn.request("GET", path, headers={"HX-Request": "true"})
    resp = conn.getresponse()
    result = resp.read().decode("utf-8")
    conn.close()
    return resp.status, result


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------

passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        print(f"  [PASS] {name}")
        passed += 1
    else:
        print(f"  [FAIL] {name} -- {detail}")
        failed += 1


def main() -> None:
    print("=" * 60)
    print("Step 12: HTMX Dashboard QA Verification")
    print("=" * 60)

    proc = start_server()
    try:
        # ------------------------------------------------------------------
        # 1. Dashboard page loads
        # ------------------------------------------------------------------
        print("\n[1] Dashboard Page Loading")
        status, body = http_get("/dashboard")
        check("Dashboard returns 200", status == 200, f"got {status}")
        check("Contains 'ASSASSIN'", "ASSASSIN" in body)
        check("Contains 'The AI Analyst'", "The AI Analyst" in body)
        check("Contains 'The Local Vault'", "The Local Vault" in body)

        # ------------------------------------------------------------------
        # 2. HTMX attributes present
        # ------------------------------------------------------------------
        print("\n[2] HTMX Attributes")
        check("hx-post present", "hx-post" in body)
        check("hx-swap present", "hx-swap" in body)
        check("hx-target present", "hx-target" in body)

        # ------------------------------------------------------------------
        # 3. Analyze endpoint — happy path
        # ------------------------------------------------------------------
        print("\n[3] Analyze Endpoint (Happy Path)")
        status, body = http_post_htmx(
            "/api/v1/dashboard/analyze",
            {"question": "Show me my total spending last month"},
        )
        check("Analyze returns 200", status == 200, f"got {status}")
        check("Contains 'Analysis Result'", "Analysis Result" in body)
        check("Contains question text", "Show me my total spending last month" in body)
        check("Contains integer cents values", "1250000" in body)

        # ------------------------------------------------------------------
        # 4. Analyze endpoint — empty question (negative)
        # ------------------------------------------------------------------
        print("\n[4] Analyze Endpoint (Negative: Empty Question)")
        status, body = http_post_htmx("/api/v1/dashboard/analyze", {"question": ""})
        check("Empty question returns 400", status == 400, f"got {status}")

        # ------------------------------------------------------------------
        # 5. Analyze endpoint — missing question (negative)
        # ------------------------------------------------------------------
        print("\n[5] Analyze Endpoint (Negative: Missing Question)")
        status, body = http_post_htmx("/api/v1/dashboard/analyze", {})
        check("Missing question returns 400", status == 400, f"got {status}")

        # ------------------------------------------------------------------
        # 6. Analyze endpoint — non-HTMX request (negative)
        # ------------------------------------------------------------------
        print("\n[6] Analyze Endpoint (Negative: Non-HTMX)")
        status, body = http_post("/api/v1/dashboard/analyze", {"question": "test"})
        check("Non-HTMX request returns 400", status == 400, f"got {status}")

        # ------------------------------------------------------------------
        # 7. Status endpoint
        # ------------------------------------------------------------------
        print("\n[7] Status Endpoint")
        status, body = http_get_htmx("/api/v1/dashboard/status")
        check("Status returns 200", status == 200, f"got {status}")
        check("Contains engine status", "Engine Online" in body)

        # ------------------------------------------------------------------
        # 8. Status endpoint — non-HTMX (negative)
        # ------------------------------------------------------------------
        print("\n[8] Status Endpoint (Negative: Non-HTMX)")
        status, body = http_get("/api/v1/dashboard/status")
        check("Non-HTMX status returns 400", status == 400, f"got {status}")

        # ------------------------------------------------------------------
        # 9. PII no-egress
        # ------------------------------------------------------------------
        print("\n[9] PII No-Egress")
        pii_patterns = ["ssn", "social security", "full_name", "account_number"]
        for pattern in pii_patterns:
            check(f"No PII pattern '{pattern}'", pattern.lower() not in body.lower())

        # ------------------------------------------------------------------
        # 10. Financial precision — no floats
        # ------------------------------------------------------------------
        print("\n[10] Financial Precision (Integer Cents)")
        dollar_floats = re.findall(r"\$\d+\.\d{2,}", body)
        check(
            "No floating-point dollar amounts",
            len(dollar_floats) == 0,
            str(dollar_floats),
        )

        # ------------------------------------------------------------------
        # Summary
        # ------------------------------------------------------------------
        print("\n" + "=" * 60)
        print(f"Results: {passed} passed, {failed} failed")
        print("=" * 60)

        if failed > 0:
            sys.exit(1)
        else:
            print("\n🎉 All Step 12 QA checks passed.")
            sys.exit(0)

    finally:
        proc.terminate()
        proc.wait(timeout=5)


if __name__ == "__main__":
    main()
