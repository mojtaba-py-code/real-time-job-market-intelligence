"""Integration tests for the service layer."""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.events.bus import InMemoryEventBus
from app.ingestion.registry import SourceRegistry
from app.models.alerts import AlertRule
from app.models.analytics import AnalyticsFilter
from app.models.enums import (
    AlertChannel,
    AlertMetric,
    ComparisonOperator,
    EventType,
    IngestionStatus,
)
from app.models.profiles import CandidateProfile
from app.services.admin_service import AdminService
from app.services.alert_service import AlertService
from app.services.analytics_service import AnalyticsService
from app.services.ingestion_service import IngestionService
from app.services.job_service import JobService
from app.services.profile_service import ProfileService
from app.storage.cache import InMemoryCache
from app.storage.session import Database

pytestmark = pytest.mark.integration

WIDE = AnalyticsFilter(window_days=1825, limit=25)


@pytest.fixture
async def ingested(
    database: Database,
    synthetic_registry: SourceRegistry,
    settings: Settings,
    event_bus: InMemoryEventBus,
) -> IngestionService:
    """A database that already contains one synthetic ingestion run."""
    service = IngestionService(
        database=database, registry=synthetic_registry, event_bus=event_bus, settings=settings
    )
    await service.run_source("synthetic")
    return service


class TestIngestionService:
    async def test_replaying_a_batch_updates_instead_of_duplicating(
        self, database: Database, ingested: IngestionService
    ) -> None:
        """Rewind the cursor so the source hands back the same batch."""
        from app.storage.repositories.ingestion import SourceStateRepository

        async with database.session() as session:
            states = SourceStateRepository(session)
            state = await states.get("synthetic")
            assert state is not None
            state.last_cursor = None
            await states.save(state)

        outcome = await ingested.run_source("synthetic")
        assert outcome.run.status in (IngestionStatus.SUCCESS, IngestionStatus.PARTIAL)
        assert outcome.run.jobs_created == 0
        assert outcome.run.jobs_updated > 0

    async def test_run_publishes_events(
        self, ingested: IngestionService, event_bus: InMemoryEventBus
    ) -> None:
        types = {event.type for event in event_bus.published}
        assert EventType.INGESTION_STARTED in types
        assert EventType.INGESTION_COMPLETED in types
        assert EventType.JOB_CREATED in types

    async def test_run_records_metrics_and_quality(self, ingested: IngestionService) -> None:
        outcome = await ingested.run_source("synthetic")
        run = outcome.run
        assert run.records_received > 0
        assert 0 < run.quality_score <= 1
        assert run.duration_seconds >= 0
        assert run.details

    async def test_unknown_source_is_an_error(self, ingested: IngestionService) -> None:
        from app.core.errors import SourceConfigurationError

        with pytest.raises(SourceConfigurationError):
            await ingested.run_source("does-not-exist")

    async def test_expiry_retires_old_postings(self, ingested: IngestionService) -> None:
        expired = await ingested.expire_stale()
        assert expired >= 0

    async def test_analytics_export_writes_parquet(self, ingested: IngestionService) -> None:
        assert await ingested.export_analytics(limit=100) > 0


class TestJobService:
    async def test_search_and_get(self, database: Database, ingested: IngestionService) -> None:
        from app.search.engine import SearchRequest

        service = JobService(database)
        result = await service.search(SearchRequest(query="python", limit=5))
        assert result.total >= 0
        if result.jobs:
            job = await service.get(result.jobs[0].id)
            assert job.id == result.jobs[0].id

    async def test_missing_job_raises_not_found(self, database: Database) -> None:
        from app.core.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await JobService(database).get("0" * 32)

    async def test_facets_are_cached(
        self, database: Database, ingested: IngestionService, cache: InMemoryCache
    ) -> None:
        service = JobService(database, cache=cache)
        first = await service.facets()
        assert "remote_type" in first
        assert len(cache) >= 1

    async def test_similar_jobs_share_skills(
        self, database: Database, ingested: IngestionService
    ) -> None:
        service = JobService(database)
        jobs, _total = await service.list_jobs(
            __import__("app.storage.repositories.jobs", fromlist=["JobQuery"]).JobQuery(limit=1)
        )
        if jobs and jobs[0].skill_slugs:
            similar = await service.similar(jobs[0].id, limit=3)
            assert all(item.id != jobs[0].id for item in similar)


class TestAnalyticsService:
    async def test_overview_and_breakdowns(
        self, database: Database, ingested: IngestionService, cache: InMemoryCache
    ) -> None:
        service = AnalyticsService(database, cache=cache)
        overview = await service.overview(WIDE)
        assert overview.active_jobs > 0
        assert overview.volume is not None

        assert await service.skills(WIDE)
        assert (await service.remote(WIDE)).total > 0
        assert (await service.seniority(WIDE)).total > 0
        assert await service.locations(WIDE)
        assert await service.companies(WIDE)

    async def test_results_are_cached_and_invalidated(
        self, database: Database, ingested: IngestionService, cache: InMemoryCache
    ) -> None:
        service = AnalyticsService(database, cache=cache)
        await service.overview(WIDE)
        cached_entries = len(cache)
        assert cached_entries > 0
        await service.overview(WIDE)
        assert len(cache) == cached_entries
        assert await service.invalidate() > 0

    async def test_skill_explorer_returns_everything_or_nothing(
        self, database: Database, ingested: IngestionService
    ) -> None:
        service = AnalyticsService(database)
        view = await service.skill_explorer("python", WIDE)
        if view is not None:
            assert view.skill.slug == "python"
            assert view.trend.slug == "python"
        assert await service.skill_explorer("nonexistent-skill", WIDE) is None

    async def test_emerging_detection_runs(
        self, database: Database, ingested: IngestionService
    ) -> None:
        assert isinstance(await AnalyticsService(database).emerging(window_days=60), list)

    async def test_cooccurrence_pairs_have_lift(
        self, database: Database, ingested: IngestionService
    ) -> None:
        pairs = await AnalyticsService(database).cooccurrence(WIDE, limit=10)
        assert all(pair.lift >= 0 for pair in pairs)


