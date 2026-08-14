from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from .bill_rate_import.isolated import pdf_sandbox_is_ready
from .config import get_settings
from .db import make_engine, make_session_factory
from .errors import PowerMeterError
from .logging_config import configure_logging
from .routes import (
    auth,
    billing,
    dashboard,
    devices,
    firmware,
    system,
    users,
)
from .routes import (
    settings as settings_routes,
)

settings = get_settings()
engine = make_engine(settings)
session_factory = make_session_factory(engine)

configure_logging(
    level_name=settings.log_level,
    log_dir=settings.log_dir,
    retention_days=settings.log_retention_days,
    service_name="api",
)
logger = structlog.get_logger()


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        request.state.correlation_id = correlation_id[:80]
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(correlation_id=request.state.correlation_id)
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()
        response.headers["X-Correlation-ID"] = request.state.correlation_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    logger.info("application_started", version="0.1.0-rc.1")
    try:
        yield
    finally:
        logger.info("application_stopped")
        await engine.dispose()


app = FastAPI(
    title="PowerMeter V2 API",
    version="0.1.0-rc.1",
    lifespan=lifespan,
    docs_url=None if settings.env == "production" else "/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json" if settings.env != "production" else None,
)
app.add_middleware(SecurityHeadersMiddleware)
app.include_router(auth.router)
app.include_router(devices.router)
app.include_router(firmware.router)
app.include_router(dashboard.router)
app.include_router(billing.router)
app.include_router(users.router)
app.include_router(settings_routes.router)
app.include_router(system.router)


@app.exception_handler(PowerMeterError)
async def power_meter_error(request: Request, exc: PowerMeterError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        media_type="application/problem+json",
        content={
            "type": f"https://power-meter.local/problems/{exc.code.lower()}",
            "title": exc.code.replace("_", " ").title(),
            "status": exc.status_code,
            "detail": exc.detail,
            "instance": request.url.path,
            "code": exc.code,
            "correlation_id": request.state.correlation_id,
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    safe_errors = [
        {
            "location": [str(part) for part in error["loc"]],
            "message": error["msg"],
            "type": error["type"],
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        media_type="application/problem+json",
        content={
            "type": "https://power-meter.local/problems/validation-error",
            "title": "Validation Error",
            "status": 422,
            "detail": "The request did not match the closed API schema.",
            "instance": request.url.path,
            "code": "VALIDATION_ERROR",
            "correlation_id": request.state.correlation_id,
            "errors": safe_errors,
        },
    )


@app.get("/health/live", include_in_schema=False)
async def health_live() -> dict[str, str]:
    return {"status": "live"}


@app.get("/health/ready", include_in_schema=False)
async def health_ready() -> JSONResponse:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "database": "unavailable"},
        )
    if settings.env != "test" and not await pdf_sandbox_is_ready():
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "database": "ready", "pdf_sandbox": "unavailable"},
        )
    return JSONResponse(
        content={
            "status": "ready",
            "database": "ready",
            "pdf_sandbox": "enforced" if settings.env != "test" else "test_portable",
        }
    )
