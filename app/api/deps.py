"""Dependency injection for the HTTP layer.

Services are built once at startup and stored on ``app.state``; the providers
here hand them to the routers. That keeps routers free of construction logic
and makes every dependency trivially replaceable in tests.
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Header, Query, Request, params

from app.core.config import Settings
from app.core.errors import AuthenticationError, AuthorizationError
from app.core.security import parse_api_key
from app.models.analytics import AnalyticsFilter
from app.models.enums import ExperienceLevel, MarketSegment, RemoteType, Scope
from app.services.admin_service import AdminService
from app.services.alert_service import AlertService
from app.services.analytics_service import AnalyticsService
from app.services.ingestion_service import IngestionService
from app.services.job_service import JobService
from app.services.profile_service import ProfileService
from app.storage.cache import CacheBackend
from app.storage.repositories.auth import Principal
from app.storage.session import Database


def get_settings_dep(request: Request) -> Settings:
    """The application settings."""
    return request.app.state.settings  # type: ignore[no-any-return]


def get_database_dep(request: Request) -> Database:
    return request.app.state.database  # type: ignore[no-any-return]


def get_cache_dep(request: Request) -> CacheBackend:
    return request.app.state.cache  # type: ignore[no-any-return]


def get_job_service(request: Request) -> JobService:
    return request.app.state.job_service  # type: ignore[no-any-return]


def get_analytics_service(request: Request) -> AnalyticsService:
    return request.app.state.analytics_service  # type: ignore[no-any-return]


def get_alert_service(request: Request) -> AlertService:
    return request.app.state.alert_service  # type: ignore[no-any-return]


def get_profile_service(request: Request) -> ProfileService:
    return request.app.state.profile_service  # type: ignore[no-any-return]


def get_admin_service(request: Request) -> AdminService:
    return request.app.state.admin_service  # type: ignore[no-any-return]


def get_ingestion_service(request: Request) -> IngestionService:
    return request.app.state.ingestion_service  # type: ignore[no-any-return]


async def get_principal(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> Principal | None:
    """Resolve the caller, or ``None`` when the request is anonymous.

    Authentication is optional at this layer; :func:`require_scope` decides
    whether a given endpoint tolerates an anonymous caller.
    """
    if not x_api_key:
        return None
    admin: AdminService = request.app.state.admin_service
    key_id, presented = parse_api_key(x_api_key)
    principal = await admin.authenticate(key_id, presented)
    if principal is None:
        raise AuthenticationError("Invalid or revoked API key")
    request.state.principal = principal
    return principal


def require_scope(scope: Scope) -> params.Depends:
    """Build a dependency that enforces one scope.

    When authentication is not configured at all (no keys issued yet) read
    scopes stay open so a fresh installation is usable; write scopes never do.
    """

    async def dependency(
        request: Request,
        principal: Annotated[Principal | None, Depends(get_principal)] = None,
    ) -> Principal | None:
        settings: Settings = request.app.state.settings
        if principal is None:
            if scope in _PUBLIC_SCOPES and not settings.environment.is_production_like:
                return None
            raise AuthenticationError("An API key is required for this endpoint")
        if not principal.has_scope(scope):
            raise AuthorizationError(
                f"the {scope.value!r} scope is required",
                details={"required_scope": scope.value},
            )
        return principal

    return cast("params.Depends", Depends(dependency))


#: Read-only scopes that an unauthenticated caller may use outside production.
_PUBLIC_SCOPES = frozenset({Scope.JOBS_READ, Scope.ANALYTICS_READ, Scope.ALERTS_READ})


def analytics_filter(
    window_days: Annotated[int, Query(ge=1, le=1825)] = 30,
    country_code: Annotated[str | None, Query(min_length=2, max_length=2)] = None,
    city: Annotated[str | None, Query(max_length=96)] = None,
    company: Annotated[str | None, Query(max_length=128)] = None,
    segment: MarketSegment | None = None,
    seniority: ExperienceLevel | None = None,
    remote_type: RemoteType | None = None,
    skill: Annotated[str | None, Query(max_length=64)] = None,
    source: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 25,
) -> AnalyticsFilter:
    """Assemble the shared analytics filter from query parameters."""
    return AnalyticsFilter(
        window_days=window_days,
        country_code=country_code.upper() if country_code else None,
        city=city,
        company_slug=company,
        segment=segment,
        seniority=seniority,
        remote_type=remote_type,
        skill=skill,
        source=source,
        limit=limit,
    )


AnalyticsFilterDep = Annotated[AnalyticsFilter, Depends(analytics_filter)]
JobServiceDep = Annotated[JobService, Depends(get_job_service)]
AnalyticsServiceDep = Annotated[AnalyticsService, Depends(get_analytics_service)]
AlertServiceDep = Annotated[AlertService, Depends(get_alert_service)]
ProfileServiceDep = Annotated[ProfileService, Depends(get_profile_service)]
AdminServiceDep = Annotated[AdminService, Depends(get_admin_service)]
IngestionServiceDep = Annotated[IngestionService, Depends(get_ingestion_service)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
PrincipalDep = Annotated[Principal | None, Depends(get_principal)]


__all__ = [
    "AdminServiceDep",
    "AlertServiceDep",
    "AnalyticsFilterDep",
    "AnalyticsServiceDep",
    "IngestionServiceDep",
    "JobServiceDep",
    "PrincipalDep",
    "ProfileServiceDep",
    "SettingsDep",
    "analytics_filter",
    "get_principal",
    "require_scope",
]
