"""Translation between ORM rows and domain models.

Keeping the mapping in one place means the ORM never leaks into the analytics
or API layers, and the domain model never has to know about SQLAlchemy.
"""

from __future__ import annotations

from typing import Any

from app.core.text import canonical_key, slugify
from app.models.alerts import AlertRule, Notification
from app.models.enums import (
    AlertChannel,
    DuplicateKind,
    EmploymentType,
    ExperienceLevel,
    JobStatus,
    MarketSegment,
    RemoteType,
    SalaryPeriod,
    SalaryProvenance,
    SourceKind,
)
from app.models.ingestion import IngestionRun, SourceState
from app.models.job import ExtractedSkill, LocationInfo, NormalizedJob, SalaryInfo
from app.models.profiles import CandidateProfile
from app.storage.models import (
    AlertRuleRow,
    CandidateProfileRow,
    IngestionRunRecord,
    Job,
    JobSkill,
    JobSource,
    NotificationRow,
)


def build_search_text(job: NormalizedJob) -> str:
    """Concatenate the fields the full-text search index should cover."""
    parts = [
        job.title,
        job.normalized_title,
        job.company_name,
        job.location.display(),
        " ".join(s.name for s in job.skills),
        job.description_snippet,
    ]
    return canonical_key(" ".join(p for p in parts if p))[:8000]


def job_to_values(job: NormalizedJob, *, company_id: str | None = None) -> dict[str, Any]:
    """Flatten a :class:`NormalizedJob` into ORM column values."""
    return {
        "id": job.id,
        "source": job.source,
        "source_kind": str(job.source_kind),
        "source_job_id": job.source_job_id,
        "canonical_url": job.canonical_url,
        "url_fingerprint": job.url_fingerprint,
        "title": job.title,
        "normalized_title": job.normalized_title,
        "title_family": job.title_family,
        "specialization": job.specialization,
        "title_confidence": job.title_confidence,
        "company_id": company_id,
        "company_name": job.company_name,
        "description": job.description,
        "description_snippet": job.description_snippet,
        "search_text": build_search_text(job),
        "location_raw": job.location.raw,
        "country_code": job.location.country_code,
        "country": job.location.country,
        "region": job.location.region,
        "city": job.location.city,
        "remote_type": str(job.remote_type),
        "employment_type": str(job.employment_type),
        "experience_level": str(job.experience_level),
        "experience_years_min": job.experience_years_min,
        "experience_years_max": job.experience_years_max,
        "salary_min": job.salary.min_amount,
        "salary_max": job.salary.max_amount,
        "salary_currency": job.salary.currency,
        "salary_period": str(job.salary.period),
        "salary_provenance": str(job.salary.provenance),
        "salary_annual_min": job.salary.annual_min,
        "salary_annual_max": job.salary.annual_max,
        "salary_confidence": job.salary.confidence,
        "salary_raw": job.salary.raw,
        "industry": job.industry,
        "segment": str(job.segment),
        "language": job.language,
        "published_at": job.published_at,
        "first_seen_at": job.first_seen_at,
        "last_seen_at": job.last_seen_at,
        "updated_at": job.updated_at,
        "expired_at": job.expired_at,
        "status": str(job.status),
        "duplicate_of": job.duplicate_of,
        "duplicate_kind": str(job.duplicate_kind),
        "content_hash": job.content_hash,
        "similarity_signature": list(job.similarity_signature),
        "quality_score": job.quality_score,
        "attributes": dict(job.metadata),
    }


