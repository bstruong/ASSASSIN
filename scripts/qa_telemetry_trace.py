#!/usr/bin/env python3
"""QA: structured logs copy the active span trace_id and still strip amount_cents.

Run with: uv run python scripts/qa_telemetry_trace.py

Manual UI QA: not applicable (no user-visible UI).
"""

from __future__ import annotations

import io
import json
import logging
import sys

from opentelemetry.sdk.trace import TracerProvider

from app.core.telemetry import JSONFormatter


def main() -> int:
    print("QA: Telemetry trace context on structured JSON logs")
    print(
        "Capability: a log emitted inside an active span carries that span's trace_id."
    )
    print("Negative check: amount_cents is still omitted from the JSON log.")
    print("Manual UI QA: not applicable (no user-visible UI).")
    print("=" * 60)

    provider = TracerProvider()
    tracer = provider.get_tracer("qa_telemetry_trace")
    logger = logging.getLogger("qa_telemetry_trace")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    capture = io.StringIO()
    handler = logging.StreamHandler(capture)
    handler.setFormatter(JSONFormatter())
    logger.addHandler(handler)

    failed = False
    try:
        with tracer.start_as_current_span(
            "document.process",
            attributes={"document_id": "doc-999"},
        ) as span:
            logger.info(
                "Processing synthetic document",
                extra={
                    "document_id": "doc-999",
                    "amount_cents": 50000,
                    "is_valid": True,
                },
            )
            expected_trace_id = format(span.get_span_context().trace_id, "032x")

        log_dict = json.loads(capture.getvalue())
        actual_trace_id = log_dict.get("trace_id", "")
        if actual_trace_id and actual_trace_id == expected_trace_id:
            print(f"[PASS] log trace_id matches active span ({actual_trace_id})")
        else:
            print(
                f"[FAIL] log trace_id {actual_trace_id!r} "
                f"does not match active span {expected_trace_id!r}"
            )
            failed = True

        if log_dict.get("document_id") == "doc-999":
            print("[PASS] synthetic document_id doc-999 present on log")
        else:
            print("[FAIL] synthetic document_id missing from log")
            failed = True

        if "amount_cents" not in log_dict and "amount_cents" not in log_dict.get(
            "context", {}
        ):
            print("[PASS] amount_cents stripped from JSON log")
        else:
            print("[FAIL] amount_cents leaked into JSON log")
            failed = True
    finally:
        logger.removeHandler(handler)
        provider.shutdown()

    print("=" * 60)
    if failed:
        print("[FAIL] telemetry trace context verification")
        return 1
    print("[PASS] telemetry trace context verification")
    return 0


if __name__ == "__main__":
    sys.exit(main())
