"""Ingestion orchestration.

This is where the pieces meet: a source produces raw records, the processing
pipeline turns them into canonical postings, the repositories persist them, and
the event bus tells the rest of the system what happened.

Two properties are deliberate:

* **Idempotency.** Replaying a run updates instead of duplicating, because
  identity is the ``(source, source_job_id)`` natural key and the deduplication
  engine is primed with the recent corpus before the batch is processed.
* **Incrementality.** Each source keeps a cursor; the next run resumes from it
  rather than re-downloading everything.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import IngestionError, JobIntelError
from app.core.logging import get_logger, log_context, new_correlation_id
from app.core.timeutils import days_ago, utcnow
from app.events.bus import EventBus, job_event
from app.ingestion.base import BaseJobSource, SourceContext
from app.ingestion.registry import SourceRegistry
from app.models.enums import EventType, IngestionStatus, JobStatus
from app.models.events import PipelineEvent
from app.models.ingestion import IngestionRun, SourceState
from app.models.job import NormalizedJob
from app.models.raw import RawBatch
from app.processing.deduplication.engine import KnownPosting
from app.processing.pipeline import ProcessingPipeline, ProcessingResult
from app.storage.models import JobEvent
from app.storage.parquet import ParquetExporter
from app.storage.repositories.ingestion import (
    IngestionRunRepository,
    RejectedRecordRepository,
    SourceStateRepository,
)
from app.storage.repositories.jobs import JobQuery, JobRepository
from app.storage.repositories.quality import QualityRepository
from app.storage.repositories.reference import (
    CompanyRepository,
    LocationRepository,
    SkillRepository,
)
from app.storage.session import Database

log = get_logger(__name__)

#: A source that keeps failing is skipped until an operator intervenes.
CIRCUIT_BREAKER_FAILURES = 5


@dataclass(slots=True)
class RunOutcome:
    """The result of ingesting one source."""

    run: IngestionRun
    processing: ProcessingResult | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.run.status in (IngestionStatus.SUCCESS, IngestionStatus.PARTIAL)


class IngestionService:
    """Fetches, processes and stores postings for the configured sources."""

    def __init__(
        self,
        *,
        database: Database,
        registry: SourceRegistry,
        pipeline: ProcessingPipeline | None = None,
        event_bus: EventBus | None = None,
        settings: Settings | None = None,
        exporter: ParquetExporter | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._db = database
        self._registry = registry
        self._pipeline = pipeline or ProcessingPipeline(settings=self._settings)
        self._bus = event_bus
        self._exporter = exporter

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    async def run_source(self, name: str, *, limit: int | None = None) -> RunOutcome:
        """Ingest one source end to end."""
        definition = self._registry.get_definition(name)
        correlation_id = new_correlation_id()
        run = IngestionRun(source=name, source_kind=definition.kind)

        with log_context(run_id=run.id, source=name, correlation_id=correlation_id):
            source = self._registry.create(name)
            batch_size = (
                limit or definition.batch_size or self._settings.ingestion.default_batch_size
            )

            async with self._db.session() as session:
                states = SourceStateRepository(session)
                runs = IngestionRunRepository(session)
                state = await states.get_or_create(name, definition.kind)
                if state.is_circuit_open(threshold=CIRCUIT_BREAKER_FAILURES):
                    run.finish(
                        IngestionStatus.SKIPPED,
                        error=f"circuit open after {state.consecutive_failures} failures",
                    )
                    await runs.save(run)
                    log.warning("ingestion.circuit_open", failures=state.consecutive_failures)
                    return RunOutcome(run=run, error=run.error)
                if not state.enabled:
                    run.finish(IngestionStatus.SKIPPED, error="source disabled")
                    await runs.save(run)
                    return RunOutcome(run=run, error=run.error)
                await runs.save(run)

            await self._publish(job_event(EventType.INGESTION_STARTED, source=name, run_id=run.id))

            try:
                batch = await self._fetch(source, state, run, batch_size)
            except JobIntelError as exc:
                return await self._record_failure(run, state, exc)
            except Exception as exc:
                return await self._record_failure(run, state, IngestionError(str(exc)))
            finally:
                await source.aclose()

            processing = await self._process(batch, run)
            outcome = await self._persist(batch, processing, run, state)
            await self._publish(
                job_event(
                    EventType.INGESTION_COMPLETED,
                    source=name,
                    run_id=run.id,
                    created=run.jobs_created,
                    updated=run.jobs_updated,
                    rejected=run.records_rejected,
                )
            )
            log.info("ingestion.completed", **_run_summary(run))
            return outcome

    async def run_all(self, *, limit: int | None = None) -> list[RunOutcome]:
        """Ingest every enabled source, one after another.

        Sources run sequentially on purpose: they share one outbound rate
        limiter and one database connection pool, and a job-market platform
        gains nothing from hammering five providers at once.
        """
        outcomes: list[RunOutcome] = []
        for definition in self._registry.definitions(enabled_only=True):
            try:
                outcomes.append(await self.run_source(definition.name, limit=limit))
            except Exception as exc:
                log.exception("ingestion.source_failed", source=definition.name, error=str(exc))
        return outcomes

    async def expire_stale(self, *, source: str | None = None) -> int:
        """Retire postings that no source has re-listed recently."""
        cutoff = days_ago(self._settings.ingestion.job_expiry_days)
        async with self._db.session() as session:
            expired = await JobRepository(session).expire_stale(cutoff=cutoff, source=source)
        if expired:
            log.info("ingestion.expired", count=expired, cutoff=cutoff.isoformat())
            await self._publish(job_event(EventType.JOB_EXPIRED, source=source, count=expired))
        return expired

    async def export_analytics(self, *, limit: int = 5000) -> int:
        """Copy recent postings into the columnar analytical store."""
        if self._exporter is None:
            self._exporter = ParquetExporter(self._settings.storage)
        async with self._db.read_session() as session:
            jobs, _total = await JobRepository(session).search(
                JobQuery(sort="first_seen_at", descending=True, limit=limit)
            )
        if not jobs:
            return 0
        result = self._exporter.export(jobs)
        return result.rows

    # ------------------------------------------------------------------ #
    # Stages
    # ------------------------------------------------------------------ #
    async def _fetch(
        self, source: BaseJobSource, state: SourceState, run: IngestionRun, batch_size: int
    ) -> RawBatch:
        started = time.perf_counter()
        context = SourceContext(
            cursor=state.last_cursor,
            since=state.last_timestamp,
            etag=state.etag,
            limit=batch_size,
            options=dict(state.config),
            run_id=run.id,
        )
        batch = await source.fetch_jobs(context)
        run.fetch_duration_ms = (time.perf_counter() - started) * 1000
        run.records_received = batch.size
        log.info(
            "ingestion.fetched",
            records=batch.size,
            cursor=batch.cursor,
            duration_ms=round(run.fetch_duration_ms, 2),
        )
        return batch

    async def _process(self, batch: RawBatch, run: IngestionRun) -> ProcessingResult:
        """Prime the deduplicator from the recent corpus, then process."""
        async with self._db.read_session() as session:
            repository = JobRepository(session)
            recent = await repository.recent_signatures(
                since=days_ago(self._settings.deduplication.candidate_window_days),
                limit=self._settings.deduplication.max_candidates,
            )
            # Read the identifiers inside the session: the ORM rows are
            # detached once it closes.
            identities = {
                key: row.id
                for key, row in (
                    await repository.find_by_natural_keys(
                        [(job.source, job.source_job_id) for job in batch.jobs]
                    )
                ).items()
            }
        self._pipeline.prime_deduplicator(
            [
                KnownPosting(job_id=job_id, content_hash=content_hash, signature=signature)
                for job_id, content_hash, signature in recent
            ]
        )
        result = self._pipeline.process(
            batch.jobs,
            source=batch.source,
            run_id=run.id,
            known_identities=identities,
        )
        run.processing_duration_ms = result.duration_ms
        run.nlp_duration_ms = result.normalization.nlp_ms
        run.records_validated = result.validation.valid
        run.records_rejected = len(result.rejected)
        run.duplicates_detected = len(result.duplicates)
        run.quality_score = result.quality.score if result.quality else 0.0
        return result

    async def _persist(
        self,
        batch: RawBatch,
        processing: ProcessingResult,
        run: IngestionRun,
        state: SourceState,
    ) -> RunOutcome:
        """Write everything the batch produced inside a single transaction."""
        started = time.perf_counter()
        async with self._db.session() as session:
            jobs_repo = JobRepository(session)
            companies = CompanyRepository(session)
            locations = LocationRepository(session)
            skills = SkillRepository(session)
            rejects = RejectedRecordRepository(session)
            quality = QualityRepository(session)
            runs = IngestionRunRepository(session)
            states = SourceStateRepository(session)

            accepted = processing.jobs
            duplicates = [job for job, _verdict in processing.duplicates]

            company_rows = await companies.resolve_many(
                [job.company_name for job in accepted + duplicates]
            )
            company_ids = {slug: row.id for slug, row in company_rows.items()}
            await locations.resolve_many([job.location for job in accepted])

            upsert = await jobs_repo.upsert_many(accepted, company_ids=company_ids)
            run.jobs_created = len(upsert.created)
            run.jobs_updated = len(upsert.updated)

            await skills.bulk_replace_job_skills({job.id: job.skills for job in accepted})
            await jobs_repo.record_salaries(accepted)

            if duplicates:
                # Duplicates are stored too: the audit trail explains why a
                # posting is not counted, instead of it silently vanishing.
                for job in duplicates:
                    job.status = JobStatus.DUPLICATE
                await jobs_repo.upsert_many(duplicates, company_ids=company_ids)

            await rejects.add_many(processing.rejected)
            await quality.add_many(processing.quality_events)
            await self._write_audit_events(session, processing, run)

            state.advance(
                cursor=batch.cursor,
                timestamp=_latest_timestamp(accepted),
                processed=len(accepted),
                failed=len(processing.rejected),
            )
            await states.save(state)

            run.storage_duration_ms = (time.perf_counter() - started) * 1000
            run.finish(IngestionStatus.PARTIAL if processing.rejected else IngestionStatus.SUCCESS)
            run.details = {
                "warnings": batch.warnings[:10],
                "duplicate_kinds": processing.deduplication.by_kind,
                "rejection_reasons": processing.validation.by_reason,
                "quality_grade": processing.quality.grade if processing.quality else None,
            }
            await runs.save(run)

        await self._publish_job_events(processing, run)
        return RunOutcome(run=run, processing=processing)

    async def _write_audit_events(
        self, session: AsyncSession, processing: ProcessingResult, run: IngestionRun
    ) -> None:
        """Append the lifecycle trail for this batch."""
        rows = [
            {
                "job_id": job.id,
                "event_type": str(EventType.JOB_CREATED),
                "occurred_at": utcnow(),
                "source": job.source,
                "ingestion_run_id": run.id,
                "payload": {"status": str(job.status), "quality": job.quality_score},
            }
            for job in processing.jobs
        ]
        rows.extend(
            {
                "job_id": job.id,
                "event_type": str(EventType.JOB_DUPLICATE),
                "occurred_at": utcnow(),
                "source": job.source,
                "ingestion_run_id": run.id,
                "payload": {
                    "duplicate_of": verdict.duplicate_of,
                    "kind": str(verdict.kind),
                    "similarity": verdict.similarity,
                },
            }
            for job, verdict in processing.duplicates
        )
        if not rows:
            return
        for start in range(0, len(rows), 200):
            await session.execute(insert(JobEvent), rows[start : start + 200])

    async def _record_failure(
        self, run: IngestionRun, state: SourceState, error: Exception
    ) -> RunOutcome:
        """Persist a failed run and open the circuit breaker a notch."""
        run.finish(IngestionStatus.FAILED, error=str(error))
        state.record_failure()
        async with self._db.session() as session:
            await IngestionRunRepository(session).save(run)
            await SourceStateRepository(session).save(state)
        log.error("ingestion.failed", error=str(error), source=run.source)
        return RunOutcome(run=run, error=str(error))

    async def _publish_job_events(self, processing: ProcessingResult, run: IngestionRun) -> None:
        if self._bus is None:
            return
        events = [
            job_event(
                EventType.JOB_CREATED,
                job_id=job.id,
                source=job.source,
                run_id=run.id,
                segment=str(job.segment),
                skills=job.skill_slugs[:10],
            )
            for job in processing.jobs
        ]
        if events:
            await self._bus.publish_many(events)

    async def _publish(self, event: PipelineEvent) -> None:
        if self._bus is None:
            return
        await self._bus.publish(event)


def _latest_timestamp(jobs: list[NormalizedJob]) -> datetime | None:
    """Newest publication timestamp in a batch, for the incremental cursor."""
    stamps = [job.published_at for job in jobs if job.published_at is not None]
    return max(stamps) if stamps else None


def _run_summary(run: IngestionRun) -> dict[str, object]:
    return {
        "source": run.source,
        "status": str(run.status),
        "received": run.records_received,
        "created": run.jobs_created,
        "updated": run.jobs_updated,
        "duplicates": run.duplicates_detected,
        "rejected": run.records_rejected,
        "quality": round(run.quality_score, 3),
        "duration_s": round(run.duration_seconds, 2),
    }


__all__ = ["CIRCUIT_BREAKER_FAILURES", "IngestionService", "RunOutcome"]