def job_to_domain(row: Job, skills: list[JobSkill] | None = None) -> NormalizedJob:
    """Rebuild a :class:`NormalizedJob` from its ORM row."""
    extracted: list[ExtractedSkill] = []
    for link in skills or []:
        name = link.skill.name if link.skill is not None else link.skill_slug
        category = link.skill.category if link.skill is not None else "other"
        extracted.append(
            ExtractedSkill(
                slug=link.skill_slug,
                name=name,
                category=category,  # type: ignore[arg-type]
                confidence=link.confidence,
                occurrences=link.occurrences,
                field=link.field,
                is_required=link.is_required,
            )
        )

    return NormalizedJob(
        id=row.id,
        source=row.source,
        source_kind=SourceKind(row.source_kind),
        source_job_id=row.source_job_id,
        canonical_url=row.canonical_url,
        url_fingerprint=row.url_fingerprint,
        title=row.title,
        normalized_title=row.normalized_title,
        title_family=row.title_family,
        specialization=row.specialization,
        title_confidence=row.title_confidence,
        company_name=row.company_name,
        company_slug=slugify(row.company_name),
        description=row.description,
        description_snippet=row.description_snippet,
        location=LocationInfo(
            raw=row.location_raw,
            country=row.country,
            country_code=row.country_code,
            region=row.region,
            city=row.city,
        ),
        remote_type=RemoteType(row.remote_type),
        employment_type=EmploymentType(row.employment_type),
        experience_level=ExperienceLevel(row.experience_level),
        experience_years_min=row.experience_years_min,
        experience_years_max=row.experience_years_max,
        salary=SalaryInfo(
            raw=row.salary_raw,
            min_amount=row.salary_min,
            max_amount=row.salary_max,
            currency=row.salary_currency,
            period=SalaryPeriod(row.salary_period),
            provenance=SalaryProvenance(row.salary_provenance),
            annual_min=row.salary_annual_min,
            annual_max=row.salary_annual_max,
            confidence=row.salary_confidence,
        ),
        industry=row.industry,
        segment=MarketSegment(row.segment),
        skills=extracted,
        published_at=row.published_at,
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        updated_at=row.updated_at,
        expired_at=row.expired_at,
        status=JobStatus(row.status),
        duplicate_of=row.duplicate_of,
        duplicate_kind=DuplicateKind(row.duplicate_kind),
        content_hash=row.content_hash,
        similarity_signature=list(row.similarity_signature or []),
        quality_score=row.quality_score,
        language=row.language,
        metadata=dict(row.attributes or {}),
    )


def source_state_to_values(state: SourceState) -> dict[str, Any]:
    """Flatten a source cursor into ORM column values."""
    return {
        "source": state.source,
        "source_kind": str(state.source_kind),
        "enabled": state.enabled,
        "last_successful_run_at": state.last_successful_run_at,
        "last_attempted_run_at": state.last_attempted_run_at,
        "last_cursor": state.last_cursor,
        "last_timestamp": state.last_timestamp,
        "etag": state.etag,
        "records_processed": state.records_processed,
        "records_failed": state.records_failed,
        "consecutive_failures": state.consecutive_failures,
        "config": dict(state.config),
    }


def source_state_to_domain(row: JobSource) -> SourceState:
    """Rebuild a source cursor from its ORM row."""
    return SourceState(
        source=row.source,
        source_kind=SourceKind(row.source_kind),
        enabled=row.enabled,
        last_successful_run_at=row.last_successful_run_at,
        last_attempted_run_at=row.last_attempted_run_at,
        last_cursor=row.last_cursor,
        last_timestamp=row.last_timestamp,
        etag=row.etag,
        records_processed=row.records_processed,
        records_failed=row.records_failed,
        consecutive_failures=row.consecutive_failures,
        config=dict(row.config or {}),
    )


def ingestion_run_to_values(run: IngestionRun) -> dict[str, Any]:
    """Flatten run metrics into ORM column values."""
    return {
        "id": run.id,
        "source": run.source,
        "source_kind": str(run.source_kind),
        "status": str(run.status),
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "records_received": run.records_received,
        "records_validated": run.records_validated,
        "records_rejected": run.records_rejected,
        "duplicates_detected": run.duplicates_detected,
        "jobs_created": run.jobs_created,
        "jobs_updated": run.jobs_updated,
        "jobs_expired": run.jobs_expired,
        "fetch_duration_ms": run.fetch_duration_ms,
        "processing_duration_ms": run.processing_duration_ms,
        "nlp_duration_ms": run.nlp_duration_ms,
        "storage_duration_ms": run.storage_duration_ms,
        "quality_score": run.quality_score,
        "error": run.error,
        "details": dict(run.details),
    }


