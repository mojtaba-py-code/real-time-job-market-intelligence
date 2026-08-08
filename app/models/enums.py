"""Controlled vocabularies used across storage, analytics and the API.

Everything is a :class:`~enum.StrEnum` so values serialise to readable strings
in JSON, in Parquet and in the database without extra conversion code.
"""

from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    """Lifecycle state of a posting inside the pipeline."""

    DISCOVERED = "discovered"
    VALIDATED = "validated"
    NORMALIZED = "normalized"
    DEDUPLICATED = "deduplicated"
    ENRICHED = "enriched"
    ACTIVE = "active"
    UPDATED = "updated"
    EXPIRED = "expired"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        return self in (JobStatus.EXPIRED, JobStatus.REJECTED, JobStatus.DUPLICATE)

    @property
    def is_live(self) -> bool:
        return self in (JobStatus.ACTIVE, JobStatus.UPDATED)


#: Legal forward transitions of the job lifecycle.
JOB_STATUS_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.DISCOVERED: frozenset({JobStatus.VALIDATED, JobStatus.REJECTED}),
    JobStatus.VALIDATED: frozenset({JobStatus.NORMALIZED, JobStatus.REJECTED}),
    JobStatus.NORMALIZED: frozenset({JobStatus.DEDUPLICATED, JobStatus.REJECTED}),
    JobStatus.DEDUPLICATED: frozenset({JobStatus.ENRICHED, JobStatus.DUPLICATE}),
    JobStatus.ENRICHED: frozenset({JobStatus.ACTIVE}),
    JobStatus.ACTIVE: frozenset({JobStatus.UPDATED, JobStatus.EXPIRED}),
    JobStatus.UPDATED: frozenset({JobStatus.ACTIVE, JobStatus.UPDATED, JobStatus.EXPIRED}),
    JobStatus.EXPIRED: frozenset({JobStatus.ACTIVE}),
    JobStatus.DUPLICATE: frozenset(),
    JobStatus.REJECTED: frozenset(),
}


def can_transition(current: JobStatus, target: JobStatus) -> bool:
    """Return whether ``current -> target`` is an allowed lifecycle move."""
    return target in JOB_STATUS_TRANSITIONS.get(current, frozenset())


class RemoteType(StrEnum):
    """Work-location arrangement."""

    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class EmploymentType(StrEnum):
    """Contractual arrangement."""

    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    FREELANCE = "freelance"
    INTERNSHIP = "internship"
    TEMPORARY = "temporary"
    VOLUNTEER = "volunteer"
    UNKNOWN = "unknown"


class ExperienceLevel(StrEnum):
    """Seniority ladder, ordered by :data:`SENIORITY_ORDER`."""

    INTERN = "intern"
    ENTRY = "entry"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    LEAD = "lead"
    PRINCIPAL = "principal"
    MANAGER = "manager"
    DIRECTOR = "director"
    EXECUTIVE = "executive"
    UNKNOWN = "unknown"


#: Rank used when sorting or comparing seniority. ``UNKNOWN`` sorts last.
SENIORITY_ORDER: dict[ExperienceLevel, int] = {
    ExperienceLevel.INTERN: 0,
    ExperienceLevel.ENTRY: 1,
    ExperienceLevel.JUNIOR: 2,
    ExperienceLevel.MID: 3,
    ExperienceLevel.SENIOR: 4,
    ExperienceLevel.LEAD: 5,
    ExperienceLevel.PRINCIPAL: 6,
    ExperienceLevel.MANAGER: 7,
    ExperienceLevel.DIRECTOR: 8,
    ExperienceLevel.EXECUTIVE: 9,
    ExperienceLevel.UNKNOWN: 99,
}

#: Typical years-of-experience band per level, used to infer a level from text.
EXPERIENCE_YEAR_BANDS: dict[ExperienceLevel, tuple[float, float]] = {
    ExperienceLevel.INTERN: (0.0, 0.5),
    ExperienceLevel.ENTRY: (0.0, 1.0),
    ExperienceLevel.JUNIOR: (1.0, 3.0),
    ExperienceLevel.MID: (3.0, 5.0),
    ExperienceLevel.SENIOR: (5.0, 9.0),
    ExperienceLevel.LEAD: (7.0, 12.0),
    ExperienceLevel.PRINCIPAL: (9.0, 20.0),
}


