
"""Tests for app.middleware.sanitization (PII Sanitization Middleware)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request

from app.middleware.sanitization import (
    PiiSanitizationMiddleware,
    _redact_text,
    _redact_value,
)

# ── _redact_text ─────────────────────────────────────────────────────


class TestRedactText:
    def test_ssn_redacted(self):
        text = "Contact SSN: 123-45-6789 for verification."
        result = _redact_text(text)
        assert "123-45-6789" not in result
        assert "[REDACTED]" in result

    def test_ssn_multiple(self):
        text = "SSN1: 111-22-3333 and SSN2: 444-55-6666"
        result = _redact_text(text)
        assert "111-22-3333" not in result
        assert "444-55-6666" not in result
        assert result.count("[REDACTED]") == 2

    def test_email_redacted(self):
        text = "Email user@example.com for support."
        result = _redact_text(text)
        assert "user@example.com" not in result
        assert "[REDACTED]" in result

    def test_phone_redacted(self):
        text = "Call (555) 123-4567 for help."
        result = _redact_text(text)
        assert "555" not in result or "[REDACTED]" in result
        assert "[REDACTED]" in result

    def test_full_account_redacted(self):
        text = "Account: 1234567890123456"
        result = _redact_text(text)
        assert "1234567890123456" not in result
        assert "[REDACTED]" in result

    def test_short_number_not_redacted(self):
        """15-digit numbers should NOT be redacted (only 16+)."""
        text = "Short: 123456789012345"
        result = _redact_text(text)
        assert "123456789012345" in result

    def test_street_address_redacted(self):
        text = "Address: 123 Main Street"
        result = _redact_text(text)
        assert "Main Street" not in result
        assert "[REDACTED]" in result

    def test_money_field_redacted(self):
        text = "amount: 1,234.56"
        result = _redact_text(text)
        assert "1,234.56" not in result
        assert "[REDACTED_AMOUNT]" in result

    def test_balance_field_redacted(self):
        text = "balance: $5000.00"
        result = _redact_text(text)
        assert "5000.00" not in result
        assert "[REDACTED_AMOUNT]" in result

    def test_no_pii_unchanged(self):
        text = "Hello world, this is a clean message."
        result = _redact_text(text)
        assert result == text

    def test_empty_string(self):
        result = _redact_text("")
        assert result == ""

    def test_non_string_unchanged(self):
        result = _redact_text(42)
        assert result == 42


# ── _redact_value ────────────────────────────────────────────────────


class TestRedactValue:
    def test_string_redaction(self):
        result = _redact_value("SSN: 123-45-6789")
        assert "123-45-6789" not in result

    def test_dict_redaction(self):
        data = {"ssn": "123-45-6789", "name": "John"}
        result = _redact_value(data)
        assert "123-45-6789" not in result["ssn"]
        assert result["name"] == "John"

    def test_nested_dict_redaction(self):
        data = {"user": {"ssn": "111-22-3333", "email": "a@b.com"}}
        result = _redact_value(data)
        assert "111-22-3333" not in result["user"]["ssn"]
        assert "a@b.com" not in result["user"]["email"]

    def test_list_redaction(self):
        data = ["123-45-6789", "clean", "999-88-7777"]
        result = _redact_value(data)
        assert "123-45-6789" not in result[0]
        assert result[1] == "clean"
        assert "999-88-7777" not in result[2]

    def test_primitive_passthrough(self):
        assert _redact_value(42) == 42
        assert _redact_value(3.14) == 3.14
        assert _redact_value(True) is True
        assert _redact_value(None) is None

    def test_empty_dict(self):
        assert _redact_value({}) == {}

    def test_empty_list(self):
        assert _redact_value([]) == []


# ── PiiSanitizationMiddleware ────────────────────────────────────────


class TestPiiSanitizationMiddleware:
    """Test the middleware's PII redaction on request and response bodies."""

    @pytest.fixture
    def app_with_middleware(self):
        """Create a test FastAPI app with PII sanitization middleware."""
        
        from starlette.testclient import TestClient

        app = FastAPI()

        @app.post("/test")
        async def test_endpoint(request: Request):
            try:
                body = await request.json()
            except:
                from fastapi.responses import JSONResponse
                return JSONResponse(status_code=422, content={"detail": "Invalid JSON"})
            body = await request.json()
            return {"received": body}

        @app.get("/test")
        async def test_get():
            return {"data": "SSN: 123-45-6789", "email": "user@test.com"}

        app.add_middleware(PiiSanitizationMiddleware)
        return TestClient(app)

    def test_request_body_redacted(self, app_with_middleware):
        """Request body with PII should be redacted before processing."""
        payload = {
            "ssn": "123-45-6789",
            "email": "user@example.com",
            "amount": 1234,
        }
        response = app_with_middleware.post(
            "/test",
            json=payload,
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 200, response.text
        # The middleware redacts the request body before it reaches the handler
        result = response.json()
        # SSN should be redacted in the received data
        assert "123-45-6789" not in str(result.get("received", {}))

    def test_response_body_redacted(self, app_with_middleware):
        """Response body with PII should be redacted before returning."""
        response = app_with_middleware.get("/test")
        assert response.status_code == 200, response.text
        result = response.json()
        assert "123-45-6789" not in str(result)
        assert "user@test.com" not in str(result)

    def test_non_json_request_skipped(self, app_with_middleware):
        """Non-JSON request bodies should pass through unchanged."""
        response = app_with_middleware.post(
            "/test",
            content=b"not json",
            headers={"content-type": "text/plain"},
        )
        assert response.status_code == 422  # FastAPI validation error for non-JSON

    def test_non_json_response_skipped(self):
        """Non-JSON responses should pass through unchanged."""
        from fastapi import FastAPI, Response
        from starlette.testclient import TestClient

        app = FastAPI()

        @app.get("/raw")
        async def raw_endpoint():
            return Response(content="SSN: 123-45-6789", media_type="text/plain")

        app.add_middleware(PiiSanitizationMiddleware)
        client = TestClient(app)

        response = client.get("/raw")
        assert response.status_code == 200, response.text
        # Non-JSON responses are not redacted by this middleware
        assert "SSN: 123-45-6789" in response.text

    def test_clean_payload_passthrough(self, app_with_middleware):
        """Clean payloads without PII should pass through unchanged."""
        payload = {"name": "John", "age": 30}
        response = app_with_middleware.post(
            "/test",
            json=payload,
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["received"]["name"] == "John"
        assert result["received"]["age"] == 30
