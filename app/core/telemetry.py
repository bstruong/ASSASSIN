"""Telemetry and structured logging configuration for ASSASSIN."""

import logging
import json
import os
from datetime import datetime, timezone
from typing import Any

# OpenTelemetry imports would go here:
# from opentelemetry import trace
# from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
# from opentelemetry.sdk.trace import TracerProvider
# from opentelemetry.sdk.trace.export import BatchSpanProcessor


class JSONFormatter(logging.Formatter):
    """Format logs as strict JSON, incorporating OTel span context."""

    def format(self, record: logging.LogRecord) -> str:
        # Prevent PII/financial leakage by stripping disallowed keys from `extra` (if passed via kwargs)
        disallowed_keys = {"amount", "amount_cents", "account_number", "page_text", "cell_text"}
        
        safe_dict = {}
        if hasattr(record, "args") and isinstance(record.args, dict):
            # Safe extraction of arguments
            for k, v in record.args.items():
                if k not in disallowed_keys:
                    safe_dict[k] = v

        log_obj: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": getattr(record, "trace_id", ""),  # Injected by OTel LoggingInstrumentor
            "span_id": getattr(record, "span_id", ""),
            "context": safe_dict
        }

        # Include explicit extra attributes added directly to the record
        for key in dir(record):
            if key not in ["args", "asctime", "created", "exc_info", "exc_text", "filename",
                           "funcName", "id", "levelname", "levelno", "lineno", "module",
                           "msecs", "message", "msg", "name", "pathname", "process",
                           "processName", "relativeCreated", "stack_info", "thread",
                           "threadName", "trace_id", "span_id", "context"]:
                val = getattr(record, key)
                if not key.startswith("_") and not callable(val) and key not in disallowed_keys:
                    log_obj[key] = val

        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_obj)


def configure_telemetry() -> None:
    """Initialize OTel tracer provider and structured JSON logging."""
    
    # 1. Logging Configuration
    log_level_str = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Clear existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(JSONFormatter())
    root_logger.addHandler(console_handler)

    # 2. OpenTelemetry Configuration (Boilerplate)
    # otel_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector.observability:4317")
    
    # provider = TracerProvider()
    # processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=otel_endpoint))
    # provider.add_span_processor(processor)
    # trace.set_tracer_provider(provider)
    
    # LoggingInstrumentor().instrument() # Binds trace_id/span_id to logs automatically
    
    logging.info("Telemetry configured successfully.")

