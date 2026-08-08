"""Relational schema for the operational database.

Design notes:

* A few columns (``company_name``, ``country_code``, ``city``) are deliberately
  denormalized onto ``jobs``. Analytics queries filter and group on them
  constantly, and the alternative - joining three tables for every dashboard
  widget - costs far more than the duplicated bytes.
* Every foreign key uses an explicit ``ondelete`` policy so that cleaning up a
  company or a source can never leave orphan rows behind.
* Enum-valued columns are stored as short strings: they survive schema
  evolution better than native database enums and are portable between SQLite
  and PostgreSQL.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.timeutils import utcnow
from app.models.enums import (
    DuplicateKind,
    EmploymentType,
    ExperienceLevel,
    IngestionStatus,
    JobStatus,
    MarketSegment,
    RemoteType,
    SalaryPeriod,
    SalaryProvenance,
    SkillCategory,
    SourceKind,
    TrendDirection,
    UserRole,
)
from app.storage.base import Base, TimestampType, json_column


def _uuid() -> str:
    return uuid.uuid4().hex


class Company(Base):
    """A hiring organisation."""

    __tablename__ = "companies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(256))
    industry: Mapped[str | None] = mapped_column(String(128), index=True)
    website: Mapped[str | None] = mapped_column(String(512))
    country_code: Mapped[str | None] = mapped_column(String(2), index=True)

    active_job_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_job_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    first_seen_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow)
    attributes: Mapped[dict[str, Any]] = json_column()

    jobs: Mapped[list[Job]] = relationship(back_populates="company", passive_deletes=True)


class Location(Base):
    """A normalized geography referenced by postings."""

    __tablename__ = "locations"
    __table_args__ = (
        UniqueConstraint("country_code", "region", "city", name="uq_locations_geo"),
        Index("ix_locations_country_city", "country_code", "city"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    country_code: Mapped[str | None] = mapped_column(String(2))
    country: Mapped[str | None] = mapped_column(String(96))
    region: Mapped[str | None] = mapped_column(String(96))
    city: Mapped[str | None] = mapped_column(String(96))
    slug: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    job_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class Skill(Base):
    """A node of the skill taxonomy."""

    __tablename__ = "skills"

    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(96))
    category: Mapped[str] = mapped_column(String(32), default=SkillCategory.OTHER, index=True)
    parent_slug: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("skills.slug", ondelete="SET NULL"), index=True
    )
    aliases: Mapped[list[str]] = json_column(default=list)
    description: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow)

    job_links: Mapped[list[JobSkill]] = relationship(back_populates="skill", passive_deletes=True)


class Job(Base):
    """A normalized job posting - the central table of the platform."""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("source", "source_job_id", name="uq_jobs_source_identity"),
        Index("ix_jobs_status_published", "status", "published_at"),
        Index("ix_jobs_status_first_seen", "status", "first_seen_at"),
        Index("ix_jobs_segment_status", "segment", "status"),
        Index("ix_jobs_country_status", "country_code", "status"),
        Index("ix_jobs_company_status", "company_id", "status"),
        CheckConstraint(
            "salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max",
            name="salary_bounds_ordered",
        ),
        CheckConstraint("quality_score >= 0 AND quality_score <= 1", name="quality_score_range"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)

    source: Mapped[str] = mapped_column(String(64), index=True)
    source_kind: Mapped[str] = mapped_column(String(32), default=SourceKind.API)
    source_job_id: Mapped[str] = mapped_column(String(256))
    canonical_url: Mapped[str | None] = mapped_column(String(2048))
    url_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)

    title: Mapped[str] = mapped_column(String(512))
    normalized_title: Mapped[str] = mapped_column(String(256), index=True)
    title_family: Mapped[str] = mapped_column(String(128), default="Other", index=True)
    specialization: Mapped[str | None] = mapped_column(String(128))
    title_confidence: Mapped[float] = mapped_column(Float, default=0.0)

    company_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("companies.id", ondelete="SET NULL"), index=True
    )
    company_name: Mapped[str] = mapped_column(String(256), default="Unknown", index=True)

    description: Mapped[str] = mapped_column(Text, default="")
    description_snippet: Mapped[str] = mapped_column(String(512), default="")
    search_text: Mapped[str] = mapped_column(Text, default="")

    location_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("locations.id", ondelete="SET NULL")
    )
    location_raw: Mapped[str | None] = mapped_column(String(256))
    country_code: Mapped[str | None] = mapped_column(String(2), index=True)
    country: Mapped[str | None] = mapped_column(String(96))
    region: Mapped[str | None] = mapped_column(String(96))
    city: Mapped[str | None] = mapped_column(String(96), index=True)

    remote_type: Mapped[str] = mapped_column(String(16), default=RemoteType.UNKNOWN, index=True)
    employment_type: Mapped[str] = mapped_column(String(24), default=EmploymentType.UNKNOWN)
    experience_level: Mapped[str] = mapped_column(
        String(16), default=ExperienceLevel.UNKNOWN, index=True
    )
    experience_years_min: Mapped[float | None] = mapped_column(Float)
    experience_years_max: Mapped[float | None] = mapped_column(Float)

    salary_min: Mapped[float | None] = mapped_column(Float)
    salary_max: Mapped[float | None] = mapped_column(Float)
    salary_currency: Mapped[str | None] = mapped_column(String(3))
    salary_period: Mapped[str] = mapped_column(String(16), default=SalaryPeriod.UNKNOWN)
    salary_provenance: Mapped[str] = mapped_column(
        String(16), default=SalaryProvenance.UNKNOWN, index=True
    )
    salary_annual_min: Mapped[float | None] = mapped_column(Float)
    salary_annual_max: Mapped[float | None] = mapped_column(Float)
    salary_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    salary_raw: Mapped[str | None] = mapped_column(String(256))

    industry: Mapped[str | None] = mapped_column(String(128))
    segment: Mapped[str] = mapped_column(String(32), default=MarketSegment.OTHER, index=True)
    language: Mapped[str | None] = mapped_column(String(8))

    published_at: Mapped[datetime | None] = mapped_column(TimestampType, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow, onupdate=utcnow)
    expired_at: Mapped[datetime | None] = mapped_column(TimestampType)

    status: Mapped[str] = mapped_column(String(16), default=JobStatus.ACTIVE, index=True)
    duplicate_of: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("jobs.id", ondelete="SET NULL"), index=True
    )
    duplicate_kind: Mapped[str] = mapped_column(String(16), default=DuplicateKind.NONE)
    content_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    similarity_signature: Mapped[list[int]] = json_column(default=list)

    quality_score: Mapped[float] = mapped_column(Float, default=0.0)
    attributes: Mapped[dict[str, Any]] = json_column()

    company: Mapped[Company | None] = relationship(back_populates="jobs")
    skill_links: Mapped[list[JobSkill]] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True
    )


class JobSkill(Base):
    """Association between a posting and a detected skill."""

    __tablename__ = "job_skills"
    __table_args__ = (
        Index("ix_job_skills_skill_job", "skill_slug", "job_id"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )

    job_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    skill_slug: Mapped[str] = mapped_column(
        String(64), ForeignKey("skills.slug", ondelete="CASCADE"), primary_key=True
    )
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    field: Mapped[str] = mapped_column(String(32), default="description")
    is_required: Mapped[bool | None] = mapped_column(Boolean)

    job: Mapped[Job] = relationship(back_populates="skill_links")
    skill: Mapped[Skill] = relationship(back_populates="job_links")


class SalaryRecord(Base):
    """Denormalized salary observation, optimised for salary analytics."""

    __tablename__ = "salary_records"
    __table_args__ = (
        Index("ix_salary_records_dims", "segment", "experience_level", "country_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("jobs.id", ondelete="CASCADE"), index=True
    )
    currency: Mapped[str] = mapped_column(String(3))
    period: Mapped[str] = mapped_column(String(16))
    min_amount: Mapped[float | None] = mapped_column(Float)
    max_amount: Mapped[float | None] = mapped_column(Float)
    annual_min: Mapped[float | None] = mapped_column(Float)
    annual_max: Mapped[float | None] = mapped_column(Float)
    annual_midpoint: Mapped[float | None] = mapped_column(Float, index=True)
    provenance: Mapped[str] = mapped_column(String(16), default=SalaryProvenance.OBSERVED)
    country_code: Mapped[str | None] = mapped_column(String(2))
    segment: Mapped[str] = mapped_column(String(32), default=MarketSegment.OTHER)
    experience_level: Mapped[str] = mapped_column(String(16), default=ExperienceLevel.UNKNOWN)
    observed_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow, index=True)


class JobEvent(Base):
    """Append-only audit trail of everything that happened to a posting."""

    __tablename__ = "job_events"
    __table_args__ = (Index("ix_job_events_type_time", "event_type", "occurred_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("jobs.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(32))
    occurred_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow, index=True)
    source: Mapped[str | None] = mapped_column(String(64))
    ingestion_run_id: Mapped[str | None] = mapped_column(String(32), index=True)
    payload: Mapped[dict[str, Any]] = json_column()


class JobSource(Base):
    """Per-source ingestion state (cursor + counters)."""

    __tablename__ = "job_sources"

    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_kind: Mapped[str] = mapped_column(String(32), default=SourceKind.API)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_successful_run_at: Mapped[datetime | None] = mapped_column(TimestampType)
    last_attempted_run_at: Mapped[datetime | None] = mapped_column(TimestampType)
    last_cursor: Mapped[str | None] = mapped_column(String(512))
    last_timestamp: Mapped[datetime | None] = mapped_column(TimestampType)
    etag: Mapped[str | None] = mapped_column(String(256))
    records_processed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    records_failed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    config: Mapped[dict[str, Any]] = json_column()


class IngestionRunRecord(Base):
    """Metrics for one ingestion run."""

    __tablename__ = "ingestion_runs"
    __table_args__ = (Index("ix_ingestion_runs_source_started", "source", "started_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    source: Mapped[str] = mapped_column(String(64), index=True)
    source_kind: Mapped[str] = mapped_column(String(32), default=SourceKind.API)
    status: Mapped[str] = mapped_column(String(16), default=IngestionStatus.RUNNING, index=True)
    started_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(TimestampType)

    records_received: Mapped[int] = mapped_column(Integer, default=0)
    records_validated: Mapped[int] = mapped_column(Integer, default=0)
    records_rejected: Mapped[int] = mapped_column(Integer, default=0)
    duplicates_detected: Mapped[int] = mapped_column(Integer, default=0)
    jobs_created: Mapped[int] = mapped_column(Integer, default=0)
    jobs_updated: Mapped[int] = mapped_column(Integer, default=0)
    jobs_expired: Mapped[int] = mapped_column(Integer, default=0)

    fetch_duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    processing_duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    nlp_duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    storage_duration_ms: Mapped[float] = mapped_column(Float, default=0.0)

    quality_score: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(String(1000))
    details: Mapped[dict[str, Any]] = json_column()


class RejectedRecordRow(Base):
    """Quarantine table - invalid records are stored, never silently dropped."""

    __tablename__ = "rejected_records"
    __table_args__ = (Index("ix_rejected_records_source_time", "source", "rejected_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    source_job_id: Mapped[str | None] = mapped_column(String(256))
    reason: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(String(1000))
    field: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = json_column()
    ingestion_run_id: Mapped[str | None] = mapped_column(String(32), index=True)
    rejected_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow, index=True)


class DataQualityEvent(Base):
    """A recorded data-quality violation."""

    __tablename__ = "data_quality_events"
    __table_args__ = (Index("ix_dq_events_source_dim", "source", "dimension"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    dimension: Mapped[str] = mapped_column(String(24))
    field: Mapped[str | None] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16), default="warning")
    message: Mapped[str] = mapped_column(String(1000))
    value: Mapped[float | None] = mapped_column(Float)
    threshold: Mapped[float | None] = mapped_column(Float)
    job_id: Mapped[str | None] = mapped_column(String(32))
    ingestion_run_id: Mapped[str | None] = mapped_column(String(32), index=True)
    occurred_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow, index=True)


class SkillTrendRow(Base):
    """Daily demand for a skill, the raw material of the trend engine."""

    __tablename__ = "skill_trends"
    __table_args__ = (
        UniqueConstraint("skill_slug", "day", name="uq_skill_trends_skill_day"),
        Index("ix_skill_trends_day", "day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    skill_slug: Mapped[str] = mapped_column(
        String(64), ForeignKey("skills.slug", ondelete="CASCADE"), index=True
    )
    day: Mapped[datetime] = mapped_column(TimestampType)
    job_count: Mapped[int] = mapped_column(Integer, default=0)
    share: Mapped[float] = mapped_column(Float, default=0.0)
    moving_average: Mapped[float | None] = mapped_column(Float)
    direction: Mapped[str] = mapped_column(String(24), default=TrendDirection.INSUFFICIENT_DATA)
    computed_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow)


class MarketSnapshot(Base):
    """A daily frozen picture of the market, used for fast dashboards."""

    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_date: Mapped[datetime] = mapped_column(TimestampType, unique=True, index=True)
    active_jobs: Mapped[int] = mapped_column(Integer, default=0)
    new_jobs: Mapped[int] = mapped_column(Integer, default=0)
    expired_jobs: Mapped[int] = mapped_column(Integer, default=0)
    companies_hiring: Mapped[int] = mapped_column(Integer, default=0)
    remote_share: Mapped[float] = mapped_column(Float, default=0.0)
    average_salary: Mapped[float | None] = mapped_column(Float)
    median_salary: Mapped[float | None] = mapped_column(Float)
    data_quality_score: Mapped[float] = mapped_column(Float, default=0.0)
    payload: Mapped[dict[str, Any]] = json_column()
    created_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow)


class ApiKey(Base):
    """An API credential. Only the Argon2 hash of the key is stored."""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    key_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    hashed_key: Mapped[str] = mapped_column(String(256))
    name: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16), default=UserRole.VIEWER)
    scopes: Mapped[list[str]] = json_column(default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(TimestampType)
    expires_at: Mapped[datetime | None] = mapped_column(TimestampType)


class AlertRuleRow(Base):
    """A persisted alert rule."""

    __tablename__ = "alert_rules"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), index=True)
    description: Mapped[str | None] = mapped_column(String(512))
    metric: Mapped[str] = mapped_column(String(48), index=True)
    subject: Mapped[str | None] = mapped_column(String(128), index=True)
    operator: Mapped[str] = mapped_column(String(8), default="gt")
    threshold: Mapped[float] = mapped_column(Float, default=0.0)
    window_days: Mapped[int] = mapped_column(Integer, default=7)
    channels: Mapped[list[str]] = json_column(default=list)
    email_to: Mapped[str | None] = mapped_column(String(254))
    webhook_url: Mapped[str | None] = mapped_column(String(2048))
    cooldown_minutes: Mapped[int] = mapped_column(Integer, default=60)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    owner: Mapped[str | None] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow)
    last_triggered_at: Mapped[datetime | None] = mapped_column(TimestampType)


class NotificationRow(Base):
    """A notification produced by the alert engine."""

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_rule_created", "rule_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    rule_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("alert_rules.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(String(4000))
    delivered: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    error: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow, index=True)
    read_at: Mapped[datetime | None] = mapped_column(TimestampType)
    context: Mapped[dict[str, Any]] = json_column()


class CandidateProfileRow(Base):
    """A stored candidate profile for personal market intelligence."""

    __tablename__ = "candidate_profiles"
    __table_args__ = (UniqueConstraint("owner", "label", name="uq_profiles_owner_label"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    owner: Mapped[str | None] = mapped_column(String(128), index=True)
    label: Mapped[str] = mapped_column(String(64), default="default")
    target_role: Mapped[str] = mapped_column(String(128))
    skills: Mapped[list[str]] = json_column(default=list)
    experience_years: Mapped[float] = mapped_column(Float, default=0.0)
    seniority: Mapped[str] = mapped_column(String(16), default=ExperienceLevel.UNKNOWN)
    countries: Mapped[list[str]] = json_column(default=list)
    cities: Mapped[list[str]] = json_column(default=list)
    salary_min: Mapped[float | None] = mapped_column(Float)
    salary_max: Mapped[float | None] = mapped_column(Float)
    salary_currency: Mapped[str] = mapped_column(String(3), default="USD")
    remote_preference: Mapped[str] = mapped_column(String(16), default=RemoteType.UNKNOWN)
    created_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TimestampType, default=utcnow, onupdate=utcnow)


#: Convenience export used by migrations and the ``duckdb`` bridge.
ALL_TABLES = (
    Company.__table__,
    Location.__table__,
    Skill.__table__,
    Job.__table__,
    JobSkill.__table__,
    SalaryRecord.__table__,
    JobEvent.__table__,
    JobSource.__table__,
    IngestionRunRecord.__table__,
    RejectedRecordRow.__table__,
    DataQualityEvent.__table__,
    SkillTrendRow.__table__,
    MarketSnapshot.__table__,
    ApiKey.__table__,
    AlertRuleRow.__table__,
    NotificationRow.__table__,
    CandidateProfileRow.__table__,
)

__all__ = [
    "ALL_TABLES",
    "AlertRuleRow",
    "ApiKey",
    "CandidateProfileRow",
    "Company",
    "DataQualityEvent",
    "IngestionRunRecord",
    "Job",
    "JobEvent",
    "JobSkill",
    "JobSource",
    "Location",
    "MarketSnapshot",
    "NotificationRow",
    "RejectedRecordRow",
    "SalaryRecord",
    "Skill",
    "SkillTrendRow",
]
