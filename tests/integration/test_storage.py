"""Integration tests for the storage layer."""

from __future__ import annotations

import pytest

from app.core.timeutils import days_ago, utcnow
from app.models.analytics import AnalyticsFilter
from app.models.enums import DuplicateKind, JobStatus, Scope, SourceKind, UserRole
from app.models.ingestion import IngestionRun, SourceState
from app.models.job import NormalizedJob
from app.models.quality import QualityEvent
from app.models.raw import RejectedRecord
from app.storage.cache import InMemoryCache, build_key, cached
from app.storage.parquet import AnalyticalStore, ParquetExporter
from app.storage.repositories.analytics import AnalyticsRepository
from app.storage.repositories.auth import ApiKeyRepository
from app.storage.repositories.ingestion import (
    IngestionRunRepository,
    RejectedRecordRepository,
    SourceStateRepository,
)
from app.storage.repositories.jobs import JobQuery, JobRepository
from app.storage.repositories.quality import QualityRepository
from app.storage.repositories.reference import CompanyRepository, SkillRepository

pytestmark = pytest.mark.integration


async def _store(database: object, jobs: list[NormalizedJob]) -> None:
    """Persist a batch the way the ingestion service does."""
    async with database.session() as session:  # type: ignore[attr-defined]
        companies = CompanyRepository(session)
        repository = JobRepository(session)
        skills = SkillRepository(session)

        rows = await companies.resolve_many([job.company_name for job in jobs])
        await repository.upsert_many(jobs, company_ids={slug: row.id for slug, row in rows.items()})
        await skills.bulk_replace_job_skills({job.id: job.skills for job in jobs})
        await repository.record_salaries(jobs)


class TestJobRepository:
    async def test_upsert_is_idempotent(
        self, database: object, normalized_jobs: list[NormalizedJob]
    ) -> None:
        await _store(database, normalized_jobs)
        async with database.session() as session:  # type: ignore[attr-defined]
            repository = JobRepository(session)
            first_total = await repository.count(JobQuery(limit=1))
            result = await repository.upsert_many(normalized_jobs)
            assert result.created == []
            assert len(result.updated) == len(normalized_jobs)
            assert await repository.count(JobQuery(limit=1)) == first_total

    async def test_natural_key_lookup(
        self, database: object, normalized_jobs: list[NormalizedJob]
    ) -> None:
        await _store(database, normalized_jobs)
        sample = normalized_jobs[0]
        async with database.read_session() as session:  # type: ignore[attr-defined]
            found = await JobRepository(session).find_by_natural_keys(
                [(sample.source, sample.source_job_id)]
            )
        assert (sample.source, sample.source_job_id) in found

    async def test_search_filters_and_pages(
        self, database: object, normalized_jobs: list[NormalizedJob]
    ) -> None:
        await _store(database, normalized_jobs)
        async with database.read_session() as session:  # type: ignore[attr-defined]
            repository = JobRepository(session)
            page_one, total = await repository.search(JobQuery(limit=5, offset=0))
            page_two, _ = await repository.search(JobQuery(limit=5, offset=5))

        assert total >= 10
        assert len(page_one) == 5
        assert {job.id for job in page_one}.isdisjoint({job.id for job in page_two})

    async def test_skill_filter_requires_all_skills(
        self, database: object, normalized_jobs: list[NormalizedJob]
    ) -> None:
        await _store(database, normalized_jobs)
        async with database.read_session() as session:  # type: ignore[attr-defined]
            jobs, _total = await JobRepository(session).search(
                JobQuery(skills=("python", "docker"), require_all_skills=True, limit=50)
            )
        for job in jobs:
            assert {"python", "docker"} <= set(job.skill_slugs)

    async def test_expire_stale_and_reactivate(
        self, database: object, normalized_jobs: list[NormalizedJob]
    ) -> None:
        await _store(database, normalized_jobs)
        async with database.session() as session:  # type: ignore[attr-defined]
            repository = JobRepository(session)
            expired = await repository.expire_stale(cutoff=utcnow())
            assert expired > 0
            active = await repository.count(JobQuery(limit=1))
            assert active == 0

            revived = await repository.reactivate([normalized_jobs[0].id])
            assert revived == 1

    async def test_mark_duplicate(
        self, database: object, normalized_jobs: list[NormalizedJob]
    ) -> None:
        await _store(database, normalized_jobs)
        first, second = normalized_jobs[0], normalized_jobs[1]
        async with database.session() as session:  # type: ignore[attr-defined]
            repository = JobRepository(session)
            await repository.mark_duplicate(
                second.id, duplicate_of=first.id, kind=DuplicateKind.CONTENT
            )
            stored = await repository.get(second.id)
        assert stored is not None
        assert stored.status is JobStatus.DUPLICATE
        assert stored.duplicate_of == first.id

    async def test_recent_signatures_feed_the_deduplicator(
        self, database: object, normalized_jobs: list[NormalizedJob]
    ) -> None:
        await _store(database, normalized_jobs)
        async with database.read_session() as session:  # type: ignore[attr-defined]
            rows = await JobRepository(session).recent_signatures(since=days_ago(365), limit=10)
        assert rows
        assert all(len(signature) > 0 for _id, _hash, signature in rows)

    async def test_salary_records_only_hold_observed_values(
        self, database: object, normalized_jobs: list[NormalizedJob]
    ) -> None:
        await _store(database, normalized_jobs)
        flt = AnalyticsFilter(window_days=1825, limit=10)
        async with database.read_session() as session:  # type: ignore[attr-defined]
            samples = await AnalyticsRepository(session).salary_samples(flt)
        disclosed = sum(1 for job in normalized_jobs if job.salary.is_observed)
        assert len(samples) <= disclosed
        assert all(value > 0 for value in samples)


