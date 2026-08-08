"""End-to-end tests.

    source -> ingestion -> processing -> database -> analytics -> API

One test walks the whole path and asserts that what came out of the generator
is what the API eventually reports.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.core.config import Settings
from app.events.bus import InMemoryEventBus
from app.ingestion.registry import SourceDefinition, SourceRegistry
from app.models.analytics import AnalyticsFilter
from app.models.enums import EventType, IngestionStatus, SourceKind
from app.services.admin_service import AdminService
from app.services.analytics_service import AnalyticsService
from app.services.ingestion_service import IngestionService
from app.storage.parquet import AnalyticalStore
from app.storage.session import Database
from app.workers.processor import EventProcessor

pytestmark = pytest.mark.e2e

WIDE = AnalyticsFilter(window_days=1825, limit=25)


@pytest.fixture
def trending_registry(settings: Settings) -> SourceRegistry:
    """A larger synthetic corpus so the trend engine has something to chew on."""
    return SourceRegistry(
        [
            SourceDefinition(
                name="synthetic",
                kind=SourceKind.SYNTHETIC,
                options={"count": 400, "days": 120, "seed": 20240101},
                batch_size=400,
            )
        ],
        settings=settings,
    )


async def test_full_pipeline_from_source_to_api(
    database: Database,
    settings: Settings,
    trending_registry: SourceRegistry,
    event_bus: InMemoryEventBus,
) -> None:
    # --- ingestion -------------------------------------------------------- #
    ingestion = IngestionService(
        database=database, registry=trending_registry, event_bus=event_bus, settings=settings
    )
    outcome = await ingestion.run_source("synthetic")

    assert outcome.run.status in (IngestionStatus.SUCCESS, IngestionStatus.PARTIAL)
    assert outcome.run.records_received == 400
    assert outcome.run.jobs_created > 300
    assert outcome.run.duplicates_detected > 0, "the generator republishes some postings"
    assert 0 < outcome.run.quality_score <= 1

    processing = outcome.processing
    assert processing is not None
    assert processing.quality is not None
    assert len(processing.quality.dimensions) == 6

    # --- the event bus saw the whole run ---------------------------------- #
    published = {event.type for event in event_bus.published}
    assert {
        EventType.INGESTION_STARTED,
        EventType.JOB_CREATED,
        EventType.INGESTION_COMPLETED,
    } <= published

    # --- analytics -------------------------------------------------------- #
    admin = AdminService(database, settings=settings)
    await admin.sync_taxonomy()
    analytics = AnalyticsService(database, settings=settings)

    overview = await analytics.overview(WIDE)
    assert overview.active_jobs > 300
    assert overview.companies_hiring > 5
    assert overview.countries_covered > 5
    assert overview.top_skill is not None
    assert overview.volume is not None and len(overview.volume.points) > 30

    skills = await analytics.skills(WIDE)
    slugs = {skill.slug for skill in skills}
    assert {"python", "docker"} & slugs, "the generator always emits these"

    remote = await analytics.remote(WIDE)
    assert remote.total > 0
    assert 0 < remote.remote_share < 1

    seniority = await analytics.seniority(WIDE)
    assert seniority.total > 0

    # --- known trends are recovered --------------------------------------- #
    python_trend = await analytics.skill_trend("python")
    assert python_trend.sample_size > 0
    assert python_trend.windows

    # --- columnar export --------------------------------------------------- #
    exported = await admin.export_analytics(limit=1000)
    assert exported > 0
    store = AnalyticalStore(settings.storage)
    assert store.available()
    assert store.describe()["rows"] == exported
    assert store.skill_demand(limit=5)

    # --- worker reacts to the run ------------------------------------------ #
    processor = EventProcessor(bus=event_bus, analytics=analytics, admin=admin, settings=settings)
    completed = next(
        event for event in event_bus.published if event.type is EventType.INGESTION_COMPLETED
    )
    await processor._handle("mem-test", completed)
    assert processor.stats.handled == 1

    # --- API ---------------------------------------------------------------- #
    with TestClient(create_app(settings, database=database)) as client:
        assert client.get("/health").json()["status"] == "healthy"

        listing = client.get("/jobs", params={"limit": 5}).json()
        assert listing["meta"]["total"] > 300

        search = client.get("/jobs/search", params={"q": "python", "limit": 5}).json()
        assert search["meta"]["total"] > 0
        assert all(item["title"] for item in search["items"])

        market = client.get("/analytics/market", params={"window_days": 1825}).json()
        assert market["active_jobs"] == overview.active_jobs

        trend = client.get("/skills/python/trend").json()
        assert trend["slug"] == "python"


async def test_pipeline_is_idempotent_end_to_end(
    database: Database, settings: Settings, trending_registry: SourceRegistry
) -> None:
    """Replaying the same batch must not inflate any metric."""
    from app.storage.repositories.ingestion import SourceStateRepository

    ingestion = IngestionService(database=database, registry=trending_registry, settings=settings)
    first = await ingestion.run_source("synthetic")

    async with database.session() as session:
        states = SourceStateRepository(session)
        state = await states.get("synthetic")
        assert state is not None
        state.last_cursor = None
        await states.save(state)

    second = await ingestion.run_source("synthetic")

    assert second.run.jobs_created == 0
    assert second.run.jobs_updated > 0

    analytics = AnalyticsService(database, settings=settings)
    overview = await analytics.overview(WIDE)
    assert overview.active_jobs == first.run.jobs_created


async def test_rejected_records_are_quarantined_not_lost(
    database: Database, settings: Settings, trending_registry: SourceRegistry
) -> None:
    """The generator emits deliberately broken records; none may disappear."""
    from app.storage.repositories.ingestion import RejectedRecordRepository

    ingestion = IngestionService(database=database, registry=trending_registry, settings=settings)
    outcome = await ingestion.run_source("synthetic")
    assert outcome.run.records_rejected > 0

    async with database.read_session() as session:
        stored = await RejectedRecordRepository(session).list_recent(limit=500)
        count = len(stored)
        reasons = {row.reason for row in stored}

    assert count == outcome.run.records_rejected
    assert reasons
