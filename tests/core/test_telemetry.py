"""Tests for telemetry and log sanitization boundaries."""

import io
import json
import logging

from app.core.telemetry import JSONFormatter, configure_telemetry


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
            "is_valid": True,
        },
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


def test_json_formatter_with_exception():
    """Ensure exceptions are serialized in formatted log output."""
    logger = logging.getLogger("test_exception_logger")
    log_capture = io.StringIO()
    handler = logging.StreamHandler(log_capture)
    handler.setFormatter(JSONFormatter())
    logger.addHandler(handler)

    try:
        raise ValueError("Simulated pipeline failure")
    except ValueError:
        logger.exception("Processing failed")

    log_dict = json.loads(log_capture.getvalue())
    assert log_dict["level"] == "ERROR"
    assert "exception" in log_dict
    assert "Simulated pipeline failure" in log_dict["exception"]


def test_json_formatter_with_record_args_dict():
    """Ensure args passed as dictionary have disallowed keys filtered."""
    logger = logging.getLogger("test_args_logger")
    log_capture = io.StringIO()
    handler = logging.StreamHandler(log_capture)
    handler.setFormatter(JSONFormatter())
    logger.addHandler(handler)

    record = logger.makeRecord(
        name="test_args_logger",
        level=logging.INFO,
        fn="test_fn",
        lno=1,
        msg="Arg test",
        args={"safe_key": "ok", "amount_cents": 100},
        exc_info=None,
    )
    formatted = handler.format(record)
    log_dict = json.loads(formatted)
    assert log_dict["context"]["safe_key"] == "ok"
    assert "amount_cents" not in log_dict["context"]


def test_configure_telemetry(monkeypatch):
    """Ensure configure_telemetry sets root level and JSON handler."""

    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    configure_telemetry()

    root_logger = logging.getLogger()
    assert root_logger.level == logging.DEBUG
    assert len(root_logger.handlers) >= 1
    assert any(isinstance(h.formatter, JSONFormatter) for h in root_logger.handlers)


def test_trace_context_propagation():
    """Ensure the active span trace_id is copied onto the JSON log."""
    from opentelemetry.sdk.trace import TracerProvider

    provider = TracerProvider()
    tracer = provider.get_tracer("test_telemetry")
    logger = logging.getLogger("test_trace_context")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    log_capture = io.StringIO()
    handler = logging.StreamHandler(log_capture)
    handler.setFormatter(JSONFormatter())
    logger.addHandler(handler)

    try:
        with tracer.start_as_current_span(
            "document.process",
            attributes={"document_id": "doc-999"},
        ) as span:
            logger.info(
                "Processing document",
                extra={"document_id": "doc-999"},
            )
            active_trace_id = format(span.get_span_context().trace_id, "032x")
    finally:
        logger.removeHandler(handler)
        provider.shutdown()

    log_dict = json.loads(log_capture.getvalue())
    assert log_dict["document_id"] == "doc-999"
    assert "trace_id" in log_dict
    assert log_dict["trace_id"]
    assert log_dict["trace_id"] == active_trace_id
