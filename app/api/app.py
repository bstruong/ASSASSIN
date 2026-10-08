"""FastAPI application factory for the ASSASSIN Local Ingest API.

Guarantees:
- Zero PII egress across all routes and responses.
- W3C Trace Context and correlation ID propagation.
- Strict contract validation and fail-loud semantics.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.api.dashboard import dashboard_api
from app.api.dashboard import router as dashboard_router
from app.api.routes import router
from app.core.telemetry import configure_telemetry
from app.db.connection import init_db
from app.middleware.sanitization import PiiSanitizationMiddleware
from app.routers.frontier import router as frontier_router
from app.routers.orchestrator import router as orchestrator_router


class TraceContextMiddleware(BaseHTTPMiddleware):
    """Inject W3C trace context / request correlation ID into request state and response headers."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        trace_id = (
            request.headers.get("x-trace-id")
            or request.headers.get("traceparent")
            or uuid.uuid4().hex
        )
        request.state.trace_id = trace_id
        response = await call_next(request)
        response.headers["X-Trace-ID"] = trace_id
        return response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifecycle hooks: configure telemetry and initialize database schema."""
    configure_telemetry()
    init_db()
    yield


def create_app() -> FastAPI:
    """Create and configure the ASSASSIN FastAPI application."""
    app = FastAPI(
        title="ASSASSIN Local Ingest API",
        description="Local financial document intelligence pipeline. Zero PII egress.",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(TraceContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost", "http://127.0.0.1"],
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    # Outermost: scrub PII from JSON I/O; frontier paths also drop row-PII keys.
    app.add_middleware(PiiSanitizationMiddleware)

    app.include_router(router)
    app.include_router(dashboard_router)
    app.include_router(dashboard_api)
    app.include_router(frontier_router)
    app.include_router(orchestrator_router)

    static_dir = Path(__file__).resolve().parent.parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app


app = create_app()
