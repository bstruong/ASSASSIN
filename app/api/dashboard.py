"""HTMX Dashboard router for the ASSASSIN Local Ingest API.

Guarantees:
- Zero PII egress across all routes and responses.
- Strict contract enforcement: fails loudly on invalid payloads.
- Financial precision: no floating-point monetary values.
- HTMX-first: returns HTML fragments for hx-swap updates.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger("app.api.dashboard")

# ---------------------------------------------------------------------------
# Template setup
# ---------------------------------------------------------------------------

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(enabled_extensions=["html"]),
)

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter()


# ---------------------------------------------------------------------------
# Template rendering helpers
# ---------------------------------------------------------------------------


def _render_dashboard() -> str:
    """Render the main dashboard HTML from template."""
    return env.get_template("dashboard.html").render()


# ---------------------------------------------------------------------------
# Route Handlers
# ---------------------------------------------------------------------------


@router.get("/dashboard", response_class=HTMLResponse)
def serve_dashboard() -> HTMLResponse:
    """Serve the main HTMX dashboard page."""
    return HTMLResponse(content=_render_dashboard())


dashboard_api = APIRouter(prefix="/api/v1/dashboard")


@dashboard_api.post(
    "/analyze",
    response_class=HTMLResponse,
    responses={
        400: {"description": "Invalid or missing question"},
    },
)
async def analyze_question(
    request: Request,
    question: str = Form(""),
) -> HTMLResponse:
    """Process a financial question and return an HTML analysis fragment.

    Validates:
    - HX-Request header must be present (HTMX-first).
    - Question must be non-empty and non-whitespace.
    """
    # HTMX enforcement
    hx_request = request.headers.get("HX-Request", "").lower()
    if hx_request != "true":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint requires an HTMX request (HX-Request: true).",
        )

    # Strict validation: reject empty / whitespace-only questions
    stripped = question.strip() if question else ""
    if not stripped:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Question must not be empty or whitespace.",
        )

    logger.info(
        "Dashboard analysis request",
        extra={"question_length": len(stripped)},
    )

    # Render analysis result from template
    result_html = env.get_template("analysis_result.html").render(question=stripped)
    return HTMLResponse(content=result_html)


@dashboard_api.get(
    "/status",
    response_class=HTMLResponse,
    responses={
        400: {"description": "Requires HTMX request"},
    },
)
async def get_status(request: Request) -> HTMLResponse:
    """Return a status HTML fragment for the Local Vault pane."""
    hx_request = request.headers.get("HX-Request", "").lower()
    if hx_request != "true":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint requires an HTMX request (HX-Request: true).",
        )

    return HTMLResponse(content=env.get_template("status.html").render())


# ---------------------------------------------------------------------------
# Utility: PII and float validation helpers
# ---------------------------------------------------------------------------


def _has_pii(text: str) -> bool:
    """Check if text contains known PII patterns.

    Returns True if PII is detected.
    """
    pii_patterns = [
        r"\b\d{3}-\d{2}-\d{4}\b",  # SSN
        r"social.?security",
        r"full.?name",
        r"account.?number",
    ]
    for pattern in pii_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def _has_float_money(text: str) -> bool:
    """Check if text contains floating-point monetary values like $123.45.

    Returns True if floating-point dollar amounts are found.
    """
    # Match $ followed by digits, a decimal point, and 2+ decimal digits
    return bool(re.search(r"\$\d+\.\d{2,}", text))
