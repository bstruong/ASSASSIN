"""HTMX Dashboard router for the ASSASSIN Local Ingest API.

Guarantees:
- Zero PII egress across all routes and responses.
- Strict contract enforcement: fails loudly on invalid payloads.
- Financial precision: no floating-point monetary values.
- HTMX-first: returns HTML fragments for hx-swap updates.

Mutation scope: this module is intentionally **out of mutmut scope**
(``do_not_mutate`` / diff allowlist). Cover behavior with unit tests and
Playwright E2E; do not gate PRs on dashboard mutation kill rates.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.mcp.cloud_tools import CloudToolError, get_financial_summary

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


def _frontier_audit_error(detail: str) -> HTMLResponse:
    """Return the rejected frontier fragment with HTTP 400."""
    return HTMLResponse(
        content=env.get_template("frontier_audit_error.html").render(detail=detail),
        status_code=status.HTTP_400_BAD_REQUEST,
    )


@dashboard_api.post(
    "/frontier-audit",
    response_class=HTMLResponse,
    responses={
        400: {"description": "Missing HTMX header or invalid group_by"},
    },
)
async def frontier_audit(
    request: Request,
    group_by: str = Form(""),
) -> HTMLResponse:
    """Return a Tier-2 aggregate fragment for the frontier audit button.

    The fragment contains integer-cent totals only. Row-level fields are not
    rendered. Invalid grouping fails loudly.
    """
    hx_request = request.headers.get("HX-Request", "").lower()
    if hx_request != "true":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint requires an HTMX request (HX-Request: true).",
        )

    stripped = group_by.strip() if group_by else ""
    if not stripped:
        return _frontier_audit_error("group_by must not be empty or whitespace.")

    logger.info(
        "Dashboard frontier audit requested",
        extra={"group_by": stripped},
    )

    try:
        summary = get_financial_summary(group_by=stripped)
    except CloudToolError as exc:
        logger.warning(
            "Dashboard frontier audit rejected",
            extra={"group_by": stripped, "error_type": type(exc).__name__},
        )
        return _frontier_audit_error(str(exc))

    totals = summary["totals"]
    return HTMLResponse(
        content=env.get_template("frontier_audit_result.html").render(
            group_by=summary["group_by_column"],
            totals=totals,
        )
    )
