"""Integration tests for cache, events, scheduling, workers and search."""

from __future__ import annotations

import asyncio

import pytest

from app.core.config import CacheSettings, EventSettings, Settings
from app.core.timeutils import days_ago
from app.events.bus import (
    InMemoryEventBus,
    RedisStreamsEventBus,
    build_event_bus,
    job_event,
)
from app.ingestion.scheduler.scheduler import Scheduler
from app.models.enums import EventType, QualityDimension, TrendDirection
from app.models.events import PipelineEvent
from app.models.job import NormalizedJob
from app.search.engine import SearchRequest, SqlSearchBackend, build_search_backend, facets
from app.services.analytics_service import AnalyticsService
from app.storage.cache import InMemoryCache, NullCache, RedisCache, build_cache, build_key
from app.storage.repositories.jobs import JobRepository
from app.storage.repositories.quality import (
    MarketSnapshotRepository,
    QualityRepository,
    SkillTrendRepository,
)
from app.storage.repositories.reference import CompanyRepository, SkillRepository
from app.storage.session import Database
from app.workers.processor import EventProcessor

pytestmark = pytest.mark.integration


async def _store(database: Database, jobs: list[NormalizedJob]) -> None:
    async with database.session() as session:
        rows = await CompanyRepository(session).resolve_many([j.company_name for j in jobs])
        await JobRepository(session).upsert_many(
            jobs, company_ids={slug: row.id for slug, row in rows.items()}
        )
        await SkillRepository(session).bulk_replace_job_skills({j.id: j.skills for j in jobs})


class TestCacheBackends:
    async def test_null_cache_stores_nothing(self) -> None:
        cache = NullCache()
        await cache.set("k", 1)
        assert await cache.get("k") is None
        assert await cache.ping()
        await cache.aclose()

    async def test_entries_expire(self) -> None:
        cache = InMemoryCache(default_ttl_seconds=60)
        await cache.set("k", "v", ttl_seconds=-1)
        assert await cache.get("k") is None

    async def test_eviction_bounds_the_cache(self) -> None:
        cache = InMemoryCache(default_ttl_seconds=60, max_entries=10)
        for index in range(25):
            await cache.set(f"key-{index}", index)
        assert len(cache) <= 10

    async def test_build_cache_falls_back_to_memory(self) -> None:
        assert isinstance(build_cache(CacheSettings(url=None)), InMemoryCache)
        assert isinstance(build_cache(CacheSettings(enabled=False)), NullCache)

    async def test_redis_cache_round_trip(self) -> None:
        fakeredis = pytest.importorskip("fakeredis")
        client = fakeredis.aioredis.FakeRedis(decode_responses=True)
        cache = RedisCache(client, default_ttl_seconds=60)

        key = build_key("test", "family", "value")
        await cache.set(key, {"a": 1})
        assert await cache.get(key) == {"a": 1}
        assert await cache.incr("counter", ttl_seconds=30) == 1
        assert await cache.delete_prefix(build_key("test", "family")) >= 1
        assert await cache.ping()
        await cache.aclose()

    async def test_redis_failures_degrade_to_a_miss(self) -> None:
        class Broken:
            async def get(self, *_args: object, **_kwargs: object) -> None:
                raise RuntimeError("redis is down")

            async def set(self, *_args: object, **_kwargs: object) -> None:
                raise RuntimeError("redis is down")

            async def ping(self) -> bool:
                raise RuntimeError("redis is down")

        cache = RedisCache(Broken())
        await cache.set("k", 1)
        assert await cache.get("k") is None
        assert await cache.ping() is False


