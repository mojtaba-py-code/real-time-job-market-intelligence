"""Administrative endpoints.

Everything here requires the ``admin`` scope. Nothing here is reachable
anonymously, in any environment.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, status

from app.api.deps import (
    AdminServiceDep,
    AnalyticsServiceDep,
    IngestionServiceDep,
    require_scope,
)
from app.api.schemas import ApiKeyCreated, ApiKeyInput, IngestionTriggerResponse
from app.core.errors import NotFoundError
from app.models.enums import ROLE_SCOPES, Scope

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[require_scope(Scope.ADMIN)])

KeyId = Annotated[str, Path(min_length=8, max_length=32, pattern=r"^[0-9a-f]+$")]
SourceName = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[a-z0-9_\-]+$")]


@router.get("/stats", summary="Operational statistics")
async def stats(admin: AdminServiceDep) -> dict[str, Any]:
    """Ingestion throughput, source health, quality events and rejections."""
    return await admin.statistics()


@router.post(
    "/keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Issue an API key",
)
async def create_key(admin: AdminServiceDep, payload: ApiKeyInput) -> ApiKeyCreated:
    """Mint a key. The plaintext is returned exactly once."""
    scopes = set(payload.scopes) if payload.scopes else set(ROLE_SCOPES[payload.role])
    generated = await admin.issue_api_key(
        name=payload.name, role=payload.role, scopes=scopes, expires_at=payload.expires_at
    )
    return ApiKeyCreated(
        key_id=generated.key_id,
        api_key=generated.full_key,
        name=payload.name,
        role=payload.role,
        scopes=sorted(str(scope) for scope in scopes),
    )


@router.get("/keys", summary="List API keys")
async def list_keys(admin: AdminServiceDep, include_inactive: bool = False) -> list[dict[str, Any]]:
    """Metadata only - the key material is never readable."""
    return await admin.list_api_keys(include_inactive=include_inactive)


@router.delete("/keys/{key_id}", summary="Revoke an API key")
async def revoke_key(admin: AdminServiceDep, key_id: KeyId) -> dict[str, bool]:
    if not await admin.revoke_api_key(key_id):
        raise NotFoundError(f"API key {key_id} was not found")
    return {"revoked": True}


@router.post(
    "/ingest/{source}",
    response_model=IngestionTriggerResponse,
    summary="Trigger ingestion for one source",
)
async def trigger_ingestion(
    ingestion: IngestionServiceDep,
    analytics: AnalyticsServiceDep,
    source: SourceName,
    limit: Annotated[int | None, Query(ge=1, le=10_000)] = None,
) -> IngestionTriggerResponse:
    """Run a source immediately, then invalidate the analytics cache."""
    outcome = await ingestion.run_source(source, limit=limit)
    await analytics.invalidate()
    run = outcome.run
    return IngestionTriggerResponse(
        source=run.source,
        status=str(run.status),
        records_received=run.records_received,
        jobs_created=run.jobs_created,
        jobs_updated=run.jobs_updated,
        duplicates_detected=run.duplicates_detected,
        records_rejected=run.records_rejected,
        quality_score=round(run.quality_score, 4),
        duration_seconds=round(run.duration_seconds, 3),
        error=outcome.error,
    )


@router.post("/expire", summary="Expire stale postings")
async def expire(ingestion: IngestionServiceDep) -> dict[str, int]:
    """Retire postings no source has re-listed within the expiry window."""
    return {"expired": await ingestion.expire_stale()}


@router.post("/taxonomy/sync", summary="Sync the skill taxonomy into the database")
async def sync_taxonomy(admin: AdminServiceDep) -> dict[str, int]:
    return {"nodes": await admin.sync_taxonomy()}


@router.post("/analytics/export", summary="Refresh the columnar analytical store")
async def export_analytics(
    admin: AdminServiceDep,
    limit: Annotated[int, Query(ge=1, le=100_000)] = 5000,
) -> dict[str, int]:
    return {"rows": await admin.export_analytics(limit=limit)}


@router.post("/analytics/snapshot", summary="Freeze today's market snapshot")
async def snapshot(admin: AdminServiceDep) -> dict[str, Any]:
    return await admin.snapshot_market()


@router.post("/cache/invalidate", summary="Drop cached analytics")
async def invalidate_cache(analytics: AnalyticsServiceDep) -> dict[str, int]:
    return {"entries": await analytics.invalidate()}


@router.post("/maintenance/prune", summary="Prune old runs and quarantined records")
async def prune(
    admin: AdminServiceDep,
    keep_days: Annotated[int, Query(ge=7, le=3650)] = 90,
) -> dict[str, int]:
    return await admin.prune(keep_days=keep_days)


__all__ = ["router"]
