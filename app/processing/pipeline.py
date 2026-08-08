"""The processing pipeline.

    raw records
        -> cleaning        (repair encoding, strip markup, collapse whitespace)
        -> validation      (accept or quarantine, never discard)
        -> normalization   (canonical schema + NLP enrichment)
        -> deduplication   (exact, URL, content, near-duplicate)
        -> quality scoring (six dimensions, per source and per batch)

The pipeline is pure: it performs no I/O and touches no database. That makes
the whole data path unit-testable and lets the same code run inside a worker,
inside the CLI or inside a benchmark.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.models.enums import JobStatus
from app.models.job import NormalizedJob
from app.models.quality import QualityEvent, QualityReport
from app.models.raw import RawJob, RejectedRecord
from app.nlp.pipeline import NlpPipeline
from app.processing.cleaning.cleaner import RecordCleaner
from app.processing.deduplication.engine import (
    DeduplicationEngine,
    DeduplicationStats,
    DuplicateVerdict,
    KnownPosting,
)
from app.processing.normalization.normalizer import JobNormalizer, NormalizationStats
from app.processing.validation.validator import RecordValidator, ValidationSummary
from app.quality.scorer import BatchSignals, QualityScorer, score_job

log = get_logger(__name__)


@dataclass(slots=True)
class ProcessingResult:
    """Everything one pass over a batch produced."""

    jobs: list[NormalizedJob] = field(default_factory=list)
    duplicates: list[tuple[NormalizedJob, DuplicateVerdict]] = field(default_factory=list)
    rejected: list[RejectedRecord] = field(default_factory=list)
    validation: ValidationSummary = field(default_factory=ValidationSummary)
    normalization: NormalizationStats = field(default_factory=NormalizationStats)
    deduplication: DeduplicationStats = field(default_factory=DeduplicationStats)
    quality: QualityReport | None = None
    quality_events: list[QualityEvent] = field(default_factory=list)
    duration_ms: float = 0.0
    records_received: int = 0

    @property
    def accepted_count(self) -> int:
        return len(self.jobs)

    @property
    def throughput_per_second(self) -> float:
        if self.duration_ms <= 0:
            return 0.0
        return self.records_received / (self.duration_ms / 1000)

    def summary(self) -> dict[str, float | int | str]:
        """Compact structure for logs and CLI output."""
        return {
            "received": self.records_received,
            "accepted": len(self.jobs),
            "duplicates": len(self.duplicates),
            "rejected": len(self.rejected),
            "quality_score": round(self.quality.score, 4) if self.quality else 0.0,
            "duration_ms": round(self.duration_ms, 2),
            "throughput_per_second": round(self.throughput_per_second, 1),
        }


class ProcessingPipeline:
    """Runs cleaning, validation, normalization, dedup and scoring."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        cleaner: RecordCleaner | None = None,
        validator: RecordValidator | None = None,
        normalizer: JobNormalizer | None = None,
        deduplicator: DeduplicationEngine | None = None,
        scorer: QualityScorer | None = None,
        nlp: NlpPipeline | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._cleaner = cleaner or RecordCleaner(
            max_description_chars=self._settings.nlp.max_description_chars
        )
        self._validator = validator or RecordValidator()
        self._normalizer = normalizer or JobNormalizer(
            nlp=nlp
            or NlpPipeline(
                taxonomy=None,
                min_skill_confidence=self._settings.nlp.min_skill_confidence,
                max_description_chars=self._settings.nlp.max_description_chars,
            )
        )
        self._deduplicator = deduplicator or DeduplicationEngine(self._settings.deduplication)
        self._scorer = scorer or QualityScorer()

    @property
    def deduplicator(self) -> DeduplicationEngine:
        return self._deduplicator

    @property
    def normalizer(self) -> JobNormalizer:
        return self._normalizer

    def prime_deduplicator(self, known: list[KnownPosting]) -> None:
        """Load the recent-postings window used for duplicate detection."""
        self._deduplicator.prime(known)

    def process(
        self,
        records: list[RawJob],
        *,
        source: str | None = None,
        run_id: str | None = None,
        known_identities: dict[tuple[str, str], str] | None = None,
    ) -> ProcessingResult:
        """Run the full pipeline over a batch of raw records.

        ``known_identities`` maps ``(source, source_job_id)`` to the identifier
        a posting already has in storage. Re-adopting that identifier is what
        makes re-ingestion an *update*: without it the deduplication engine
        would recognise the posting's own stored copy and flag it as a
        duplicate of itself.
        """
        started = time.perf_counter()
        result = ProcessingResult(records_received=len(records))
        if not records:
            result.duration_ms = (time.perf_counter() - started) * 1000
            return result

        batch_source = source or records[0].source

        cleaned: list[RawJob] = []
        for record in records:
            repaired, _report = self._cleaner.clean(record)
            cleaned.append(repaired)

        accepted, rejected, validation = self._validator.validate_batch(cleaned, run_id=run_id)
        result.rejected = rejected
        result.validation = validation

        normalized, normalization = self._normalizer.normalize_batch(accepted)
        result.normalization = normalization

        identities = known_identities or {}
        for job in normalized:
            existing_id = identities.get((job.source, job.source_job_id))
            if existing_id:
                job.id = existing_id
            self._deduplicator.fingerprint(job)

        unique, duplicates, dedup_stats = self._deduplicator.process_batch(normalized)
        result.duplicates = duplicates
        result.deduplication = dedup_stats

        for job in unique:
            job.quality_score = score_job(job)
            job.status = JobStatus.ACTIVE
        for job, _verdict in duplicates:
            job.quality_score = score_job(job)
            job.status = JobStatus.DUPLICATE
        result.jobs = unique

        report, events = self._scorer.evaluate(
            source=batch_source,
            jobs=unique,
            signals=BatchSignals(
                records_received=len(records),
                records_rejected=len(rejected),
                duplicates_detected=len(duplicates),
            ),
            run_id=run_id,
        )
        result.quality = report
        result.quality_events = events
        result.duration_ms = (time.perf_counter() - started) * 1000

        log.info(
            "processing.batch_completed",
            source=batch_source,
            run_id=run_id,
            **result.summary(),
        )
        return result


__all__ = ["ProcessingPipeline", "ProcessingResult"]