class TestAnalyticsRepository:
    async def test_aggregations(
        self, database: object, normalized_jobs: list[NormalizedJob]
    ) -> None:
        await _store(database, normalized_jobs)
        flt = AnalyticsFilter(window_days=1825, limit=20)
        async with database.read_session() as session:  # type: ignore[attr-defined]
            repository = AnalyticsRepository(session)
            assert await repository.active_job_count() > 0
            assert await repository.total_jobs(flt) > 0
            assert await repository.daily_volume(flt)
            assert await repository.skill_counts(flt)
            assert await repository.remote_counts(flt)
            assert await repository.seniority_counts(flt)
            assert await repository.country_counts(flt)
            assert await repository.company_counts(flt)
            assert await repository.distinct_company_count(flt) > 0
            assert 0 <= await repository.average_quality_score(flt) <= 1

    async def test_skill_pairs_are_unordered_and_deduplicated(
        self, database: object, normalized_jobs: list[NormalizedJob]
    ) -> None:
        await _store(database, normalized_jobs)
        flt = AnalyticsFilter(window_days=1825, limit=50)
        async with database.read_session() as session:  # type: ignore[attr-defined]
            pairs = await AnalyticsRepository(session).skill_pairs(flt, min_count=1, limit=50)
        for left, right, count in pairs:
            assert left < right
            assert count > 0


class TestIngestionRepositories:
    async def test_source_state_round_trip(self, database: object) -> None:
        async with database.session() as session:  # type: ignore[attr-defined]
            repository = SourceStateRepository(session)
            state = await repository.get_or_create("demo", SourceKind.SYNTHETIC)
            state.advance(cursor="42", timestamp=utcnow(), processed=10, failed=1)
            await repository.save(state)

        async with database.read_session() as session:  # type: ignore[attr-defined]
            stored = await SourceStateRepository(session).get("demo")
        assert stored is not None
        assert stored.last_cursor == "42"
        assert stored.records_processed == 10
        assert stored.last_timestamp is not None
        assert stored.last_timestamp.tzinfo is not None

    async def test_circuit_breaker_counts_failures(self, database: object) -> None:
        state = SourceState(source="flaky")
        for _ in range(5):
            state.record_failure()
        assert state.is_circuit_open(threshold=5)

    async def test_run_metrics_are_persisted(self, database: object) -> None:
        run = IngestionRun(source="demo", records_received=10, jobs_created=8)
        async with database.session() as session:  # type: ignore[attr-defined]
            await IngestionRunRepository(session).save(run)
        async with database.read_session() as session:  # type: ignore[attr-defined]
            stored = await IngestionRunRepository(session).get(run.id)
            summary = await IngestionRunRepository(session).summary(since=days_ago(1))
        assert stored is not None
        assert stored.jobs_created == 8
        assert summary["runs"] == 1

    async def test_rejected_records_are_quarantined(self, database: object) -> None:
        records = [
            RejectedRecord(source="demo", reason="missing_required_field", message="no title")
        ]
        async with database.session() as session:  # type: ignore[attr-defined]
            assert await RejectedRecordRepository(session).add_many(records) == 1
        async with database.read_session() as session:  # type: ignore[attr-defined]
            counts = await RejectedRecordRepository(session).count_by_reason(since=days_ago(1))
        assert counts["missing_required_field"] == 1

    async def test_quality_events_are_stored(self, database: object) -> None:
        from app.models.enums import QualityDimension

        async with database.session() as session:  # type: ignore[attr-defined]
            await QualityRepository(session).add_many(
                [
                    QualityEvent(
                        source="demo",
                        dimension=QualityDimension.COMPLETENESS,
                        message="low completeness",
                        value=0.4,
                        threshold=0.6,
                    )
                ]
            )
        async with database.read_session() as session:  # type: ignore[attr-defined]
            counts = await QualityRepository(session).counts_by_dimension(since=days_ago(1))
        assert counts[QualityDimension.COMPLETENESS] == 1


