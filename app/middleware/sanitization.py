"""PII Sanitization Interceptor / Middleware.

This middleware scrubs personally identifiable information (PII) and
sensitive financial data from request/response payloads before they are
dispatched to the frontier model or logged.

PII patterns detected and redacted:
- Social Security Numbers (SSN): XXX-XX-XXXX
- Full account numbers (16+ digit sequences)
- Raw account masks replaced with [REDACTED]
- Email addresses
- Phone numbers
- Physical street addresses (patterns like "123 Main St")
- Unmasked monetary amounts (stored as cents)

All redactions are logged with structured JSON for observability
without leaking actual PII.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from fastapi import Request, Response
from starlette.middleware.base import (
    BaseHTTPMiddleware,
    RequestResponseEndpoint,
    _StreamingResponse,
)
from starlette.responses import StreamingResponse

logger = logging.getLogger("app.middleware.sanitization")

# PII / Sensitive Data Patterns

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "SSN",
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    ),
    (
        "EMAIL",
        re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
    ),
    (
        "PHONE_US",
        re.compile(r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)?\d{3}[-.\s]?\d{4}\b"),
    ),
    (
        "FULL_ACCOUNT",
        re.compile(r"\b\d{16,}\b"),
    ),
    (
        "STREET_ADDRESS",
        re.compile(
            r"\b\d{1,5}\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+(?:Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Road|Rd|Lane|Ln|Way|Court|Ct)\b",
            re.IGNORECASE,
        ),
    ),
]

_MONEY_FIELD_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "MONEY_FIELD",
        re.compile(
            r"((?:amount|balance|payment|charge|fee|total)\s*[:=]?\s*)(?:\$)?(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)",
            re.IGNORECASE,
        ),
    ),
]


def _redact_text(text: str) -> str:
    """Redact all known PII patterns from a text string."""
    if not isinstance(text, str):
        return text

    for pattern_name, pattern in _PATTERNS:
        count = len(pattern.findall(text))
        if count:
            logger.info(
                "PII pattern found and redacted",
                extra={"pattern": pattern_name, "occurrences": count},
            )
            text = pattern.sub("[REDACTED]", text)

    for pattern_name, pattern in _MONEY_FIELD_PATTERNS:
        count = len(pattern.findall(text))
        if count:
            logger.info(
                "Financial amount found and redacted",
                extra={"pattern": pattern_name, "occurrences": count},
            )
            text = pattern.sub(r"\1[REDACTED_AMOUNT]", text)

    return text


def _redact_value(value: Any) -> Any:
    """Recursively redact PII from a value (string, dict, list, or primitive)."""
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


class PiiSanitizationMiddleware(BaseHTTPMiddleware):
    """Middleware that scrubs PII from request and response payloads."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Dispatch handler: redact PII from request and response bodies."""
        # Redact request body
        body = await request.body()
        request_content_type = request.headers.get("content-type", "")

        if body and request_content_type.startswith("application/json"):
            try:
                payload = json.loads(body)
                redacted_payload = _redact_value(payload)
                if redacted_payload != payload:
                    body = json.dumps(redacted_payload).encode("utf-8")
                    logger.info(
                        "Request body PII redacted",
                        extra={"route": request.url.path},
                    )
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

        # Forward request with redacted body
        request._body = body

        response: Response = await call_next(request)

        # Redact response body
        response_content_type = response.headers.get("content-type", "")
        if response_content_type.startswith("application/json"):
            if isinstance(response, (StreamingResponse, _StreamingResponse)):
                body_chunks: list[bytes] = []
                async for chunk in response.body_iterator:
                    body_chunks.append(
                        chunk if isinstance(chunk, bytes) else chunk.encode()
                    )
                response_body = b"".join(body_chunks)
            else:
                response_body = getattr(response, "body", b"")

            if response_body:
                try:
                    resp_data = json.loads(response_body)
                    redacted_resp = _redact_value(resp_data)
                    logger.info(
                        "Response body PII processed",
                        extra={"route": request.url.path},
                    )
                    return Response(
                        content=json.dumps(redacted_resp).encode("utf-8"),
                        status_code=response.status_code,
                        media_type="application/json",
                    )
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass

        return response