class SalaryPeriod(StrEnum):
    """Payment cadence of a quoted salary."""

    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"
    UNKNOWN = "unknown"


#: Multipliers used to annualise a salary. ``UNKNOWN`` is intentionally absent.
ANNUALIZATION_FACTORS: dict[SalaryPeriod, float] = {
    SalaryPeriod.HOURLY: 2080.0,  # 40h * 52 weeks
    SalaryPeriod.DAILY: 260.0,
    SalaryPeriod.WEEKLY: 52.0,
    SalaryPeriod.MONTHLY: 12.0,
    SalaryPeriod.YEARLY: 1.0,
}


class SalaryProvenance(StrEnum):
    """Where a salary figure came from. Never guess silently."""

    OBSERVED = "observed"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class SkillCategory(StrEnum):
    """Top-level bucket of the skill taxonomy."""

    LANGUAGE = "language"
    FRAMEWORK = "framework"
    DATABASE = "database"
    CLOUD = "cloud"
    DEVOPS = "devops"
    DATA = "data"
    TESTING = "testing"
    SECURITY = "security"
    TOOL = "tool"
    PRACTICE = "practice"
    SOFT_SKILL = "soft_skill"
    OTHER = "other"


class MarketSegment(StrEnum):
    """Coarse market segmentation used for comparison dashboards."""

    BACKEND = "backend_engineering"
    FRONTEND = "frontend_engineering"
    FULLSTACK = "fullstack_engineering"
    DATA_ENGINEERING = "data_engineering"
    DATA_SCIENCE = "data_science"
    AI_ML = "ai_ml"
    DEVOPS = "devops"
    CLOUD = "cloud_engineering"
    CYBERSECURITY = "cybersecurity"
    QA = "qa"
    MOBILE = "mobile_development"
    EMBEDDED = "embedded"
    PRODUCT = "product"
    OTHER = "other"


class TrendDirection(StrEnum):
    """Classification produced by the trend engine."""

    RISING = "rising"
    STABLE = "stable"
    DECLINING = "declining"
    VOLATILE = "volatile"
    EMERGING = "emerging"
    INSUFFICIENT_DATA = "insufficient_data"


class DuplicateKind(StrEnum):
    """How a duplicate was detected."""

    NONE = "none"
    EXACT_SOURCE = "exact_source"
    URL = "url"
    CONTENT = "content"
    NEAR = "near"


class RejectionReason(StrEnum):
    """Why a raw record was quarantined instead of promoted."""

    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_FIELD = "invalid_field"
    TITLE_TOO_SHORT = "title_too_short"
    DESCRIPTION_TOO_SHORT = "description_too_short"
    UNPARSEABLE_DATE = "unparseable_date"
    UNSAFE_CONTENT = "unsafe_content"
    FUTURE_TIMESTAMP = "future_timestamp"
    STALE_POSTING = "stale_posting"
    SCHEMA_ERROR = "schema_error"
    NORMALIZATION_FAILED = "normalization_failed"
    UNKNOWN = "unknown"


class QualityDimension(StrEnum):
    """Data-quality dimensions measured per source and per batch."""

    COMPLETENESS = "completeness"
    ACCURACY = "accuracy"
    UNIQUENESS = "uniqueness"
    VALIDITY = "validity"
    CONSISTENCY = "consistency"
    FRESHNESS = "freshness"


#: Weights used to fold dimension scores into a single quality score.
QUALITY_DIMENSION_WEIGHTS: dict[QualityDimension, float] = {
    QualityDimension.COMPLETENESS: 0.25,
    QualityDimension.VALIDITY: 0.20,
    QualityDimension.UNIQUENESS: 0.15,
    QualityDimension.CONSISTENCY: 0.15,
    QualityDimension.ACCURACY: 0.15,
    QualityDimension.FRESHNESS: 0.10,
}


