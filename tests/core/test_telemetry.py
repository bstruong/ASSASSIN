"""Tests for telemetry and log sanitization boundaries."""

import json
import logging
import io
from app.core.telemetry import JSONFormatter

def test_json_formatter_strips_pii_and_financials():
    """Ensure the JSON formatter blocks explicitly banned keys like 'amount_cents'."""
    logger = logging.getLogger("test_sanitization")
    logger.setLevel(logging.DEBUG)
    
    # Capture output in memory
    log_capture_string = io.StringIO()
    handler = logging.StreamHandler(log_capture_string)
    handler.setFormatter(JSONFormatter())
    logger.addHandler(handler)

    # Log with safe and unsafe extra fields
    logger.info(
        "Processing table row", 
        extra={
            "document_id": "doc-1234", 
            "amount_cents": 50000, 
            "account_number": "123456789",
            "is_valid": True
        }
    )

    # Read the output
    log_contents = log_capture_string.getvalue()
    log_dict = json.loads(log_contents)

    # Assertions
    assert log_dict["logger"] == "test_sanitization"
    assert log_dict["message"] == "Processing table row"
    assert log_dict["document_id"] == "doc-1234"
    assert log_dict["is_valid"] is True
    
    # Financial and PII fields MUST be stripped
    assert "amount_cents" not in log_dict, "Financial values leaked into logs!"
    assert "account_number" not in log_dict, "PII leaked into logs!"

def test_trace_context_propagation():
    """Placeholder: ensure trace_id and document_id propagate correctly."""
    # TODO: Initialize mock tracer provider
    # TODO: Start span with document_id="doc-999"
    # TODO: Log message
    # TODO: Assert log_dict["trace_id"] exists and matches active span
    pass
