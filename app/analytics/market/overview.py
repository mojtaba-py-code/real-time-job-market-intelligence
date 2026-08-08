"""Market-level analytics: volume, headline numbers and segmentation."""

from __future__ import annotations

from datetime import timedelta

from app.analytics.geography.analyzer import GeographyAnalytics
from app.analytics.salary.analyzer import SalaryAnalytics
from app.analytics.statistics import (
    fill_missing_days,
    mean,
    moving_average,
    percent_change,
    share,
)
from app.analytics.trends.engine import TrendEngine
from app.core.config import AnalyticsSettings
from app.core.logging import get_logger
from app.core.timeutils import day_start, to_date, utcnow
from app.models.analytics import (
    AnalyticsFilter,
    MarketOverview,
    SegmentSummary,
    SeniorityBreakdown,
    TimeSeriesPoint,
    VolumeSeries,
)
from app.models.enums import ExperienceLevel, MarketSegment
from app.storage.repositories.analytics import AnalyticsRepository

log = get_logger(__name__)


class MarketAnalytics:
    """The numbers that answer "what does the market look like right now?"."""

    def __init__(
        self,
        repository: AnalyticsRepository,
        *,
        settings: AnalyticsSettings | None = None,
        trends: TrendEngine | None = None,
        salaries: SalaryAnalytics | None = None,
        geography: GeographyAnalytics | None = None,
    ) -> None:
        self._repo = repository
        self._settings = settings or AnalyticsSettings()
        self._trends = trends or TrendEngine(repository, self._settings)
        self._salaries = salaries or SalaryAnalytics(repository, self._settings)
        self._geography = geography or GeographyAnalytics(repository, self._settings)

    async def volume(self, flt: AnalyticsFilter) -> VolumeSeries:
        """Daily posting volume with a trailing moving average."""
        points = await self._repo.daily_volume(flt)
        start, end = self._repo.window_bounds(flt)
        dense = fill_missing_days(points, start=to_date(start), end=to_date(end or utcnow()))
        counts = [float(count) for _day, count in dense]
        smoothed = moving_average(counts, self._settings.moving_average_window)

        return VolumeSeries(
            granularity="day",
            points=[
                TimeSeriesPoint(day=day, value=float(count), count=count) for day, count in dense
            ],
            total=int(sum(counts)),
            average_per_period=round(mean(counts) or 0.0, 2),
            moving_average=[
                TimeSeriesPoint(day=day, value=round(value, 3), count=int(value))
                for (day, _count), value in zip(dense, smoothed, strict=True)
            ],
        )

    async def seniority(self, flt: AnalyticsFilter) -> SeniorityBreakdown:
        """Distribution of experience levels."""
        raw = await self._repo.seniority_counts(flt)
        counts = {level: raw.get(str(level), 0) for level in ExperienceLevel}
        total = sum(counts.values())
        return SeniorityBreakdown(
            counts=counts,
            total=total,
            shares={level: round(share(count, total), 4) for level, count in counts.items()},
            average_experience_years=await self._repo.average_experience_years(flt),
        )

    async def segments(self, flt: AnalyticsFilter, *, limit: int = 15) -> list[SegmentSummary]:
        """Comparable summary per market segment."""
        raw = await self._repo.segment_counts(flt)
        total = sum(raw.values())
        summaries: list[SegmentSummary] = []

        for segment_value, count in sorted(raw.items(), key=lambda item: item[1], reverse=True)[
            :limit
        ]:
            try:
                segment = MarketSegment(segment_value)
            except ValueError:
                continue
            scoped = flt.model_copy(update={"segment": segment, "limit": 5})
            remote = await self._geography.remote_breakdown(scoped)
            salary = await self._salaries.overview(scoped)
            skills = await self._trends.demand_snapshot(scoped, limit=5)
            summaries.append(
                SegmentSummary(
                    segment=segment,
                    job_count=count,
                    share=round(share(count, total), 4),
                    remote_share=remote.remote_share,
                    median_salary=salary.median,
                    top_skills=skills,
                    growth_pct=await self._segment_growth(segment, flt.window_days),
                )
            )
        return summaries

    async def overview(self, flt: AnalyticsFilter) -> MarketOverview:
        """The dashboard headline block."""
        now = utcnow()
        volume = await self.volume(flt)
        top_skills = await self._trends.demand_snapshot(flt, limit=1)
        fastest = await self._trends.fastest_growing(
            flt, window_days=min(flt.window_days, 30), limit=1
        )
        salary = await self._salaries.overview(flt)
        remote = await self._geography.remote_breakdown(flt)
        coverage = await self._geography.coverage(flt)

        return MarketOverview(
            generated_at=now,
            window_days=flt.window_days,
            active_jobs=await self._repo.active_job_count(),
            new_jobs_today=await self._repo.count_since(day_start(now)),
            new_jobs_this_week=await self._repo.count_since(now - timedelta(days=7)),
            total_jobs=await self._repo.total_job_count(),
            companies_hiring=coverage["companies"],
            countries_covered=coverage["countries"],
            top_skill=top_skills[0] if top_skills else None,
            fastest_growing_skill=fastest[0] if fastest else None,
            average_salary=salary.average,
            median_salary=salary.median,
            remote_share=remote.remote_share,
            data_quality_score=round(await self._repo.average_quality_score(flt), 4),
            volume=volume,
        )

    async def _segment_growth(self, segment: MarketSegment, window_days: int) -> float | None:
        """Change in a segment's posting volume against the previous window."""
        now = utcnow()
        current = await self._repo.total_jobs(
            AnalyticsFilter(
                segment=segment,
                date_from=now - timedelta(days=window_days),
                date_to=now,
                window_days=window_days,
            )
        )
        previous = await self._repo.total_jobs(
            AnalyticsFilter(
                segment=segment,
                date_from=now - timedelta(days=window_days * 2),
                date_to=now - timedelta(days=window_days),
                window_days=window_days,
            )
        )
        change = percent_change(current, previous)
        return round(change, 2) if change is not None else None


__all__ = ["MarketAnalytics"]
