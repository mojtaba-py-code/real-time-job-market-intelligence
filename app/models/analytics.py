"""Analytical value objects returned by the analytics engines and the API.

These models are pure data - no persistence, no I/O - which keeps the analytics
engines testable in isolation and lets the API serialise them directly.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.timeutils import utcnow
from app.models.enums import (
    ExperienceLevel,
    MarketSegment,
    RemoteType,
    SkillCategory,
    TrendDirection,
)

Ratio = Annotated[float, Field(ge=0.0, le=1.0)]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TimeSeriesPoint(_Frozen):
    """One observation in a daily time series."""

    day: date
    value: float
    count: int = 0


class VolumeSeries(_Frozen):
    """Job volume over time with derived aggregates."""

    granularity: str = "day"
    points: list[TimeSeriesPoint] = Field(default_factory=list)
    total: int = 0
    average_per_period: float = 0.0
    moving_average: list[TimeSeriesPoint] = Field(default_factory=list)


class SkillDemand(_Frozen):
    """Demand for a single skill within a window."""

    slug: str
    name: str
    category: SkillCategory = SkillCategory.OTHER
    job_count: int = 0
    share: Ratio = 0.0
    rank: int | None = None
    average_confidence: float = 0.0


class TrendWindow(_Frozen):
    """Change of a metric across one look-back window."""

    days: int
    current_count: int
    previous_count: int
    change_pct: float | None
    direction: TrendDirection = TrendDirection.INSUFFICIENT_DATA


class SkillTrend(_Frozen):
    """Multi-window trend summary for a skill."""

    slug: str
    name: str
    category: SkillCategory = SkillCategory.OTHER
    windows: list[TrendWindow] = Field(default_factory=list)
    direction: TrendDirection = TrendDirection.INSUFFICIENT_DATA
    moving_average: float | None = None
    volatility: float | None = None
    series: list[TimeSeriesPoint] = Field(default_factory=list)
    sample_size: int = 0
    confidence: Ratio = 0.0


class EmergingSkill(_Frozen):
    """A technology whose demand grew sharply from a small base."""

    slug: str
    name: str
    category: SkillCategory = SkillCategory.OTHER
    current_count: int
    previous_count: int
    growth_pct: float | None
    baseline_share: float
    z_score: float | None = None
    confidence: Ratio = 0.0


class SkillCooccurrence(_Frozen):
    """Association strength between two skills."""

    skill_a: str
    skill_b: str
    cooccurrence_count: int
    support: float
    confidence_a_to_b: float
    confidence_b_to_a: float
    lift: float
    association_score: float


class SkillGraph(_Frozen):
    """Skill relationship graph (nodes + weighted undirected edges)."""

    nodes: list[SkillDemand] = Field(default_factory=list)
    edges: list[SkillCooccurrence] = Field(default_factory=list)
    total_jobs: int = 0


class SalaryStatistics(_Frozen):
    """Descriptive statistics over annualised, observed salaries."""

    sample_size: int = 0
    currency: str = "USD"
    average: float | None = None
    median: float | None = None
    p25: float | None = None
    p75: float | None = None
    p90: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    observed_share: Ratio = 0.0


class SalaryBreakdown(_Frozen):
    """Salary statistics grouped by an arbitrary dimension."""

    dimension: str
    key: str
    label: str
    statistics: SalaryStatistics


class LocationDemand(_Frozen):
    """Job demand for a geography."""

    country_code: str | None = None
    country: str | None = None
    city: str | None = None
    region: str | None = None
    job_count: int = 0
    share: Ratio = 0.0
    remote_share: Ratio = 0.0
    average_salary: float | None = None


class RemoteBreakdown(_Frozen):
    """Distribution of work arrangements."""

    counts: dict[RemoteType, int] = Field(default_factory=dict)
    total: int = 0
    remote_share: Ratio = 0.0
    hybrid_share: Ratio = 0.0
    onsite_share: Ratio = 0.0
    unknown_share: Ratio = 0.0
    change_pct: float | None = None


class SeniorityBreakdown(_Frozen):
    """Distribution of experience levels."""

    counts: dict[ExperienceLevel, int] = Field(default_factory=dict)
    total: int = 0
    shares: dict[ExperienceLevel, float] = Field(default_factory=dict)
    average_experience_years: float | None = None


class CompanyHiring(_Frozen):
    """Hiring profile of one company."""

    company_id: str
    name: str
    slug: str
    industry: str | None = None
    active_job_count: int = 0
    total_job_count: int = 0
    jobs_last_30_days: int = 0
    jobs_previous_30_days: int = 0
    hiring_growth_pct: float | None = None
    remote_share: Ratio = 0.0
    top_skills: list[SkillDemand] = Field(default_factory=list)
    top_locations: list[str] = Field(default_factory=list)
    salary: SalaryStatistics | None = None


class SegmentSummary(_Frozen):
    """Aggregate view of one market segment."""

    segment: MarketSegment
    job_count: int = 0
    share: Ratio = 0.0
    remote_share: Ratio = 0.0
    median_salary: float | None = None
    top_skills: list[SkillDemand] = Field(default_factory=list)
    growth_pct: float | None = None


class MarketOverview(_Frozen):
    """The headline numbers shown on the dashboard."""

    generated_at: datetime = Field(default_factory=utcnow)
    window_days: int = 30
    active_jobs: int = 0
    new_jobs_today: int = 0
    new_jobs_this_week: int = 0
    total_jobs: int = 0
    companies_hiring: int = 0
    countries_covered: int = 0
    top_skill: SkillDemand | None = None
    fastest_growing_skill: SkillTrend | None = None
    average_salary: float | None = None
    median_salary: float | None = None
    remote_share: Ratio = 0.0
    data_quality_score: Ratio = 0.0
    volume: VolumeSeries | None = None


class SkillExplorerView(_Frozen):
    """Everything the dashboard's skill explorer needs in one payload."""

    skill: SkillDemand
    trend: SkillTrend
    related_skills: list[SkillCooccurrence] = Field(default_factory=list)
    top_companies: list[CompanyHiring] = Field(default_factory=list)
    top_locations: list[LocationDemand] = Field(default_factory=list)
    salary: SalaryStatistics | None = None
    remote: RemoteBreakdown | None = None
    seniority: SeniorityBreakdown | None = None


