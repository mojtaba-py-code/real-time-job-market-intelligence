"""Administrative operations: credentials, taxonomy, maintenance and health."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.security import GeneratedApiKey
from app.core.timeutils import day_start, days_ago, utcnow
from app.models.analytics import AnalyticsFilter
from app.models.enums import Scope, UserRole
from app.nlp.skills.taxonomy import SkillTaxonomy, get_taxonomy
from app.storage.cache import CacheBackend, NullCache
from app.storage.parquet import AnalyticalStore, ParquetExporter
from app.storage.repositories.analytics import AnalyticsRepository
from app.storage.repositories.auth import ApiKeyRepository, Principal
from app.storage.repositories.ingestion import (
    IngestionRunRepository,
    RejectedRecordRepository,
    SourceStateRepository,
)
from app.storage.repositories.quality import MarketSnapshotRepository, QualityRepository
from app.storage.repositories.reference import CompanyRepository, SkillRepository
from app.storage.session import Database

log = get_logger(__name__)


@dataclass(slots=True)
class HealthReport:
    """Liveness and readiness of every dependency."""

    status: str
    database: bool
    cache: bool
    analytics_store: bool
    active_jobs: int
    last_ingestion_at: datetime | None
    version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "version": self.version,
            "checks": {
                "database": self.database,
                "cache": self.cache,
                "analytics_store": self.analytics_store,
            },
            "active_jobs": self.active_jobs,
            "last_ingestion_at": (
                self.last_ingestion_at.isoformat() if self.last_ingestion_at else None
            ),
        }


class AdminService:
    """Operations an operator performs, not an end user."""

    def __init__(
        self,
        database: Database,
        *,
        settings: Settings | None = None,
        cache: CacheBackend | None = None,
    ) -> None:
        self._db = database
        self._settings = settings or get_settings()
        self._cache = cache if cache is not None else NullCache()

    # ------------------------------------------------------------------ #
    # Credentials
    # ------------------------------------------------------------------ #
    async def issue_api_key(
        self,
        *,
        name: str,
        role: UserRole = UserRole.VIEWER,
        scopes: set[Scope] | None = None,
        expires_at: datetime | None = None,
    ) -> GeneratedApiKey:
        """Mint a key. The plaintext is returned once and never stored."""
        async with self._db.session() as session:
            generated, _row = await ApiKeyRepository(session).create(
                name=name, role=role, scopes=scopes, expires_at=expires_at
            )
        log.info("admin.api_key_issued", name=name, role=str(role), key_id=generated.key_id)
        return generated

    async def revoke_api_key(self, key_id: str) -> bool:
        async with self._db.session() as session:
            revoked = await ApiKeyRepository(session).revoke(key_id)
        if revoked:
            log.info("admin.api_key_revoked", key_id=key_id)
        return revoked

    async def list_api_keys(self, *, include_inactive: bool = False) -> list[dict[str, Any]]:
        # Read every attribute inside the session: the ORM rows detach when it
        # closes, and touching them afterwards raises.
        async with self._db.read_session() as session:
            rows = await ApiKeyRepository(session).list_keys(include_inactive=include_inactive)
            return [
                {
                    "key_id": row.key_id,
                    "name": row.name,
                    "role": row.role,
                    "scopes": list(row.scopes or []),
                    "active": row.active,
                    "created_at": row.created_at.isoformat(),
                    "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
                    "expires_at": row.expires_at.isoformat() if row.expires_at else None,
                }
                for row in rows
            ]

    async def authenticate(self, key_id: str, presented: str) -> Principal | None:
        """Verify a presented API key."""
        async with self._db.session() as session:
            return await ApiKeyRepository(session).authenticate(key_id, presented)

    async def bootstrap_admin_key(self) -> GeneratedApiKey | None:
        """Create the first admin key when the platform has none."""
        async with self._db.read_session() as session:
            existing = await ApiKeyRepository(session).count_active()
        if existing:
            return None
        return await self.issue_api_key(name="bootstrap-admin", role=UserRole.ADMIN)

    # ------------------------------------------------------------------ #
    # Taxonomy
    # ------------------------------------------------------------------ #
    async def sync_taxonomy(self, taxonomy: SkillTaxonomy | None = None) -> int:
        """Write the configured taxonomy into the database."""
        resolved = taxonomy or get_taxonomy(str(self._settings.nlp.taxonomy_path))
        async with self._db.session() as session:
            written = await SkillRepository(session).sync_taxonomy(resolved.to_rows())
        log.info("admin.taxonomy_synced", nodes=written, version=resolved.version)
        return written

    # ------------------------------------------------------------------ #
    # Maintenance
    # ------------------------------------------------------------------ #
    async def refresh_company_counts(self) -> int:
        async with self._db.session() as session:
            return await CompanyRepository(session).refresh_job_counts()

    async def prune(self, *, keep_days: int = 90) -> dict[str, int]:
        """Drop old run metrics and quarantined records."""
        cutoff = days_ago(keep_days)
        async with self._db.session() as session:
            runs = await IngestionRunRepository(session).prune(older_than=cutoff)
            rejected = await RejectedRecordRepository(session).prune(older_than=cutoff)
        return {"ingestion_runs": runs, "rejected_records": rejected}

    async def snapshot_market(self) -> dict[str, Any]:
        """Freeze today's headline metrics for historical comparison."""
        from app.analytics.market.overview import MarketAnalytics

        flt = AnalyticsFilter(window_days=30, limit=10)
        async with self._db.session() as session:
            repo = AnalyticsRepository(session)
            overview = await MarketAnalytics(repo, settings=self._settings.analytics).overview(flt)
            payload: dict[str, object] = {
                "active_jobs": overview.active_jobs,
                "new_jobs": overview.new_jobs_today,
                "expired_jobs": 0,
                "companies_hiring": overview.companies_hiring,
                "remote_share": overview.remote_share,
                "average_salary": overview.average_salary,
                "median_salary": overview.median_salary,
                "data_quality_score": overview.data_quality_score,
                "payload": {
                    "top_skill": overview.top_skill.model_dump(mode="json")
                    if overview.top_skill
                    else None,
                    "window_days": overview.window_days,
                },
            }
            await MarketSnapshotRepository(session).upsert(day_start(utcnow()), payload)
        return payload

    async def export_analytics(self, *, limit: int = 5000) -> int:
        """Refresh the columnar store from the operational database."""
        from app.storage.repositories.jobs import JobQuery, JobRepository

        exporter = ParquetExporter(self._settings.storage)
        async with self._db.read_session() as session:
            jobs, _total = await JobRepository(session).search(
                JobQuery(sort="first_seen_at", descending=True, limit=limit)
            )
        if not jobs:
            return 0
        return exporter.export(jobs).rows

    # ------------------------------------------------------------------ #
    # Observability
    # ------------------------------------------------------------------ #
    async def health(self) -> HealthReport:
        """Check every dependency without raising."""
        from app import __version__

        database_ok = await self._db.healthcheck()
        cache_ok = await self._cache.ping()
        store = AnalyticalStore(self._settings.storage)

        active_jobs = 0
        last_ingestion: datetime | None = None
        if database_ok:
            try:
                async with self._db.read_session() as session:
                    active_jobs = await AnalyticsRepository(session).active_job_count()
                    recent = await IngestionRunRepository(session).list_recent(limit=1)
                    last_ingestion = recent[0].started_at if recent else None
            except Exception as exc:
                log.warning("admin.health_partial", error=str(exc))
                database_ok = False

        status = "healthy" if database_ok else "degraded"
        return HealthReport(
            status=status,
            database=database_ok,
            cache=cache_ok,
            analytics_store=store.available(),
            active_jobs=active_jobs,
            last_ingestion_at=last_ingestion,
            version=__version__,
        )

    async def statistics(self) -> dict[str, Any]:
        """Operational counters for the admin dashboard."""
        since = days_ago(7)
        async with self._db.read_session() as session:
            runs = IngestionRunRepository(session)
            analytics = AnalyticsRepository(session)
            quality = QualityRepository(session)
            rejected = RejectedRecordRepository(session)
            sources = SourceStateRepository(session)

            return {
                "ingestion": await runs.summary(since=since),
                "sources": [
                    {
                        "source": state.source,
                        "enabled": state.enabled,
                        "last_successful_run_at": (
                            state.last_successful_run_at.isoformat()
                            if state.last_successful_run_at
                            else None
                        ),
                        "records_processed": state.records_processed,
                        "records_failed": state.records_failed,
                        "consecutive_failures": state.consecutive_failures,
                    }
                    for state in await sources.list_all()
                ],
                "jobs_by_source": await analytics.source_counts(),
                "quality_events": {
                    str(dimension): count
                    for dimension, count in (await quality.counts_by_dimension(since=since)).items()
                },
                "rejections": await rejected.count_by_reason(since=since),
                "freshness_hours": await analytics.freshness_hours(),
                "analytics_store": AnalyticalStore(self._settings.storage).describe(),
            }


__all__ = ["AdminService", "HealthReport"]
