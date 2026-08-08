"""Ingestion bookkeeping: source cursors and per-run statistics.

Incremental ingestion depends on remembering where each source left off, and
observability depends on measuring what every run actually did.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.timeutils import utcnow
from app.models.enums import IngestionStatus, SourceKind


class SourceState(BaseModel):
    """Persisted cursor for one source, enabling incremental ingestion."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=64)
    source_kind: SourceKind = SourceKind.API
    enabled: bool = True
    last_successful_run_at: datetime | None = None
    last_attempted_run_at: datetime | None = None
    last_cursor: str | None = Field(default=None, max_length=512)
    last_timestamp: datetime | None = None
    records_processed: int = Field(default=0, ge=0)
    records_failed: int = Field(default=0, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)
    etag: str | None = Field(default=None, max_length=256)
    config: dict[str, Any] = Field(default_factory=dict)

    def advance(
        self,
        *,
        cursor: str | None,
        timestamp: datetime | None,
        processed: int,
        failed: int,
        etag: str | None = None,
    ) -> None:
        """Record a successful run and move the cursor forward."""
        now = utcnow()
        self.last_successful_run_at = now
        self.last_attempted_run_at = now
        if cursor is not None:
            self.last_cursor = cursor
        if timestamp is not None and (
            self.last_timestamp is None or timestamp > self.last_timestamp
        ):
            self.last_timestamp = timestamp
        if etag is not None:
            self.etag = etag
        self.records_processed += processed
        self.records_failed += failed
        self.consecutive_failures = 0

    def record_failure(self) -> None:
        """Record a failed run without moving the cursor."""
        self.last_attempted_run_at = utcnow()
        self.consecutive_failures += 1

    def is_circuit_open(self, *, threshold: int = 5) -> bool:
        """Whether the source has failed often enough to be paused."""
        return self.consecutive_failures >= threshold


class IngestionRun(BaseModel):
    """Metrics for a single ingestion + processing run."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, max_length=32)
    source: str
    source_kind: SourceKind = SourceKind.API
    status: IngestionStatus = IngestionStatus.RUNNING
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None

    records_received: int = Field(default=0, ge=0)
    records_validated: int = Field(default=0, ge=0)
    records_rejected: int = Field(default=0, ge=0)
    duplicates_detected: int = Field(default=0, ge=0)
    jobs_created: int = Field(default=0, ge=0)
    jobs_updated: int = Field(default=0, ge=0)
    jobs_expired: int = Field(default=0, ge=0)

    fetch_duration_ms: float = Field(default=0.0, ge=0)
    processing_duration_ms: float = Field(default=0.0, ge=0)
    nlp_duration_ms: float = Field(default=0.0, ge=0)
    storage_duration_ms: float = Field(default=0.0, ge=0)

    quality_score: float = Field(default=0.0, ge=0.0, le=1.0)
    error: str | None = Field(default=None, max_length=1000)
    details: dict[str, Any] = Field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        end = self.finished_at or utcnow()
        return max(0.0, (end - self.started_at).total_seconds())

    @property
    def throughput_per_second(self) -> float:
        duration = self.duration_seconds
        return self.records_received / duration if duration > 0 else 0.0

    def finish(self, status: IngestionStatus, *, error: str | None = None) -> None:
        """Close the run with a terminal status."""
        self.status = status
        self.finished_at = utcnow()
        if error:
            self.error = error[:1000]

    def merge(self, other: IngestionRun) -> None:
        """Fold another run's counters into this one (multi-source runs)."""
        self.records_received += other.records_received
        self.records_validated += other.records_validated
        self.records_rejected += other.records_rejected
        self.duplicates_detected += other.duplicates_detected
        self.jobs_created += other.jobs_created
        self.jobs_updated += other.jobs_updated
        self.jobs_expired += other.jobs_expired
        self.fetch_duration_ms += other.fetch_duration_ms
        self.processing_duration_ms += other.processing_duration_ms
        self.nlp_duration_ms += other.nlp_duration_ms
        self.storage_duration_ms += other.storage_duration_ms


__all__ = ["IngestionRun", "SourceState"]
