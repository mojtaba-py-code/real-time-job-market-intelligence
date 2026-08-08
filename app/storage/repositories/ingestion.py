"""Persistence for ingestion state, run metrics and quarantined records."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, func, insert, select, update

from app.core.timeutils import utcnow
from app.models.enums import IngestionStatus, SourceKind
from app.models.ingestion import IngestionRun, SourceState
from app.models.raw import RejectedRecord
from app.storage.mappers import (
    ingestion_run_to_domain,
    ingestion_run_to_values,
    source_state_to_domain,
    source_state_to_values,
)
from app.storage.models import IngestionRunRecord, JobSource, RejectedRecordRow
from app.storage.repositories.base import BaseRepository


class SourceStateRepository(BaseRepository):
    """Reads and writes the per-source ingestion cursor."""

    async def get(self, source: str) -> SourceState | None:
        row = await self.session.get(JobSource, source)
        return source_state_to_domain(row) if row else None

    async def get_or_create(self, source: str, kind: SourceKind) -> SourceState:
        """Return the stored cursor, creating an empty one on first sight."""
        existing = await self.get(source)
        if existing is not None:
            return existing
        state = SourceState(source=source, source_kind=kind)
        self.session.add(JobSource(**source_state_to_values(state)))
        await self.session.flush()
        return state

    async def save(self, state: SourceState) -> None:
        """Persist a cursor (insert or update)."""
        values = source_state_to_values(state)
        row = await self.session.get(JobSource, state.source)
        if row is None:
            self.session.add(JobSource(**values))
        else:
            for key, value in values.items():
                setattr(row, key, value)
        await self.session.flush()

    async def list_all(self, *, enabled_only: bool = False) -> list[SourceState]:
        stmt = select(JobSource).order_by(JobSource.source)
        if enabled_only:
            stmt = stmt.where(JobSource.enabled.is_(True))
        rows = (await self.session.execute(stmt)).scalars()
        return [source_state_to_domain(row) for row in rows]

    async def set_enabled(self, source: str, enabled: bool) -> bool:
        result = await self.session.execute(
            update(JobSource).where(JobSource.source == source).values(enabled=enabled)
        )
        return bool(result.rowcount)


class IngestionRunRepository(BaseRepository):
    """Stores metrics for every ingestion run."""

    async def create(self, run: IngestionRun) -> None:
        self.session.add(IngestionRunRecord(**ingestion_run_to_values(run)))
        await self.session.flush()

    async def save(self, run: IngestionRun) -> None:
        """Insert or update run metrics."""
        values = ingestion_run_to_values(run)
        row = await self.session.get(IngestionRunRecord, run.id)
        if row is None:
            self.session.add(IngestionRunRecord(**values))
        else:
            for key, value in values.items():
                setattr(row, key, value)
        await self.session.flush()

    async def get(self, run_id: str) -> IngestionRun | None:
        row = await self.session.get(IngestionRunRecord, run_id)
        return ingestion_run_to_domain(row) if row else None

    async def list_recent(
        self, *, source: str | None = None, limit: int = 20
    ) -> list[IngestionRun]:
        stmt = (
            select(IngestionRunRecord).order_by(IngestionRunRecord.started_at.desc()).limit(limit)
        )
        if source:
            stmt = stmt.where(IngestionRunRecord.source == source)
        rows = (await self.session.execute(stmt)).scalars()
        return [ingestion_run_to_domain(row) for row in rows]

    async def last_successful(self, source: str) -> IngestionRun | None:
        stmt = (
            select(IngestionRunRecord)
            .where(IngestionRunRecord.source == source)
            .where(IngestionRunRecord.status == IngestionStatus.SUCCESS.value)
            .order_by(IngestionRunRecord.started_at.desc())
            .limit(1)
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        return ingestion_run_to_domain(row) if row else None

    async def prune(self, *, older_than: datetime) -> int:
        result = await self.session.execute(
            delete(IngestionRunRecord).where(IngestionRunRecord.started_at < older_than)
        )
        return result.rowcount or 0

    async def summary(self, *, since: datetime) -> dict[str, float]:
        """Aggregate throughput metrics over recent runs."""
        stmt = select(
            func.count(),
            func.sum(IngestionRunRecord.records_received),
            func.sum(IngestionRunRecord.records_rejected),
            func.sum(IngestionRunRecord.jobs_created),
            func.sum(IngestionRunRecord.jobs_updated),
            func.sum(IngestionRunRecord.duplicates_detected),
            func.avg(IngestionRunRecord.quality_score),
        ).where(IngestionRunRecord.started_at >= since)
        row = (await self.session.execute(stmt)).one()
        return {
            "runs": float(row[0] or 0),
            "records_received": float(row[1] or 0),
            "records_rejected": float(row[2] or 0),
            "jobs_created": float(row[3] or 0),
            "jobs_updated": float(row[4] or 0),
            "duplicates_detected": float(row[5] or 0),
            "average_quality": float(row[6] or 0.0),
        }


class RejectedRecordRepository(BaseRepository):
    """Quarantine store - nothing invalid is ever silently discarded."""

    async def add_many(self, records: list[RejectedRecord]) -> int:
        if not records:
            return 0
        rows = [
            {
                "source": record.source,
                "source_job_id": record.source_job_id,
                "reason": record.reason,
                "message": record.message[:1000],
                "field": record.field,
                "payload": record.payload,
                "ingestion_run_id": record.ingestion_run_id,
                "rejected_at": record.rejected_at,
            }
            for record in records
        ]
        for chunk in self._chunked(rows, 200):
            await self.session.execute(insert(RejectedRecordRow), chunk)
        return len(rows)

    async def list_recent(
        self, *, source: str | None = None, limit: int = 100
    ) -> list[RejectedRecordRow]:
        stmt = select(RejectedRecordRow).order_by(RejectedRecordRow.rejected_at.desc()).limit(limit)
        if source:
            stmt = stmt.where(RejectedRecordRow.source == source)
        return list((await self.session.execute(stmt)).scalars())

    async def count_by_reason(self, *, since: datetime) -> dict[str, int]:
        stmt = (
            select(RejectedRecordRow.reason, func.count())
            .where(RejectedRecordRow.rejected_at >= since)
            .group_by(RejectedRecordRow.reason)
        )
        rows = (await self.session.execute(stmt)).all()
        return {row[0]: int(row[1]) for row in rows}

    async def prune(self, *, older_than: datetime | None = None, keep_days: int = 90) -> int:
        cutoff = older_than or (utcnow() - timedelta(days=keep_days))
        result = await self.session.execute(
            delete(RejectedRecordRow).where(RejectedRecordRow.rejected_at < cutoff)
        )
        return result.rowcount or 0


__all__ = [
    "IngestionRunRepository",
    "RejectedRecordRepository",
    "SourceStateRepository",
]
