"""Repository layer - the only place that speaks SQL."""

from __future__ import annotations

from app.storage.repositories.alerts import AlertRepository
from app.storage.repositories.analytics import AnalyticsRepository
from app.storage.repositories.auth import ApiKeyRepository, Principal
from app.storage.repositories.base import BaseRepository
from app.storage.repositories.ingestion import (
    IngestionRunRepository,
    RejectedRecordRepository,
    SourceStateRepository,
)
from app.storage.repositories.jobs import JobQuery, JobRepository, UpsertResult
from app.storage.repositories.profiles import ProfileRepository
from app.storage.repositories.quality import (
    MarketSnapshotRepository,
    QualityRepository,
    SkillTrendRepository,
)
from app.storage.repositories.reference import (
    CompanyRepository,
    LocationRepository,
    SkillRepository,
)

__all__ = [
    "AlertRepository",
    "AnalyticsRepository",
    "ApiKeyRepository",
    "BaseRepository",
    "CompanyRepository",
    "IngestionRunRepository",
    "JobQuery",
    "JobRepository",
    "LocationRepository",
    "MarketSnapshotRepository",
    "Principal",
    "ProfileRepository",
    "QualityRepository",
    "RejectedRecordRepository",
    "SkillRepository",
    "SkillTrendRepository",
    "SourceStateRepository",
    "UpsertResult",
]
