"""Persistence for data-quality events, skill trends and market snapshots."""

from __future__ import annotations

from datetime import date, datetime
from typing import cast

from sqlalchemy import func, insert, select

from app.core.timeutils import day_start, utcnow
from app.models.analytics import SkillTrend
from app.models.enums import QualityDimension
from app.models.quality import QualityEvent
from app.storage.models import DataQualityEvent, MarketSnapshot, SkillTrendRow
from app.storage.repositories.base import BaseRepository


class QualityRepository(BaseRepository):
    """Stores measured quality violations."""

    async def add_many(self, events: list[QualityEvent]) -> int:
        if not events:
            return 0
        rows = [
            {
                "source": event.source,
                "dimension": str(event.dimension),
                "field": event.field,
                "severity": event.severity,
                "message": event.message[:1000],
                "value": event.value,
                "threshold": event.threshold,
                "job_id": event.job_id,
                "ingestion_run_id": event.ingestion_run_id,
                "occurred_at": event.occurred_at,
            }
            for event in events
        ]
        for chunk in self._chunked(rows, 200):
            await self.session.execute(insert(DataQualityEvent), chunk)
        return len(rows)

    async def counts_by_dimension(self, *, since: datetime) -> dict[QualityDimension, int]:
        stmt = (
            select(DataQualityEvent.dimension, func.count())
            .where(DataQualityEvent.occurred_at >= since)
            .group_by(DataQualityEvent.dimension)
        )
        rows = (await self.session.execute(stmt)).all()
        return {QualityDimension(row[0]): int(row[1]) for row in rows}

    async def counts_by_source(self, *, since: datetime) -> dict[str, int]:
        stmt = (
            select(DataQualityEvent.source, func.count())
            .where(DataQualityEvent.occurred_at >= since)
            .group_by(DataQualityEvent.source)
        )
        rows = (await self.session.execute(stmt)).all()
        return {row[0]: int(row[1]) for row in rows}

    async def list_recent(self, *, limit: int = 100) -> list[DataQualityEvent]:
        stmt = select(DataQualityEvent).order_by(DataQualityEvent.occurred_at.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars())


class SkillTrendRepository(BaseRepository):
    """Materialised daily skill demand, refreshed by the analytics worker."""

    async def upsert_daily(self, rows: list[dict[str, object]]) -> int:
        """Insert or update ``(skill_slug, day)`` demand rows."""
        if not rows:
            return 0
        keys = {(str(r["skill_slug"]), r["day"]) for r in rows}
        slugs = sorted({k[0] for k in keys})
        existing: dict[tuple[str, datetime], SkillTrendRow] = {}
        for chunk in self._chunked(slugs, 200):
            result = await self.session.execute(
                select(SkillTrendRow).where(SkillTrendRow.skill_slug.in_(chunk))
            )
            for row in result.scalars():
                existing[(row.skill_slug, row.day)] = row

        written = 0
        for values in rows:
            row_key = (str(values["skill_slug"]), cast("datetime", values["day"]))
            found = existing.get(row_key)
            if found is None:
                self.session.add(SkillTrendRow(**values))  # type: ignore[arg-type]
            else:
                for field, value in values.items():
                    setattr(found, field, value)
            written += 1
        await self.session.flush()
        return written

    async def series(
        self, slug: str, *, since: datetime
    ) -> list[tuple[date, int, float, float | None]]:
        stmt = (
            select(
                SkillTrendRow.day,
                SkillTrendRow.job_count,
                SkillTrendRow.share,
                SkillTrendRow.moving_average,
            )
            .where(SkillTrendRow.skill_slug == slug)
            .where(SkillTrendRow.day >= since)
            .order_by(SkillTrendRow.day)
        )
        rows = (await self.session.execute(stmt)).all()
        return [(row[0].date(), int(row[1]), float(row[2]), row[3]) for row in rows]

    async def save_trends(self, trends: list[SkillTrend]) -> int:
        """Persist the computed direction of each skill for the current day."""
        today = day_start(utcnow())
        rows: list[dict[str, object]] = []
        for trend in trends:
            latest = trend.series[-1] if trend.series else None
            rows.append(
                {
                    "skill_slug": trend.slug,
                    "day": today,
                    "job_count": latest.count if latest else 0,
                    "share": float(latest.value) if latest else 0.0,
                    "moving_average": trend.moving_average,
                    "direction": str(trend.direction),
                    "computed_at": utcnow(),
                }
            )
        return await self.upsert_daily(rows)


class MarketSnapshotRepository(BaseRepository):
    """Daily frozen market metrics, used for fast dashboards and back-testing."""

    async def upsert(self, snapshot_date: datetime, values: dict[str, object]) -> None:
        normalized_day = day_start(snapshot_date)
        stmt = select(MarketSnapshot).where(MarketSnapshot.snapshot_date == normalized_day)
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            self.session.add(MarketSnapshot(snapshot_date=normalized_day, **values))  # type: ignore[arg-type]
        else:
            for field, value in values.items():
                setattr(row, field, value)
        await self.session.flush()

    async def latest(self) -> MarketSnapshot | None:
        stmt = select(MarketSnapshot).order_by(MarketSnapshot.snapshot_date.desc()).limit(1)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def history(self, *, since: datetime, limit: int = 365) -> list[MarketSnapshot]:
        stmt = (
            select(MarketSnapshot)
            .where(MarketSnapshot.snapshot_date >= since)
            .order_by(MarketSnapshot.snapshot_date)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars())


__all__ = ["MarketSnapshotRepository", "QualityRepository", "SkillTrendRepository"]
