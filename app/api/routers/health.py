"""Health and readiness endpoints.

``/health`` is deliberately unauthenticated and cheap: orchestrators poll it
constantly, and an endpoint that needs credentials or a slow query is an
endpoint that reports the wrong thing under load.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status

from app.api.deps import AdminServiceDep, SettingsDep
from app.api.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Full health report")
async def health(admin: AdminServiceDep, response: Response) -> HealthResponse:
    """Report the status of every dependency."""
    report = await admin.health()
    payload = report.to_dict()
    if report.status != "healthy":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status=payload["status"],
        version=payload["version"],
        checks=payload["checks"],
        active_jobs=payload["active_jobs"],
        last_ingestion_at=payload["last_ingestion_at"],
    )


@router.get("/health/live", summary="Liveness probe")
async def live() -> dict[str, str]:
    """Answer as long as the process is running."""
    return {"status": "alive"}


@router.get("/health/ready", summary="Readiness probe")
async def ready(admin: AdminServiceDep, response: Response) -> dict[str, Any]:
    """Answer only when the database is reachable."""
    report = await admin.health()
    if not report.database:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "database": False}
    return {"status": "ready", "database": True, "cache": report.cache}


@router.get("/version", summary="Build information")
async def version(settings: SettingsDep) -> dict[str, str]:
    """Report the running version and environment."""
    from app import __version__

    return {
        "name": settings.app_name,
        "version": __version__,
        "environment": str(settings.environment),
    }


__all__ = ["router"]
