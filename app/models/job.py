"""The canonical, normalized job model.

This is the schema every downstream consumer (storage, analytics, search, API)
agrees on. It is intentionally explicit: whenever the platform infers something
it also records how confident it is and where the value came from.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from app.core.text import truncate
from app.core.timeutils import utcnow
from app.models.enums import (
    ANNUALIZATION_FACTORS,
    DuplicateKind,
    EmploymentType,
    ExperienceLevel,
    JobStatus,
    MarketSegment,
    RemoteType,
    SalaryPeriod,
    SalaryProvenance,
    SkillCategory,
    SourceKind,
)

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


def new_job_id() -> str:
    """Generate a job identifier (UUID4 hex, safe to expose publicly)."""
    return uuid.uuid4().hex


class LocationInfo(BaseModel):
    """Normalized geography for a posting."""

    model_config = ConfigDict(extra="forbid")

    raw: str | None = None
    country: str | None = Field(default=None, max_length=96)
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    region: str | None = Field(default=None, max_length=96)
    city: str | None = Field(default=None, max_length=96)
    remote_hint: RemoteType = RemoteType.UNKNOWN
    confidence: Confidence = 0.0

    @property
    def is_resolved(self) -> bool:
        return bool(self.country_code or self.city)

    def display(self) -> str:
        """Human-readable ``City, Country`` string."""
        parts = [p for p in (self.city, self.region, self.country) if p]
        return ", ".join(dict.fromkeys(parts)) or (self.raw or "Unknown")


class SalaryInfo(BaseModel):
    """Salary range with explicit provenance.

    The platform never invents figures: when a posting has no salary the
    provenance stays :attr:`SalaryProvenance.UNKNOWN` and all amounts are
    ``None``.
    """

    model_config = ConfigDict(extra="forbid")

    raw: str | None = None
    min_amount: float | None = Field(default=None, ge=0)
    max_amount: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    period: SalaryPeriod = SalaryPeriod.UNKNOWN
    provenance: SalaryProvenance = SalaryProvenance.UNKNOWN
    normalized_currency: str | None = Field(default=None, min_length=3, max_length=3)
    annual_min: float | None = Field(default=None, ge=0)
    annual_max: float | None = Field(default=None, ge=0)
    confidence: Confidence = 0.0

    @model_validator(mode="after")
    def _order_bounds(self) -> Self:
        low, high = self.min_amount, self.max_amount
        if low is not None and high is not None and low > high:
            object.__setattr__(self, "min_amount", high)
            object.__setattr__(self, "max_amount", low)
        return self

    @property
    def is_observed(self) -> bool:
        return self.provenance is SalaryProvenance.OBSERVED

    @property
    def annual_midpoint(self) -> float | None:
        """Midpoint of the annualised range, or the single known bound."""
        values = [v for v in (self.annual_min, self.annual_max) if v is not None]
        if not values:
            return None
        return sum(values) / len(values)

    def annualize(self) -> tuple[float | None, float | None]:
        """Convert the quoted range to a yearly figure using the period factor."""
        factor = ANNUALIZATION_FACTORS.get(self.period)
        if factor is None:
            return (None, None)
        low = self.min_amount * factor if self.min_amount is not None else None
        high = self.max_amount * factor if self.max_amount is not None else None
        return (low, high)


class ExtractedSkill(BaseModel):
    """A skill detected in a posting, with evidence."""

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=96)
    category: SkillCategory = SkillCategory.OTHER
    parent: str | None = Field(default=None, max_length=64)
    confidence: Confidence = 0.0
    occurrences: int = Field(default=1, ge=0)
    matched_alias: str | None = Field(default=None, max_length=96)
    field: str = Field(default="description", max_length=32)
    is_required: bool | None = None

    def __hash__(self) -> int:
        return hash(self.slug)


class TitleAnalysis(BaseModel):
    """Result of job-title normalization."""

    model_config = ConfigDict(extra="forbid")

    raw_title: str
    canonical_title: str
    title_family: str
    seniority: ExperienceLevel = ExperienceLevel.UNKNOWN
    specialization: str | None = None
    segment: MarketSegment = MarketSegment.OTHER
    confidence: Confidence = 0.0


class NormalizedJob(BaseModel):
    """The canonical representation of a job posting."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    id: str = Field(default_factory=new_job_id, max_length=32)
    source: str = Field(min_length=1, max_length=64)
    source_kind: SourceKind = SourceKind.API
    source_job_id: str = Field(min_length=1, max_length=256)
    canonical_url: str | None = Field(default=None, max_length=2048)
    url_fingerprint: str | None = Field(default=None, max_length=64)

    title: str = Field(min_length=1, max_length=512)
    normalized_title: str = Field(min_length=1, max_length=256)
    title_family: str = Field(default="Other", max_length=128)
    specialization: str | None = Field(default=None, max_length=128)
    title_confidence: Confidence = 0.0

    company_name: str = Field(default="Unknown", max_length=256)
    company_slug: str = Field(default="unknown", max_length=128)

    description: str = ""
    description_snippet: str = Field(default="", max_length=512)

    location: LocationInfo = Field(default_factory=LocationInfo)
    remote_type: RemoteType = RemoteType.UNKNOWN
    employment_type: EmploymentType = EmploymentType.UNKNOWN
    experience_level: ExperienceLevel = ExperienceLevel.UNKNOWN
    experience_years_min: float | None = Field(default=None, ge=0, le=60)
    experience_years_max: float | None = Field(default=None, ge=0, le=60)

    salary: SalaryInfo = Field(default_factory=SalaryInfo)
    industry: str | None = Field(default=None, max_length=128)
    segment: MarketSegment = MarketSegment.OTHER
    skills: list[ExtractedSkill] = Field(default_factory=list)

    published_at: datetime | None = None
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    expired_at: datetime | None = None

    status: JobStatus = JobStatus.NORMALIZED
    duplicate_of: str | None = Field(default=None, max_length=32)
    duplicate_kind: DuplicateKind = DuplicateKind.NONE
    content_hash: str = Field(default="", max_length=64)
    similarity_signature: list[int] = Field(default_factory=list)

    quality_score: Confidence = 0.0
    language: str | None = Field(default=None, max_length=8)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _derive_defaults(self) -> Self:
        if not self.description_snippet and self.description:
            object.__setattr__(self, "description_snippet", truncate(self.description, 480))
        if (
            self.experience_years_min is not None
            and self.experience_years_max is not None
            and self.experience_years_min > self.experience_years_max
        ):
            object.__setattr__(self, "experience_years_max", self.experience_years_min)
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def skill_slugs(self) -> list[str]:
        """Sorted list of detected skill identifiers."""
        return sorted({s.slug for s in self.skills})

    @property
    def is_remote(self) -> bool:
        return self.remote_type is RemoteType.REMOTE

    @property
    def is_active(self) -> bool:
        return self.status.is_live

    def top_skills(self, limit: int = 10) -> list[ExtractedSkill]:
        """Highest-confidence skills first."""
        return sorted(self.skills, key=lambda s: (-s.confidence, -s.occurrences, s.slug))[:limit]

    def touch(self, *, seen_at: datetime | None = None) -> None:
        """Record that the posting was observed again."""
        now = seen_at or utcnow()
        object.__setattr__(self, "last_seen_at", now)
        object.__setattr__(self, "updated_at", now)


__all__ = [
    "Confidence",
    "ExtractedSkill",
    "LocationInfo",
    "NormalizedJob",
    "SalaryInfo",
    "TitleAnalysis",
    "new_job_id",
]
