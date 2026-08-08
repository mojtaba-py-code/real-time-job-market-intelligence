"""The FastAPI application factory.

Wiring happens once, here: settings are resolved, the database, cache, event
bus and services are constructed, middleware is stacked in the right order and
the routers are mounted. Everything downstream receives its dependencies rather
than reaching for globals, which is what makes the API testable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.errors import install_error_handlers
from app.api.middleware import (
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.api.routers import admin, alerts, analytics, companies, health, jobs, profiles, skills
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.core.security import configure_hasher
from app.events.bus import build_event_bus
from app.ingestion.registry import SourceRegistry
from app.services.admin_service import AdminService
from app.services.alert_service import AlertService
from app.services.analytics_service import AnalyticsService
from app.services.ingestion_service import IngestionService
from app.services.job_service import JobService
from app.services.profile_service import ProfileService
from app.storage.cache import build_cache
from app.storage.session import Database, get_database

log = get_logger(__name__)

DASHBOARD_DIR = Path(__file__).resolve().parents[2] / "dashboard"

API_DESCRIPTION = """
Real-time intelligence over public job-market data.

The platform ingests postings from permitted sources, normalizes and enriches
them, and exposes the result as search plus market analytics.

**Authentication** - send an API key in the `X-API-Key` header. Outside
production, read-only endpoints also answer anonymous requests so a fresh
installation is explorable.

**Rate limiting** - responses carry `X-RateLimit-Limit` and
`X-RateLimit-Remaining`; exceeding the budget returns `429` with `Retry-After`.

**Errors** - every non-2xx response has the shape
`{"error": {"code", "message", "details", "request_id"}}`.
""".strip()

TAGS_METADATA: list[dict[str, Any]] = [
    {"name": "health", "description": "Liveness, readiness and build information."},
    {"name": "jobs", "description": "Listing, retrieval and search over postings."},
    {"name": "analytics", "description": "Market, skill, salary and geography analytics."},
    {"name": "skills", "description": "Skill taxonomy, trends and the skill explorer."},
    {"name": "companies", "description": "Company hiring profiles."},
    {"name": "alerts", "description": "Market alert rules and notifications."},
    {"name": "profiles", "description": "Personal market-fit analysis."},
    {"name": "admin", "description": "Credentials, ingestion control and maintenance."},
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open and close every long-lived resource."""
    settings: Settings = app.state.settings
    settings.ensure_directories()
    log.info(
        "api.starting",
        version=__version__,
        environment=str(settings.environment),
        database=settings.database.url.split("://", 1)[0],
    )
    try:
        yield
    finally:
        log.info("api.stopping")
        await app.state.cache.aclose()
        await app.state.event_bus.aclose()
        await app.state.source_registry.aclose()
        await app.state.database.dispose()


def create_app(settings: Settings | None = None, *, database: Database | None = None) -> FastAPI:
    """Build the application.

    Args:
        settings: Overrides the environment-derived configuration.
        database: Injects a database (used by tests to bind a temporary one).
    """
    resolved = settings or get_settings()
    configure_logging(
        level=resolved.observability.log_level,
        fmt=resolved.observability.log_format,
        log_file=resolved.observability.log_file,
        service="jobintel-api",
    )
    configure_hasher(
        time_cost=resolved.security.password_hash_time_cost,
        memory_cost_kib=resolved.security.password_hash_memory_cost_kib,
        parallelism=resolved.security.password_hash_parallelism,
    )

    docs_enabled = resolved.api.docs_enabled and not resolved.environment.is_production_like
    app = FastAPI(
        title=resolved.app_name,
        version=__version__,
        description=API_DESCRIPTION,
        openapi_tags=TAGS_METADATA,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
        root_path=resolved.api.root_path,
        lifespan=lifespan,
        contact={"name": "Job Market Intelligence Platform"},
        license_info={"name": "MIT"},
    )

    _wire_state(app, resolved, database)
    _install_middleware(app, resolved)
    install_error_handlers(app)
    _mount_routers(app)
    _mount_dashboard(app, resolved)
    return app


def _wire_state(app: FastAPI, settings: Settings, database: Database | None) -> None:
    """Construct the dependency graph once and park it on ``app.state``."""
    db = database or get_database(settings)
    cache = build_cache(settings.cache)
    bus = build_event_bus(settings.events, redis_url=settings.cache.url)
    registry = SourceRegistry.from_file(settings=settings)

    app.state.settings = settings
    app.state.database = db
    app.state.cache = cache
    app.state.event_bus = bus
    app.state.source_registry = registry
    app.state.job_service = JobService(db, cache=cache, settings=settings)
    app.state.analytics_service = AnalyticsService(db, cache=cache, settings=settings)
    app.state.alert_service = AlertService(db, settings=settings, event_bus=bus)
    app.state.profile_service = ProfileService(db, settings=settings)
    app.state.admin_service = AdminService(db, settings=settings, cache=cache)
    app.state.ingestion_service = IngestionService(
        database=db, registry=registry, event_bus=bus, settings=settings
    )


def _install_middleware(app: FastAPI, settings: Settings) -> None:
    """Stack middleware.

    Starlette runs middleware in reverse registration order, so the request
    context (which assigns the request id every other layer logs) is added
    last and therefore runs first.
    """
    if settings.api.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.api.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", settings.security.api_key_header],
            max_age=600,
        )
    app.add_middleware(
        RateLimitMiddleware,
        cache=app.state.cache,
        settings=settings.api,
        namespace=settings.cache.namespace,
    )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.security.max_request_body_bytes)
    app.add_middleware(SecurityHeadersMiddleware, settings=settings.security)
    app.add_middleware(RequestContextMiddleware)


def _mount_routers(app: FastAPI) -> None:
    for router in (
        health.router,
        jobs.router,
        analytics.router,
        skills.router,
        companies.router,
        alerts.router,
        profiles.router,
        admin.router,
    ):
        app.include_router(router)


def _mount_dashboard(app: FastAPI, settings: Settings) -> None:
    """Serve the static dashboard from the same origin as the API."""
    if not settings.api.serve_dashboard or not DASHBOARD_DIR.exists():
        return

    app.mount(
        "/dashboard",
        StaticFiles(directory=str(DASHBOARD_DIR), html=True),
        name="dashboard",
    )

    @app.get("/", include_in_schema=False)
    async def index() -> RedirectResponse:
        return RedirectResponse(url="/dashboard/")

    @app.get("/favicon.ico", include_in_schema=False, response_model=None)
    async def favicon() -> FileResponse | RedirectResponse:
        icon = DASHBOARD_DIR / "favicon.svg"
        if icon.exists():
            return FileResponse(icon, media_type="image/svg+xml")
        return RedirectResponse(url="/dashboard/")


# The application is built by a factory rather than at import time, so that
# importing this module never opens a database connection. Run it with:
#     uvicorn "app.api.app:create_app" --factory
__all__ = ["DASHBOARD_DIR", "create_app", "lifespan"]
