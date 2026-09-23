"""FastAPI application factory."""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db import get_engine, session_scope
from app.deps import manager_required
from app.errors import install_error_handlers
from app.logging_setup import configure_logging, request_id_var, user_id_var
from app.routers import (
    auth,
    doc_sets,
    events,
    files,
    interview_taxonomy,
    interviews,
    jobs,
    ops,
    profiles,
    settings as settings_router,
    stats,
    users,
)

logger = logging.getLogger(__name__)
API_PREFIX = "/api/v1"

#: SEC-8: the API serves JSON only, so a strict CSP costs nothing here.
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}

#: SEC-8: the standalone /api/docs page is HTML, so it gets a relaxed policy.
DOCS_CSP = (
    "default-src 'none'; script-src 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'unsafe-inline' https://cdn.jsdelivr.net; img-src data: https://fastapi.tiangolo.com; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging()
    from app.services import storage

    storage.ensure_dir(storage.storage_root())
    storage.ensure_dir(storage.tmp_dir())
    if settings.auto_create_schema or settings.autocreate_tables_on_start:
        from app.db import Base
        from app import models  # noqa: F401 - register tables

        engine = get_engine()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    async with session_scope() as session:
        from app.services import settings_store
        from app.services.theme import seed_default_theme

        await settings_store.ensure_defaults(session)
        await seed_default_theme(session)
        from app.services.seeds import ensure_interview_taxonomy

        await ensure_interview_taxonomy(session)
        if settings.seed_on_start:
            from app.services.seeds import seed_demo_data

            await seed_demo_data(session)
    driver = None
    if settings.inline_pipeline:
        from app.workers.inline import InlineDriver

        if not settings.is_sqlite and settings.environment.lower() in {"production", "prod"}:
            logger.warning("INLINE_PIPELINE is enabled in production; use Celery workers instead")
        driver = InlineDriver()
        await driver.start()
        logger.info("inline pipeline driver started (single-process dev mode)")
    try:
        yield
    finally:
        if driver is not None:
            await driver.stop()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Remote Flow API",
        version=settings.app_version,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.public_url, "http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("request failed", extra={"path": request.url.path})
            raise
        finally:
            user_id_var.set(None)
        response.headers["X-Request-ID"] = request_id
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if (response.headers.get("content-type") or "").startswith("text/html"):
            # The manager-only API docs page needs the Swagger CDN and its inline bootstrap.
            response.headers["Content-Security-Policy"] = DOCS_CSP
        if request.url.path.startswith(API_PREFIX):
            logger.info(
                "request",
                extra={
                    "path": request.url.path,
                    "method": request.method,
                    "status": response.status_code,
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                },
            )
        return response

    for router in (
        auth.router,
        events.router,
        jobs.router,
        doc_sets.router,
        files.router,
        profiles.router,
        interviews.router,
        interview_taxonomy.router,
        settings_router.router,
        users.router,
        stats.router,
    ):
        app.include_router(router, prefix=API_PREFIX)
    app.include_router(ops.router)

    @app.get("/api/openapi.json", include_in_schema=False)
    async def openapi_for_managers(_=Depends(manager_required)):
        return JSONResponse(app.openapi())

    @app.get("/api/docs", include_in_schema=False)
    async def docs_for_managers(_=Depends(manager_required)):
        return get_swagger_ui_html(openapi_url="/api/openapi.json", title="Remote Flow API")

    @app.get("/", include_in_schema=False)
    async def root():
        return {"service": settings.app_name, "version": settings.app_version, "docs": "/api/docs"}

    return app


app = create_app()
