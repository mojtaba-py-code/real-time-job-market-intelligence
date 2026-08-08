"""Data-quality measurement.

Quality is not a vibe: it is six measurable dimensions, each with an explicit
definition, combined with declared weights into one score. Every dimension is
computed per source and per batch so a regression can be attributed instead of
being averaged away.

    completeness - are the fields we need actually populated?
    validity     - did the records pass the validation rules?
    uniqueness   - how much of the batch was duplicated?
    consistency  - do derived fields contradict each other?
    accuracy     - how confident were the parsers and classifiers?
    freshness    - how recent are the postings?
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.timeutils import utcnow
from app.models.enums import (
    QUALITY_DIMENSION_WEIGHTS,
    QualityDimension,
    RemoteType,
    SalaryProvenance,
)
from app.models.job import NormalizedJob
from app.models.quality import FieldQuality, QualityEvent, QualityReport

#: Fields whose presence defines completeness, with their relative importance.
COMPLETENESS_FIELDS: tuple[tuple[str, float], ...] = (
    ("title", 1.5),
    ("description", 1.5),
    ("company_name", 1.2),
    ("location", 1.0),
    ("published_at", 1.0),
    ("canonical_url", 0.8),
    ("employment_type", 0.6),
    ("remote_type", 0.6),
    ("salary", 0.5),
    ("skills", 1.0),
)

#: A dimension below its threshold produces a quality event.
DEFAULT_THRESHOLDS: dict[QualityDimension, float] = {
    QualityDimension.COMPLETENESS: 0.6,
    QualityDimension.VALIDITY: 0.9,
    QualityDimension.UNIQUENESS: 0.7,
    QualityDimension.CONSISTENCY: 0.8,
    QualityDimension.ACCURACY: 0.5,
    QualityDimension.FRESHNESS: 0.5,
}

#: Postings older than this contribute nothing to the freshness score.
FRESHNESS_HORIZON_DAYS = 60.0


def _has_value(job: NormalizedJob, field: str) -> bool:
    """Whether one completeness field carries information."""
    match field:
        case "location":
            return job.location.is_resolved
        case "salary":
            return job.salary.is_observed
        case "skills":
            return bool(job.skills)
        case "remote_type":
            return job.remote_type is not RemoteType.UNKNOWN
        case "employment_type":
            return str(job.employment_type) != "unknown"
        case _:
            value = getattr(job, field, None)
            return value not in (None, "")


def score_job(job: NormalizedJob) -> float:
    """Completeness-weighted quality score for a single posting."""
    total_weight = sum(weight for _, weight in COMPLETENESS_FIELDS)
    achieved = sum(weight for field, weight in COMPLETENESS_FIELDS if _has_value(job, field))
    completeness = achieved / total_weight if total_weight else 0.0

    confidences = [
        job.title_confidence,
        job.location.confidence,
        job.salary.confidence if job.salary.is_observed else 0.5,
    ]
    if job.skills:
        confidences.append(sum(s.confidence for s in job.skills) / len(job.skills))
    accuracy = sum(confidences) / len(confidences)

    return round(min(1.0, 0.65 * completeness + 0.35 * accuracy), 4)


@dataclass(slots=True)
class BatchSignals:
    """Cross-cutting numbers the scorer cannot derive from the jobs alone."""

    records_received: int = 0
    records_rejected: int = 0
    duplicates_detected: int = 0


class QualityScorer:
    """Computes the quality report for one processed batch."""

    def __init__(self, thresholds: dict[QualityDimension, float] | None = None) -> None:
        self._thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}

    def evaluate(
        self,
        *,
        source: str,
        jobs: list[NormalizedJob],
        signals: BatchSignals,
        run_id: str | None = None,
    ) -> tuple[QualityReport, list[QualityEvent]]:
        """Measure every dimension and raise events for threshold breaches."""
        received = max(signals.records_received, len(jobs))
        dimensions: dict[QualityDimension, float] = {
            QualityDimension.COMPLETENESS: self._completeness(jobs),
            QualityDimension.VALIDITY: self._validity(received, signals.records_rejected),
            QualityDimension.UNIQUENESS: self._uniqueness(received, signals.duplicates_detected),
            QualityDimension.CONSISTENCY: self._consistency(jobs),
            QualityDimension.ACCURACY: self._accuracy(jobs),
            QualityDimension.FRESHNESS: self._freshness(jobs),
        }

        report = QualityReport(
            source=source,
            ingestion_run_id=run_id,
            records_evaluated=len(jobs),
            dimensions=dimensions,
            fields=self._field_quality(jobs),
        )

        events: list[QualityEvent] = []
        for dimension, value in dimensions.items():
            threshold = self._thresholds.get(dimension)
            if threshold is not None and value < threshold:
                events.append(
                    QualityEvent(
                        source=source,
                        dimension=dimension,
                        severity="warning" if value >= threshold * 0.7 else "error",
                        message=(
                            f"{dimension.value} scored {value:.2f}, below the "
                            f"threshold of {threshold:.2f}"
                        ),
                        value=round(value, 4),
                        threshold=threshold,
                        ingestion_run_id=run_id,
                        occurred_at=utcnow(),
                    )
                )
                report.issues.append(f"{dimension.value} below threshold")

        return report, events

    # ------------------------------------------------------------------ #
    # Dimensions
    # ------------------------------------------------------------------ #
    @staticmethod
    def _completeness(jobs: list[NormalizedJob]) -> float:
        if not jobs:
            return 0.0
        total_weight = sum(weight for _, weight in COMPLETENESS_FIELDS) * len(jobs)
        achieved = sum(
            weight
            for job in jobs
            for field, weight in COMPLETENESS_FIELDS
            if _has_value(job, field)
        )
        return achieved / total_weight if total_weight else 0.0

    @staticmethod
    def _validity(received: int, rejected: int) -> float:
        if received <= 0:
            return 1.0
        return max(0.0, 1.0 - rejected / received)

    @staticmethod
    def _uniqueness(received: int, duplicates: int) -> float:
        if received <= 0:
            return 1.0
        return max(0.0, 1.0 - duplicates / received)

    @staticmethod
    def _consistency(jobs: list[NormalizedJob]) -> float:
        """Share of postings whose derived fields do not contradict each other."""
        if not jobs:
            return 1.0
        consistent = 0
        for job in jobs:
            problems = 0
            salary = job.salary
            if (
                salary.min_amount is not None
                and salary.max_amount is not None
                and salary.min_amount > salary.max_amount
            ):
                problems += 1
            if salary.provenance is SalaryProvenance.OBSERVED and salary.currency is None:
                problems += 1
            if (
                job.experience_years_min is not None
                and job.experience_years_max is not None
                and job.experience_years_min > job.experience_years_max
            ):
                problems += 1
            if job.published_at is not None and job.published_at > utcnow():
                problems += 1
            if not job.normalized_title or job.normalized_title == "Other":
                problems += 1
            if problems == 0:
                consistent += 1
        return consistent / len(jobs)

    @staticmethod
    def _accuracy(jobs: list[NormalizedJob]) -> float:
        if not jobs:
            return 0.0
        scores: list[float] = []
        for job in jobs:
            parts = [job.title_confidence, job.location.confidence]
            if job.salary.is_observed:
                parts.append(job.salary.confidence)
            if job.skills:
                parts.append(sum(s.confidence for s in job.skills) / len(job.skills))
            scores.append(sum(parts) / len(parts))
        return sum(scores) / len(scores)

    @staticmethod
    def _freshness(jobs: list[NormalizedJob]) -> float:
        dated = [job.published_at for job in jobs if job.published_at is not None]
        if not dated:
            return 0.0
        now = utcnow()
        scores = [
            max(0.0, 1.0 - ((now - published).days / FRESHNESS_HORIZON_DAYS)) for published in dated
        ]
        coverage = len(dated) / len(jobs)
        return (sum(scores) / len(scores)) * coverage

    @staticmethod
    def _field_quality(jobs: list[NormalizedJob]) -> list[FieldQuality]:
        """Per-field presence counts, for drill-down in the dashboard."""
        return [
            FieldQuality(
                field=field,
                present=sum(1 for job in jobs if _has_value(job, field)),
                missing=sum(1 for job in jobs if not _has_value(job, field)),
            )
            for field, _ in COMPLETENESS_FIELDS
        ]

    @property
    def weights(self) -> dict[QualityDimension, float]:
        """The dimension weights used to fold the score."""
        return dict(QUALITY_DIMENSION_WEIGHTS)


__all__ = [
    "COMPLETENESS_FIELDS",
    "DEFAULT_THRESHOLDS",
    "FRESHNESS_HORIZON_DAYS",
    "BatchSignals",
    "QualityScorer",
    "score_job",
]