class MarketFitResult(_Frozen):
    """Personalised market analysis for a candidate profile.

    This is an analytical recommendation derived from public postings - it is
    explicitly not a prediction of employment outcomes.
    """

    target_role: str
    matching_jobs: int = 0
    market_fit_score: Ratio = 0.0
    skill_coverage: Ratio = 0.0
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[SkillDemand] = Field(default_factory=list)
    salary: SalaryStatistics | None = None
    remote_share: Ratio = 0.0
    top_locations: list[LocationDemand] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class AnalyticsFilter(BaseModel):
    """Common filter accepted by every analytics engine."""

    model_config = ConfigDict(extra="forbid")

    window_days: int = Field(default=30, ge=1, le=1825)
    date_from: datetime | None = None
    date_to: datetime | None = None
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    city: str | None = None
    company_slug: str | None = None
    segment: MarketSegment | None = None
    seniority: ExperienceLevel | None = None
    remote_type: RemoteType | None = None
    skill: str | None = None
    source: str | None = None
    limit: int = Field(default=25, ge=1, le=500)

    def cache_key(self) -> str:
        """Deterministic key for caching analytics responses."""
        payload: dict[str, Any] = self.model_dump(exclude_none=True, mode="json")
        return "|".join(f"{k}={payload[k]}" for k in sorted(payload))


__all__ = [
    "AnalyticsFilter",
    "CompanyHiring",
    "EmergingSkill",
    "LocationDemand",
    "MarketFitResult",
    "MarketOverview",
    "RemoteBreakdown",
    "SalaryBreakdown",
    "SalaryStatistics",
    "SegmentSummary",
    "SeniorityBreakdown",
    "SkillCooccurrence",
    "SkillDemand",
    "SkillExplorerView",
    "SkillGraph",
    "SkillTrend",
    "TimeSeriesPoint",
    "TrendWindow",
    "VolumeSeries",
]
