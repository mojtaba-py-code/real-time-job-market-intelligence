"""Request and response models for the HTTP API.

The domain models are reused wherever they already have the right shape; this
module only adds the envelopes, the pagination contract and the input models
that exist purely at the HTTP boundary.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.core.timeutils import utcnow
from app.models.enums import (
    AlertChannel,
    AlertMetric,
    ComparisonOperator,
    EmploymentType,
    ExperienceLevel,
    MarketSegment,
    RemoteType,
    Scope,
    UserRole,
)
from app.models.job import NormalizedJob

T = TypeVar("T")


class PageMeta(BaseModel):
    """Pagination metadata attached to every list response."""

    model_config = ConfigDict(extra="forbid")

    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    returned: int = Field(ge=0)

    @property
    def has_more(self) -> bool:
        return self.offset + self.returned < self.total

    @property
    def next_offset(self) -> int | None:
        return self.offset + self.returned if self.has_more else None


class Page(BaseModel, Generic[T]):
    """A page of results."""

    model_config = ConfigDict(extra="forbid")

    items: list[T]
    meta: PageMeta

    @classmethod
    def build(cls, items: list[T], *, total: int, limit: int, offset: int) -> Page[T]:
        return cls(
            items=items,
            meta=PageMeta(total=total, limit=limit, offset=offset, returned=len(items)),
        )


class ErrorBody(BaseModel):
    """The body of every error response."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None
    timestamp: datetime = Field(default_factory=utcnow)


class ErrorResponse(BaseModel):
    """Envelope used by every non-2xx response."""

    model_config = ConfigDict(extra="forbid")

    error: ErrorBody


class HealthResponse(BaseModel):
    """Liveness and dependency status."""

    model_config = ConfigDict(extra="forbid")

    status: str
    version: str
    checks: dict[str, bool]
    active_jobs: int = 0
    last_ingestion_at: str | None = None


class JobSummary(BaseModel):
    """The compact posting representation used in list responses."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    normalized_title: str
    title_family: str
    company_name: str
    location: str
    country_code: str | None = None
    city: str | None = None
    remote_type: RemoteType
    employment_type: EmploymentType
    experience_level: ExperienceLevel
    segment: MarketSegment
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_provenance: str
    skills: list[str] = Field(default_factory=list)
    published_at: datetime | None = None
    first_seen_at: datetime
    url: str | None = None
    quality_score: float = 0.0
    score: float | None = None

    @classmethod
    def from_job(cls, job: NormalizedJob, *, score: float | None = None) -> JobSummary:
        return cls(
            id=job.id,
            title=job.title,
            normalized_title=job.normalized_title,
            title_family=job.title_family,
            company_name=job.company_name,
            location=job.location.display(),
            country_code=job.location.country_code,
            city=job.location.city,
            remote_type=job.remote_type,
            employment_type=job.employment_type,
            experience_level=job.experience_level,
            segment=job.segment,
            salary_min=job.salary.annual_min,
            salary_max=job.salary.annual_max,
            salary_currency=job.salary.normalized_currency or job.salary.currency,
            salary_provenance=str(job.salary.provenance),
            skills=job.skill_slugs,
            published_at=job.published_at,
            first_seen_at=job.first_seen_at,
            url=job.canonical_url,
            quality_score=job.quality_score,
            score=score,
        )


class JobDetail(JobSummary):
    """The full posting, including its description."""

    description: str = ""
    description_snippet: str = ""
    source: str = ""
    specialization: str | None = None
    experience_years_min: float | None = None
    experience_years_max: float | None = None
    industry: str | None = None
    language: str | None = None
    last_seen_at: datetime | None = None
    status: str = "active"

    @classmethod
    def from_job(cls, job: NormalizedJob, *, score: float | None = None) -> JobDetail:
        summary = JobSummary.from_job(job, score=score)
        return cls(
            **summary.model_dump(),
            description=job.description,
            description_snippet=job.description_snippet,
            source=job.source,
            specialization=job.specialization,
            experience_years_min=job.experience_years_min,
            experience_years_max=job.experience_years_max,
            industry=job.industry,
            language=job.language,
            last_seen_at=job.last_seen_at,
            status=str(job.status),
        )


class SearchMeta(BaseModel):
    """How a search was answered."""

    model_config = ConfigDict(extra="forbid")

    backend: str
    took_ms: float
    query: str | None = None


class SearchResult(BaseModel):
    """Search response envelope."""

    model_config = ConfigDict(extra="forbid")

    items: list[JobSummary]
    meta: PageMeta
    search: SearchMeta


class AlertRuleInput(BaseModel):
    """Payload for creating or updating an alert rule."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    metric: AlertMetric
    subject: str | None = Field(default=None, max_length=128)
    operator: ComparisonOperator = ComparisonOperator.GT
    threshold: float = 0.0
    window_days: int = Field(default=7, ge=1, le=365)
    channels: list[AlertChannel] = Field(default_factory=lambda: [AlertChannel.IN_APP])
    email_to: str | None = Field(default=None, max_length=254)
    webhook_url: HttpUrl | None = None
    cooldown_minutes: int = Field(default=60, ge=0, le=10_080)
    enabled: bool = True


class ProfileInput(BaseModel):
    """Payload for creating or updating a candidate profile."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(default="default", min_length=1, max_length=64)
    target_role: str = Field(min_length=2, max_length=128)
    skills: list[str] = Field(default_factory=list, max_length=200)
    experience_years: float = Field(default=0.0, ge=0, le=60)
    seniority: ExperienceLevel = ExperienceLevel.UNKNOWN
    countries: list[str] = Field(default_factory=list, max_length=50)
    cities: list[str] = Field(default_factory=list, max_length=50)
    salary_min: float | None = Field(default=None, ge=0)
    salary_max: float | None = Field(default=None, ge=0)
    salary_currency: str = Field(default="USD", min_length=3, max_length=3)
    remote_preference: RemoteType = RemoteType.UNKNOWN


class ApiKeyInput(BaseModel):
    """Payload for minting an API key."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    role: UserRole = UserRole.VIEWER
    scopes: list[Scope] | None = None
    expires_at: datetime | None = None


class ApiKeyCreated(BaseModel):
    """The one and only time the plaintext key is returned."""

    model_config = ConfigDict(extra="forbid")

    key_id: str
    api_key: str
    name: str
    role: UserRole
    scopes: list[str]
    warning: str = "Store this key now - it cannot be retrieved again."


class IngestionTriggerResponse(BaseModel):
    """Result of a manually triggered ingestion run."""

    model_config = ConfigDict(extra="forbid")

    source: str
    status: str
    records_received: int
    jobs_created: int
    jobs_updated: int
    duplicates_detected: int
    records_rejected: int
    quality_score: float
    duration_seconds: float
    error: str | None = None


#: Query parameter aliases shared by several routers.
Limit = Annotated[int, Field(ge=1, le=200)]
Offset = Annotated[int, Field(ge=0, le=100_000)]
WindowDays = Annotated[int, Field(ge=1, le=1825)]


__all__ = [
    "AlertRuleInput",
    "ApiKeyCreated",
    "ApiKeyInput",
    "ErrorBody",
    "ErrorResponse",
    "HealthResponse",
    "IngestionTriggerResponse",
    "JobDetail",
    "JobSummary",
    "Limit",
    "Offset",
    "Page",
    "PageMeta",
    "ProfileInput",
    "SearchMeta",
    "SearchResult",
    "WindowDays",
]