class IngestionStatus(StrEnum):
    """Outcome of an ingestion run."""

    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class SourceKind(StrEnum):
    """Category of a configured data source."""

    API = "api"
    RSS = "rss"
    CAREER_PAGE = "career_page"
    DATASET = "dataset"
    SYNTHETIC = "synthetic"


class EventType(StrEnum):
    """Message types published on the event bus."""

    JOB_DISCOVERED = "job.discovered"
    JOB_VALIDATED = "job.validated"
    JOB_NORMALIZED = "job.normalized"
    JOB_DUPLICATE = "job.duplicate"
    JOB_REJECTED = "job.rejected"
    JOB_CREATED = "job.created"
    JOB_UPDATED = "job.updated"
    JOB_EXPIRED = "job.expired"
    INGESTION_STARTED = "ingestion.started"
    INGESTION_COMPLETED = "ingestion.completed"
    ANALYTICS_REFRESHED = "analytics.refreshed"
    ALERT_TRIGGERED = "alert.triggered"


class AlertChannel(StrEnum):
    """Delivery channel for an alert notification."""

    EMAIL = "email"
    WEBHOOK = "webhook"
    IN_APP = "in_app"


class AlertMetric(StrEnum):
    """Quantity an alert rule watches."""

    SKILL_DEMAND_CHANGE_PCT = "skill_demand_change_pct"
    SKILL_JOB_COUNT = "skill_job_count"
    EMERGING_SKILL = "emerging_skill"
    COMPANY_JOB_COUNT = "company_job_count"
    AVERAGE_SALARY = "average_salary"
    REMOTE_SHARE_PCT = "remote_share_pct"
    TOTAL_ACTIVE_JOBS = "total_active_jobs"


class ComparisonOperator(StrEnum):
    """Comparison used by alert thresholds."""

    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    EQ = "eq"

    def compare(self, left: float, right: float) -> bool:
        """Evaluate ``left <op> right``."""
        match self:
            case ComparisonOperator.GT:
                return left > right
            case ComparisonOperator.GTE:
                return left >= right
            case ComparisonOperator.LT:
                return left < right
            case ComparisonOperator.LTE:
                return left <= right
            case ComparisonOperator.EQ:
                return left == right
        raise ValueError(f"unsupported operator: {self}")  # pragma: no cover


class UserRole(StrEnum):
    """Coarse role attached to an API principal."""

    ADMIN = "admin"
    ANALYST = "analyst"
    VIEWER = "viewer"


class Scope(StrEnum):
    """Fine-grained permissions granted to an API key."""

    JOBS_READ = "jobs:read"
    ANALYTICS_READ = "analytics:read"
    ALERTS_READ = "alerts:read"
    ALERTS_WRITE = "alerts:write"
    PROFILES_WRITE = "profiles:write"
    INGESTION_WRITE = "ingestion:write"
    ADMIN = "admin"


#: Default scope bundle granted to each role.
ROLE_SCOPES: dict[UserRole, frozenset[Scope]] = {
    UserRole.VIEWER: frozenset({Scope.JOBS_READ, Scope.ANALYTICS_READ}),
    UserRole.ANALYST: frozenset(
        {
            Scope.JOBS_READ,
            Scope.ANALYTICS_READ,
            Scope.ALERTS_READ,
            Scope.ALERTS_WRITE,
            Scope.PROFILES_WRITE,
        }
    ),
    UserRole.ADMIN: frozenset(Scope),
}


__all__ = [
    "ANNUALIZATION_FACTORS",
    "EXPERIENCE_YEAR_BANDS",
    "JOB_STATUS_TRANSITIONS",
    "QUALITY_DIMENSION_WEIGHTS",
    "ROLE_SCOPES",
    "SENIORITY_ORDER",
    "AlertChannel",
    "AlertMetric",
    "ComparisonOperator",
    "DuplicateKind",
    "EmploymentType",
    "EventType",
    "ExperienceLevel",
    "IngestionStatus",
    "JobStatus",
    "MarketSegment",
    "QualityDimension",
    "RejectionReason",
    "RemoteType",
    "SalaryPeriod",
    "SalaryProvenance",
    "Scope",
    "SkillCategory",
    "SourceKind",
    "TrendDirection",
    "UserRole",
    "can_transition",
]