class TestEventBus:
    async def test_in_memory_publish_and_consume(self) -> None:
        bus = InMemoryEventBus()
        await bus.publish(job_event(EventType.JOB_CREATED, job_id="abc"))
        assert await bus.pending_count() == 1

        received: list[PipelineEvent] = []
        async for message_id, event in bus.consume(block_ms=100):
            received.append(event)
            await bus.ack(message_id)
            await bus.aclose()
        assert received[0].job_id == "abc"

    async def test_publish_many(self) -> None:
        bus = InMemoryEventBus()
        events = [job_event(EventType.JOB_CREATED, job_id=str(i)) for i in range(5)]
        assert await bus.publish_many(events) == 5
        assert len(bus.drain()) == 5

    async def test_closed_bus_refuses_publishing(self) -> None:
        from app.core.errors import EventBusError

        bus = InMemoryEventBus()
        await bus.aclose()
        with pytest.raises(EventBusError):
            await bus.publish(job_event(EventType.JOB_CREATED))

    def test_wire_format_round_trip(self) -> None:
        event = job_event(EventType.JOB_UPDATED, job_id="x", source="test", count=3)
        assert PipelineEvent.from_wire(event.to_wire()).payload["count"] == 3

    def test_build_event_bus_defaults_to_memory(self) -> None:
        assert isinstance(build_event_bus(EventSettings(backend="memory")), InMemoryEventBus)
        assert isinstance(build_event_bus(EventSettings(backend="redis")), InMemoryEventBus)

    async def test_redis_streams_round_trip(self) -> None:
        fakeredis = pytest.importorskip("fakeredis")
        client = fakeredis.aioredis.FakeRedis(decode_responses=True)
        bus = RedisStreamsEventBus(client, stream="test:events", group="testers")

        await bus.publish(job_event(EventType.JOB_CREATED, job_id="abc"))
        received: list[PipelineEvent] = []
        async for message_id, event in bus.consume(consumer="c1", block_ms=50):
            received.append(event)
            await bus.ack(message_id)
            break
        assert received[0].job_id == "abc"
        await bus.aclose()


class TestScheduler:
    async def test_runs_a_task_and_records_statistics(self) -> None:
        calls: list[int] = []

        async def task() -> None:
            calls.append(1)

        scheduler = Scheduler()
        scheduler.register("tick", task, interval_seconds=0.05)
        await scheduler.start()
        await asyncio.sleep(0.2)
        await scheduler.stop(grace_seconds=1)

        assert len(calls) >= 2
        snapshot = scheduler.snapshot()
        assert snapshot["tasks"][0]["runs"] >= 2
        assert snapshot["tasks"][0]["failures"] == 0

    async def test_a_failing_task_does_not_stop_the_loop(self) -> None:
        attempts: list[int] = []

        async def flaky() -> None:
            attempts.append(1)
            raise RuntimeError("boom")

        scheduler = Scheduler()
        task = scheduler.register("flaky", flaky, interval_seconds=0.05)
        await scheduler.start()
        await asyncio.sleep(0.2)
        await scheduler.stop(grace_seconds=1)

        assert len(attempts) >= 2
        assert task.failures >= 2
        assert task.last_error == "boom"

    async def test_run_once_bypasses_the_schedule(self) -> None:
        calls: list[int] = []

        async def task() -> str:
            calls.append(1)
            return "done"

        scheduler = Scheduler()
        scheduler.register("manual", task, interval_seconds=3600)
        assert await scheduler.run_once("manual") == "done"
        assert len(calls) == 1

        with pytest.raises(KeyError):
            await scheduler.run_once("missing")

    def test_intervals_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            Scheduler().register("bad", lambda: asyncio.sleep(0), interval_seconds=0)


class TestWorker:
    async def test_processes_events_and_invalidates_the_cache(
        self, database: Database, settings: Settings, cache: InMemoryCache
    ) -> None:
        analytics = AnalyticsService(database, cache=cache, settings=settings)
        bus = InMemoryEventBus()
        processor = EventProcessor(bus=bus, analytics=analytics, settings=settings)

        await cache.set(build_key(settings.cache.namespace, "analytics", "overview"), {"x": 1})
        await processor._handle(
            "mem-1", job_event(EventType.INGESTION_COMPLETED, source="synthetic")
        )

        assert processor.stats.handled == 1
        assert len(cache) == 0

    async def test_unknown_event_types_are_acknowledged(
        self, database: Database, settings: Settings
    ) -> None:
        bus = InMemoryEventBus()
        processor = EventProcessor(
            bus=bus, analytics=AnalyticsService(database, settings=settings), settings=settings
        )
        await processor._handle("mem-1", job_event(EventType.JOB_REJECTED))
        assert processor.stats.skipped == 1

    async def test_handler_failures_are_retried_then_dropped(
        self, database: Database, settings: Settings
    ) -> None:
        bus = InMemoryEventBus()
        processor = EventProcessor(
            bus=bus, analytics=AnalyticsService(database, settings=settings), settings=settings
        )

        async def explode(_event: PipelineEvent) -> None:
            raise RuntimeError("handler failed")

        processor.register(EventType.JOB_CREATED, explode)
        await processor._handle("mem-1", job_event(EventType.JOB_CREATED))
        assert processor.stats.failed == 1
        # The event was re-published for another attempt.
        assert any(event.attempt == 1 for event in bus.published)


