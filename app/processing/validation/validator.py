"""Validation of cleaned records.

The rule the platform follows is simple: **never silently delete data**. A
record that fails validation becomes a :class:`RejectedRecord`, is written to
the quarantine table with the reason and the original payload, and can be
replayed once the adapter or the rule is fixed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

from app.core.timeutils import utcnow
from app.models.enums import RejectionReason
from app.models.raw import RawJob, RejectedRecord

#: A title shorter than this cannot identify a role.
MIN_TITLE_LENGTH = 3
#: Descriptions below this length carry no analysable signal.
MIN_DESCRIPTION_LENGTH = 40
#: Clock skew we tolerate on a "published" timestamp.
FUTURE_TOLERANCE = timedelta(days=2)
#: Postings older than this are historical noise for a real-time platform.
MAX_AGE_DAYS = 730

_SCRIPT_RE = re.compile(r"<\s*script|javascript:\s*|on(?:error|load|click)\s*=", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


@dataclass(slots=True)
class ValidationOutcome:
    """The verdict for one record."""

    valid: bool
    rejection: RejectedRecord | None = None
    warnings: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    invalid_fields: list[str] = field(default_factory=list)

    @property
    def rejected(self) -> bool:
        return not self.valid


@dataclass(slots=True)
class ValidationSummary:
    """Aggregated validation results for a batch."""

    total: int = 0
    valid: int = 0
    rejected: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)
    missing_by_field: dict[str, int] = field(default_factory=dict)
    invalid_by_field: dict[str, int] = field(default_factory=dict)

    def record(self, outcome: ValidationOutcome) -> None:
        """Fold one outcome into the summary."""
        self.total += 1
        if outcome.valid:
            self.valid += 1
        else:
            self.rejected += 1
            if outcome.rejection is not None:
                reason = outcome.rejection.reason
                self.by_reason[reason] = self.by_reason.get(reason, 0) + 1
        for name in outcome.missing_fields:
            self.missing_by_field[name] = self.missing_by_field.get(name, 0) + 1
        for name in outcome.invalid_fields:
            self.invalid_by_field[name] = self.invalid_by_field.get(name, 0) + 1


class RecordValidator:
    """Decides whether a cleaned record may enter the pipeline."""

    #: Fields that are not required but whose absence lowers the quality score.
    OPTIONAL_TRACKED_FIELDS = (
        "company",
        "location",
        "salary",
        "employment_type",
        "remote_status",
        "published_at",
        "url",
    )

    def __init__(
        self,
        *,
        min_description_length: int = MIN_DESCRIPTION_LENGTH,
        max_age_days: int = MAX_AGE_DAYS,
        require_description: bool = True,
    ) -> None:
        self._min_description = min_description_length
        self._max_age_days = max_age_days
        self._require_description = require_description

    def validate(self, job: RawJob, *, run_id: str | None = None) -> ValidationOutcome:
        """Check one record and, on failure, build its quarantine entry."""
        outcome = ValidationOutcome(valid=True)

        def reject(reason: RejectionReason, message: str, field_name: str | None = None) -> None:
            outcome.valid = False
            outcome.rejection = RejectedRecord(
                source=job.source,
                source_job_id=job.source_job_id,
                reason=str(reason),
                message=message,
                field=field_name,
                payload=_safe_payload(job),
                ingestion_run_id=run_id,
            )

        title = (job.title or "").strip()
        if not title:
            outcome.missing_fields.append("title")
            reject(RejectionReason.MISSING_REQUIRED_FIELD, "title is missing", "title")
            return outcome
        if len(title) < MIN_TITLE_LENGTH:
            outcome.invalid_fields.append("title")
            reject(
                RejectionReason.TITLE_TOO_SHORT,
                f"title {title!r} is shorter than {MIN_TITLE_LENGTH} characters",
                "title",
            )
            return outcome

        description = (job.description or "").strip()
        if not description:
            outcome.missing_fields.append("description")
            if self._require_description:
                reject(
                    RejectionReason.MISSING_REQUIRED_FIELD, "description is missing", "description"
                )
                return outcome
        elif len(description) < self._min_description:
            outcome.invalid_fields.append("description")
            reject(
                RejectionReason.DESCRIPTION_TOO_SHORT,
                f"description has {len(description)} characters, "
                f"minimum is {self._min_description}",
                "description",
            )
            return outcome

        if _SCRIPT_RE.search(description) or _SCRIPT_RE.search(title):
            outcome.invalid_fields.append("description")
            reject(
                RejectionReason.UNSAFE_CONTENT,
                "record contains active content (script or event handler)",
                "description",
            )
            return outcome
        if _CONTROL_RE.search(description):
            outcome.warnings.append("description contained control characters")

        if job.published_at is not None:
            now = utcnow()
            if job.published_at > now + FUTURE_TOLERANCE:
                outcome.invalid_fields.append("published_at")
                reject(
                    RejectionReason.FUTURE_TIMESTAMP,
                    f"published_at {job.published_at.isoformat()} is in the future",
                    "published_at",
                )
                return outcome
            age_days = (now - job.published_at).days
            if age_days > self._max_age_days:
                outcome.invalid_fields.append("published_at")
                reject(
                    RejectionReason.STALE_POSTING,
                    f"posting is {age_days} days old (limit {self._max_age_days})",
                    "published_at",
                )
                return outcome

        if job.url and not job.url.lower().startswith(("http://", "https://")):
            outcome.invalid_fields.append("url")
            outcome.warnings.append(f"url has an unexpected scheme: {job.url[:60]}")

        for name in self.OPTIONAL_TRACKED_FIELDS:
            value = getattr(job, name, None)
            if value in (None, ""):
                outcome.missing_fields.append(name)

        return outcome

    def validate_batch(
        self, jobs: list[RawJob], *, run_id: str | None = None
    ) -> tuple[list[RawJob], list[RejectedRecord], ValidationSummary]:
        """Split a batch into accepted records and quarantined ones."""
        accepted: list[RawJob] = []
        rejected: list[RejectedRecord] = []
        summary = ValidationSummary()
        for job in jobs:
            outcome = self.validate(job, run_id=run_id)
            summary.record(outcome)
            if outcome.valid:
                accepted.append(job)
            elif outcome.rejection is not None:
                rejected.append(outcome.rejection)
        return accepted, rejected, summary


def _safe_payload(job: RawJob) -> dict[str, object]:
    """Keep enough of the record to debug and replay it, without the bulk."""
    return {
        "title": (job.title or "")[:200],
        "company": (job.company or "")[:120],
        "location": (job.location or "")[:120],
        "url": (job.url or "")[:300],
        "description_preview": (job.description or "")[:400],
        "published_at": job.published_at.isoformat() if job.published_at else None,
        "collected_at": job.collected_at.isoformat(),
    }


__all__ = [
    "FUTURE_TOLERANCE",
    "MAX_AGE_DAYS",
    "MIN_DESCRIPTION_LENGTH",
    "MIN_TITLE_LENGTH",
    "RecordValidator",
    "ValidationOutcome",
    "ValidationSummary",
]