class TestApiKeys:
    async def test_issue_and_authenticate(self, database: object) -> None:
        async with database.session() as session:  # type: ignore[attr-defined]
            repository = ApiKeyRepository(session)
            generated, _row = await repository.create(name="test", role=UserRole.ANALYST)

        async with database.session() as session:  # type: ignore[attr-defined]
            principal = await ApiKeyRepository(session).authenticate(
                generated.key_id, generated.full_key
            )
        assert principal is not None
        assert principal.role is UserRole.ANALYST
        assert principal.has_scope(Scope.ANALYTICS_READ)
        assert not principal.has_scope(Scope.ADMIN)

    async def test_wrong_secret_is_rejected(self, database: object) -> None:
        async with database.session() as session:  # type: ignore[attr-defined]
            generated, _row = await ApiKeyRepository(session).create(name="test")
        async with database.session() as session:  # type: ignore[attr-defined]
            principal = await ApiKeyRepository(session).authenticate(
                generated.key_id, generated.full_key + "tampered"
            )
        assert principal is None

    async def test_revoked_keys_stop_working(self, database: object) -> None:
        async with database.session() as session:  # type: ignore[attr-defined]
            generated, _row = await ApiKeyRepository(session).create(name="test")
        async with database.session() as session:  # type: ignore[attr-defined]
            await ApiKeyRepository(session).revoke(generated.key_id)
        async with database.session() as session:  # type: ignore[attr-defined]
            assert (
                await ApiKeyRepository(session).authenticate(generated.key_id, generated.full_key)
                is None
            )

    async def test_admin_role_implies_every_scope(self, database: object) -> None:
        async with database.session() as session:  # type: ignore[attr-defined]
            generated, _row = await ApiKeyRepository(session).create(
                name="root", role=UserRole.ADMIN
            )
        async with database.session() as session:  # type: ignore[attr-defined]
            principal = await ApiKeyRepository(session).authenticate(
                generated.key_id, generated.full_key
            )
        assert principal is not None
        assert principal.has_scope(Scope.INGESTION_WRITE)


class TestCache:
    async def test_set_get_and_prefix_delete(self, cache: InMemoryCache) -> None:
        key = build_key("test", "analytics", "overview")
        await cache.set(key, {"value": 1})
        assert await cache.get(key) == {"value": 1}
        assert await cache.delete_prefix(build_key("test", "analytics")) == 1
        assert await cache.get(key) is None

    async def test_read_through_helper_calls_the_factory_once(self, cache: InMemoryCache) -> None:
        calls = {"count": 0}

        async def factory() -> int:
            calls["count"] += 1
            return 7

        assert await cached(cache, "k", factory) == 7
        assert await cached(cache, "k", factory) == 7
        assert calls["count"] == 1

    async def test_counter_increments(self, cache: InMemoryCache) -> None:
        assert await cache.incr("c") == 1
        assert await cache.incr("c", amount=2) == 3

    async def test_long_keys_are_hashed(self) -> None:
        key = build_key("ns", "family", "x" * 500)
        assert len(key) < 200


class TestColumnarStore:
    def test_export_and_query(self, settings: object, normalized_jobs: list[NormalizedJob]) -> None:
        exporter = ParquetExporter(settings.storage)  # type: ignore[attr-defined]
        result = exporter.export(normalized_jobs)
        assert result.rows == len(normalized_jobs)
        assert exporter.partitions()

        store = AnalyticalStore(settings.storage)  # type: ignore[attr-defined]
        assert store.available()
        described = store.describe()
        assert described["rows"] == len(normalized_jobs)
        assert store.daily_volume()
        assert store.skill_demand(limit=5)

    def test_empty_export_is_a_no_op(self, settings: object) -> None:
        result = ParquetExporter(settings.storage).export([])  # type: ignore[attr-defined]
        assert result.rows == 0
        assert not AnalyticalStore(settings.storage).available()  # type: ignore[attr-defined]