def ingestion_run_to_domain(row: IngestionRunRecord) -> IngestionRun:
    """Rebuild run metrics from an ORM row."""
    return IngestionRun.model_validate(
        {
            "id": row.id,
            "source": row.source,
            "source_kind": row.source_kind,
            "status": row.status,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
            "records_received": row.records_received,
            "records_validated": row.records_validated,
            "records_rejected": row.records_rejected,
            "duplicates_detected": row.duplicates_detected,
            "jobs_created": row.jobs_created,
            "jobs_updated": row.jobs_updated,
            "jobs_expired": row.jobs_expired,
            "fetch_duration_ms": row.fetch_duration_ms,
            "processing_duration_ms": row.processing_duration_ms,
            "nlp_duration_ms": row.nlp_duration_ms,
            "storage_duration_ms": row.storage_duration_ms,
            "quality_score": row.quality_score,
            "error": row.error,
            "details": dict(row.details or {}),
        }
    )


def alert_rule_to_values(rule: AlertRule) -> dict[str, Any]:
    """Flatten an alert rule into ORM column values."""
    return {
        "id": rule.id,
        "name": rule.name,
        "description": rule.description,
        "metric": str(rule.metric),
        "subject": rule.subject,
        "operator": str(rule.operator),
        "threshold": rule.threshold,
        "window_days": rule.window_days,
        "channels": [str(c) for c in rule.channels],
        "email_to": rule.email_to,
        "webhook_url": str(rule.webhook_url) if rule.webhook_url else None,
        "cooldown_minutes": rule.cooldown_minutes,
        "enabled": rule.enabled,
        "owner": rule.owner,
        "created_at": rule.created_at,
        "last_triggered_at": rule.last_triggered_at,
    }


def alert_rule_to_domain(row: AlertRuleRow) -> AlertRule:
    """Rebuild an alert rule from its ORM row."""
    return AlertRule.model_validate(
        {
            "id": row.id,
            "name": row.name,
            "description": row.description,
            "metric": row.metric,
            "subject": row.subject,
            "operator": row.operator,
            "threshold": row.threshold,
            "window_days": row.window_days,
            "channels": [AlertChannel(c) for c in (row.channels or [])],
            "email_to": row.email_to,
            "webhook_url": row.webhook_url,
            "cooldown_minutes": row.cooldown_minutes,
            "enabled": row.enabled,
            "owner": row.owner,
            "created_at": row.created_at,
            "last_triggered_at": row.last_triggered_at,
        }
    )


def notification_to_domain(row: NotificationRow) -> Notification:
    """Rebuild a notification from its ORM row."""
    return Notification.model_validate(
        {
            "id": row.id,
            "rule_id": row.rule_id,
            "channel": row.channel,
            "title": row.title,
            "body": row.body,
            "delivered": row.delivered,
            "error": row.error,
            "created_at": row.created_at,
            "read_at": row.read_at,
            "context": dict(row.context or {}),
        }
    )


def profile_to_values(profile: CandidateProfile) -> dict[str, Any]:
    """Flatten a candidate profile into ORM column values."""
    return {
        "id": profile.id,
        "owner": profile.owner,
        "label": profile.label,
        "target_role": profile.target_role,
        "skills": list(profile.skills),
        "experience_years": profile.experience_years,
        "seniority": str(profile.seniority),
        "countries": list(profile.countries),
        "cities": list(profile.cities),
        "salary_min": profile.salary_min,
        "salary_max": profile.salary_max,
        "salary_currency": profile.salary_currency,
        "remote_preference": str(profile.remote_preference),
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
    }


def profile_to_domain(row: CandidateProfileRow) -> CandidateProfile:
    """Rebuild a candidate profile from its ORM row."""
    return CandidateProfile.model_validate(
        {
            "id": row.id,
            "owner": row.owner,
            "label": row.label,
            "target_role": row.target_role,
            "skills": list(row.skills or []),
            "experience_years": row.experience_years,
            "seniority": row.seniority,
            "countries": list(row.countries or []),
            "cities": list(row.cities or []),
            "salary_min": row.salary_min,
            "salary_max": row.salary_max,
            "salary_currency": row.salary_currency,
            "remote_preference": row.remote_preference,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
    )


__all__ = [
    "alert_rule_to_domain",
    "alert_rule_to_values",
    "build_search_text",
    "ingestion_run_to_domain",
    "ingestion_run_to_values",
    "job_to_domain",
    "job_to_values",
    "notification_to_domain",
    "profile_to_domain",
    "profile_to_values",
    "source_state_to_domain",
    "source_state_to_values",
]