class TestSearch:
    async def test_sql_backend_ranks_and_pages(
        self, database: Database, normalized_jobs: list[NormalizedJob]
    ) -> None:
        await _store(database, normalized_jobs)
        async with database.read_session() as session:
            backend = SqlSearchBackend(JobRepository(session))
            result = await backend.search(SearchRequest(query="python", limit=5))

        assert result.backend == "sql"
        assert result.took_ms >= 0
        assert len(result.jobs) <= 5
        if result.jobs:
            assert all(job.id in result.scores for job in result.jobs)

    async def test_backend_selection_matches_the_dialect(self, database: Database) -> None:
        async with database.read_session() as session:
            assert build_search_backend(JobRepository(session)).name == "sql"

    async def test_empty_query_returns_the_newest_postings(
        self, database: Database, normalized_jobs: list[NormalizedJob]
    ) -> None:
        await _store(database, normalized_jobs)
        async with database.read_session() as session:
            result = await SqlSearchBackend(JobRepository(session)).search(SearchRequest(limit=3))
        assert len(result.jobs) == 3
        assert result.scores == {}

    async def test_facets_cover_every_filter(
        self, database: Database, normalized_jobs: list[NormalizedJob]
    ) -> None:
        await _store(database, normalized_jobs)
        async with database.read_session() as session:
            values = await facets(JobRepository(session))
        assert {"remote_type", "employment_type", "segment", "skills"} <= set(values)

    async def test_suggestions_need_a_real_prefix(
        self, database: Database, normalized_jobs: list[NormalizedJob]
    ) -> None:
        await _store(database, normalized_jobs)
        async with database.read_session() as session:
            backend = SqlSearchBackend(JobRepository(session))
            assert await backend.suggest("a") == []
            assert isinstance(await backend.suggest("so"), list)

    def test_long_queries_are_truncated(self) -> None:
        request = SearchRequest(query="x" * 500)
        assert len(request.sanitized_query() or "") <= 200


class TestDerivedTables:
    async def test_skill_trends_are_upserted(self, database: Database) -> None:
        from app.core.timeutils import day_start, utcnow

        async with database.session() as session:
            await SkillRepository(session).sync_taxonomy(
                [{"slug": "python", "name": "Python", "category": "language", "parent": None}]
            )
            repository = SkillTrendRepository(session)
            day = day_start(utcnow())
            rows = [
                {
                    "skill_slug": "python",
                    "day": day,
                    "job_count": 10,
                    "share": 0.5,
                    "moving_average": 9.0,
                    "direction": str(TrendDirection.RISING),
                    "computed_at": utcnow(),
                }
            ]
            assert await repository.upsert_daily(rows) == 1
            rows[0]["job_count"] = 12
            assert await repository.upsert_daily(rows) == 1

            series = await repository.series("python", since=days_ago(2))
        assert series and series[0][1] == 12

    async def test_market_snapshots_are_idempotent(self, database: Database) -> None:
        from app.core.timeutils import utcnow

        async with database.session() as session:
            repository = MarketSnapshotRepository(session)
            await repository.upsert(utcnow(), {"active_jobs": 10})
            await repository.upsert(utcnow(), {"active_jobs": 20})
            latest = await repository.latest()
            value = latest.active_jobs if latest else None
        assert value == 20

    async def test_quality_events_can_be_listed(self, database: Database) -> None:
        from app.models.quality import QualityEvent

        async with database.session() as session:
            await QualityRepository(session).add_many(
                [
                    QualityEvent(
                        source="demo",
                        dimension=QualityDimension.FRESHNESS,
                        message="stale batch",
                    )
                ]
            )
        async with database.read_session() as session:
            repository = QualityRepository(session)
            recent = await repository.list_recent(limit=10)
            by_source = await repository.counts_by_source(since=days_ago(1))
            count = len(recent)
        assert count == 1
        assert by_source["demo"] == 1