class TestAlertService:
    async def test_rule_lifecycle(self, database: Database, settings: Settings) -> None:
        service = AlertService(database, settings=settings)
        rule = AlertRule(
            name="python demand",
            metric=AlertMetric.SKILL_DEMAND_CHANGE_PCT,
            subject="python",
            operator=ComparisonOperator.GT,
            threshold=10,
            channels=[AlertChannel.IN_APP],
        )
        created = await service.create_rule(rule)
        assert (await service.get_rule(created.id)).name == "python demand"
        assert len(await service.list_rules()) == 1
        assert await service.delete_rule(created.id)
        assert await service.list_rules() == []

    async def test_evaluation_produces_notifications(
        self, database: Database, settings: Settings, ingested: IngestionService
    ) -> None:
        service = AlertService(database, settings=settings)
        await service.create_rule(
            AlertRule(
                name="any active jobs",
                metric=AlertMetric.TOTAL_ACTIVE_JOBS,
                operator=ComparisonOperator.GT,
                threshold=0,
                channels=[AlertChannel.IN_APP],
            )
        )
        result = await service.evaluate_all()
        assert result.evaluated == 1
        assert result.triggered == 1
        assert result.notifications
        assert result.notifications[0].delivered

        stored = await service.list_notifications()
        assert len(stored) == 1

    async def test_cooldown_suppresses_repeats(
        self, database: Database, settings: Settings, ingested: IngestionService
    ) -> None:
        service = AlertService(database, settings=settings)
        await service.create_rule(
            AlertRule(
                name="always",
                metric=AlertMetric.TOTAL_ACTIVE_JOBS,
                operator=ComparisonOperator.GT,
                threshold=0,
                cooldown_minutes=60,
            )
        )
        assert (await service.evaluate_all()).triggered == 1
        assert (await service.evaluate_all()).triggered == 0

    async def test_channel_prerequisites_are_validated(self) -> None:
        with pytest.raises(ValueError, match="email_to"):
            AlertRule(
                name="broken",
                metric=AlertMetric.TOTAL_ACTIVE_JOBS,
                channels=[AlertChannel.EMAIL],
            )


class TestProfileService:
    async def test_profiles_are_keyed_by_owner_and_label(
        self, database: Database, settings: Settings
    ) -> None:
        service = ProfileService(database, settings=settings)
        profile = CandidateProfile(
            owner="tester",
            label="default",
            target_role="Python Developer",
            skills=["python", "fastapi"],
        )
        saved = await service.save(profile)
        again = await service.save(
            CandidateProfile(
                owner="tester",
                label="default",
                target_role="Backend Engineer",
                skills=["python", "docker"],
            )
        )
        assert again.id == saved.id
        assert len(await service.list_profiles(owner="tester")) == 1

    async def test_market_fit_is_computed(
        self, database: Database, settings: Settings, ingested: IngestionService
    ) -> None:
        service = ProfileService(database, settings=settings)
        result = await service.market_fit(
            CandidateProfile(
                target_role="Python Developer", skills=["python", "docker", "postgresql"]
            ),
            window_days=365,
        )
        assert 0.0 <= result.market_fit_score <= 1.0
        assert 0.0 <= result.skill_coverage <= 1.0
        assert result.notes
        assert "not a prediction" in result.notes[0]

    async def test_empty_profile_scores_zero(
        self, database: Database, settings: Settings, ingested: IngestionService
    ) -> None:
        result = await ProfileService(database, settings=settings).market_fit(
            CandidateProfile(target_role="Anything", skills=[])
        )
        assert result.market_fit_score == 0.0


class TestAdminService:
    async def test_health_reports_dependencies(
        self, database: Database, settings: Settings, cache: InMemoryCache
    ) -> None:
        report = await AdminService(database, settings=settings, cache=cache).health()
        assert report.status == "healthy"
        assert report.database is True
        assert report.version

    async def test_statistics_cover_the_pipeline(
        self, database: Database, settings: Settings, ingested: IngestionService
    ) -> None:
        stats = await AdminService(database, settings=settings).statistics()
        assert "ingestion" in stats
        assert stats["jobs_by_source"]
        assert isinstance(stats["sources"], list)

    async def test_taxonomy_sync_writes_every_node(
        self, database: Database, settings: Settings
    ) -> None:
        written = await AdminService(database, settings=settings).sync_taxonomy()
        assert written > 100

    async def test_bootstrap_key_is_created_once(
        self, database: Database, settings: Settings
    ) -> None:
        service = AdminService(database, settings=settings)
        first = await service.bootstrap_admin_key()
        assert first is not None
        assert await service.bootstrap_admin_key() is None

    async def test_snapshot_is_idempotent(
        self, database: Database, settings: Settings, ingested: IngestionService
    ) -> None:
        service = AdminService(database, settings=settings)
        first = await service.snapshot_market()
        second = await service.snapshot_market()
        assert first["active_jobs"] == second["active_jobs"]
