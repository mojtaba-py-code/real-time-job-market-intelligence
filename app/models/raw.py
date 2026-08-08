"""The raw job record - the platform's contract with the outside world.

Adapters convert whatever a source returns into a :class:`RawJob`. Nothing is
interpreted at this stage: the payload is captured as faithfully as possible so
that a later pipeline change can be replayed against the original data.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.hashing import normalize_url, source_fingerprint
from app.core.timeutils import parse_datetime, utcnow
from app.models.enums import SourceKind

MAX_RAW_TEXT = 200_000


class RawJob(BaseModel):
    """A single job posting exactly as it was received from a source."""

    model_config = ConfigDict(extra="forbid", frozen=False, str_strip_whitespace=True)

    source: str = Field(min_length=1, max_length=64, description="Identifier of the source adapter")
    source_kind: SourceKind = SourceKind.API
    source_job_id: str = Field(min_length=1, max_length=256)
    url: str | None = Field(default=None, max_length=2048)

    title: str | None = Field(default=None, max_length=512)
    description: str | None = Field(default=None, max_length=MAX_RAW_TEXT)
    company: str | None = Field(default=None, max_length=256)
    location: str | None = Field(default=None, max_length=256)
    salary: str | None = Field(default=None, max_length=256)
    employment_type: str | None = Field(default=None, max_length=64)
    remote_status: str | None = Field(default=None, max_length=64)
    industry: str | None = Field(default=None, max_length=128)

    published_at: datetime | None = None
    collected_at: datetime = Field(default_factory=utcnow)

    raw_payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source", "source_job_id")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned

    @field_validator("published_at", "collected_at", mode="before")
    @classmethod
    def _coerce_datetime(cls, value: object) -> object:
        if value is None or isinstance(value, datetime):
            return value
        parsed = parse_datetime(value)
        if parsed is None:
            raise ValueError(f"unparseable timestamp: {value!r}")
        return parsed

    @model_validator(mode="after")
    def _ensure_timezones(self) -> Self:
        from app.core.timeutils import ensure_utc

        if self.published_at is not None:
            object.__setattr__(self, "published_at", ensure_utc(self.published_at))
        object.__setattr__(self, "collected_at", ensure_utc(self.collected_at))
        return self

    @property
    def natural_key(self) -> str:
        """Stable ``source + source_job_id`` identity."""
        return source_fingerprint(self.source, self.source_job_id)

    @property
    def canonical_url(self) -> str:
        """URL with tracking parameters and cosmetic variance removed."""
        return normalize_url(self.url)

    def payload_size(self) -> int:
        """Approximate size of the captured payload in characters."""
        return len(str(self.raw_payload))

    def with_metadata(self, **fields: Any) -> RawJob:
        """Return a copy carrying additional pipeline metadata."""
        return self.model_copy(update={"metadata": {**self.metadata, **fields}})


class RawBatch(BaseModel):
    """A batch of raw jobs produced by one adapter invocation."""

    model_config = ConfigDict(extra="forbid")

    source: str
    source_kind: SourceKind
    jobs: list[RawJob] = Field(default_factory=list)
    fetched_at: datetime = Field(default_factory=utcnow)
    cursor: str | None = None
    has_more: bool = False
    warnings: list[str] = Field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.jobs)

    def __len__(self) -> int:
        return len(self.jobs)


class RejectedRecord(BaseModel):
    """A raw record that failed validation, kept for auditing and replay."""

    model_config = ConfigDict(extra="forbid")

    source: str
    source_job_id: str | None = None
    reason: str
    message: str
    field: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    rejected_at: datetime = Field(default_factory=utcnow)
    ingestion_run_id: str | None = None


__all__ = ["MAX_RAW_TEXT", "RawBatch", "RawJob", "RejectedRecord"]
