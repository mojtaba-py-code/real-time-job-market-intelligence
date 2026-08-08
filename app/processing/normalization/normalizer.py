"""Assembly of the canonical job record.

The normalizer is the point where a raw posting becomes a
:class:`NormalizedJob`: geography, compensation, employer, role and skills are
resolved by dedicated components and combined here. It owns no parsing logic of
its own, which keeps each rule testable in isolation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.core.hashing import normalize_url, url_fingerprint
from app.core.logging import get_logger
from app.core.text import truncate
from app.core.timeutils import utcnow
from app.models.enums import JobStatus, RemoteType
from app.models.job import NormalizedJob
from app.models.raw import RawJob
from app.nlp.pipeline import NlpPipeline, NlpResult
from app.processing.normalization.company import CompanyNormalizer
from app.processing.normalization.location import LocationNormalizer
from app.processing.normalization.salary import SalaryParser

log = get_logger(__name__)


@dataclass(slots=True)
class NormalizationStats:
    """Timing and coverage counters for one batch."""

    normalized: int = 0
    nlp_ms: float = 0.0
    total_ms: float = 0.0
    with_location: int = 0
    with_salary: int = 0
    with_skills: int = 0
    remote_detected: int = 0

    def merge(self, other: NormalizationStats) -> None:
        self.normalized += other.normalized
        self.nlp_ms += other.nlp_ms
        self.total_ms += other.total_ms
        self.with_location += other.with_location
        self.with_salary += other.with_salary
        self.with_skills += other.with_skills
        self.remote_detected += other.remote_detected


class JobNormalizer:
    """Turns a cleaned :class:`RawJob` into the canonical schema."""

    def __init__(
        self,
        *,
        nlp: NlpPipeline | None = None,
        location: LocationNormalizer | None = None,
        salary: SalaryParser | None = None,
        company: CompanyNormalizer | None = None,
        snippet_length: int = 480,
    ) -> None:
        self._nlp = nlp or NlpPipeline()
        self._location = location or LocationNormalizer()
        self._salary = salary or SalaryParser()
        self._company = company or CompanyNormalizer()
        self._snippet_length = snippet_length

    @property
    def nlp(self) -> NlpPipeline:
        return self._nlp

    def normalize(self, raw: RawJob, *, seen_at: object = None) -> NormalizedJob:
        """Build the canonical record for one posting."""
        started = time.perf_counter()
        now = utcnow()

        company = self._company.normalize(raw.company)
        location = self._location.normalize(
            raw.location, remote_hint=raw.remote_status, description=raw.description
        )
        analysis: NlpResult = self._nlp.analyze(
            title=raw.title or "",
            description=raw.description,
            raw_employment_type=raw.employment_type,
        )
        salary = self._salary.parse(raw.salary, country_code=location.country_code)

        remote = location.remote_hint
        if remote is RemoteType.UNKNOWN and raw.remote_status:
            remote = self._location.normalize(raw.remote_status).remote_hint

        canonical_url = normalize_url(raw.url) or None
        description = raw.description or ""

        job = NormalizedJob(
            source=raw.source,
            source_kind=raw.source_kind,
            source_job_id=raw.source_job_id,
            canonical_url=canonical_url,
            url_fingerprint=url_fingerprint(canonical_url) or None,
            title=(raw.title or "Untitled")[:512],
            normalized_title=analysis.title.canonical_title[:256],
            title_family=analysis.title.title_family[:128],
            specialization=analysis.title.specialization,
            title_confidence=analysis.title.confidence,
            company_name=company.name,
            company_slug=company.slug,
            description=description,
            description_snippet=truncate(description, self._snippet_length),
            location=location,
            remote_type=remote,
            employment_type=analysis.employment_type,
            experience_level=analysis.experience.level,
            experience_years_min=analysis.experience.years_min,
            experience_years_max=analysis.experience.years_max,
            salary=salary,
            industry=raw.industry,
            segment=analysis.segment,
            skills=analysis.skills,
            published_at=raw.published_at,
            first_seen_at=raw.collected_at or now,
            last_seen_at=now,
            updated_at=now,
            status=JobStatus.NORMALIZED,
            language=analysis.language,
            metadata={
                **raw.metadata,
                "nlp_ms": round(analysis.duration_ms, 3),
                "title_confidence": analysis.title.confidence,
                "location_confidence": location.confidence,
                "salary_confidence": salary.confidence,
                "language_confidence": analysis.language_confidence,
            },
        )
        job.metadata["normalization_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return job

    def normalize_batch(
        self, records: list[RawJob]
    ) -> tuple[list[NormalizedJob], NormalizationStats]:
        """Normalize a batch, collecting coverage statistics as it goes."""
        stats = NormalizationStats()
        jobs: list[NormalizedJob] = []
        started = time.perf_counter()

        for raw in records:
            job = self.normalize(raw)
            jobs.append(job)
            stats.normalized += 1
            stats.nlp_ms += float(job.metadata.get("nlp_ms", 0.0))
            if job.location.is_resolved:
                stats.with_location += 1
            if job.salary.is_observed:
                stats.with_salary += 1
            if job.skills:
                stats.with_skills += 1
            if job.remote_type is not RemoteType.UNKNOWN:
                stats.remote_detected += 1

        stats.total_ms = (time.perf_counter() - started) * 1000
        return jobs, stats


__all__ = ["JobNormalizer", "NormalizationStats"]
