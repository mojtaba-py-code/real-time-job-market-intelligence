"""Skill-trend detection.

For every skill the engine builds a dense daily demand series, smooths it,
compares the trailing window with the equivalent preceding window and labels
the result. Three things keep the output honest:

* windows are compared like-for-like (last 7 days against the 7 days before,
  never against a different-length period);
* gaps are filled with zeros before smoothing, so a skill that disappears for
  a week is not silently interpolated;
* every trend carries a sample size and a confidence, and a series that is too
  short is reported as ``insufficient_data`` rather than guessed at.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta

from app.analytics.statistics import (
    classify_trend,
    coefficient_of_variation,
    confidence_from_sample,
    fill_missing_days,
    moving_average,
    percent_change,
    share,
)
from app.core.config import AnalyticsSettings
from app.core.logging import get_logger
from app.core.timeutils import to_date, utcnow
from app.models.analytics import (
    AnalyticsFilter,
    SkillDemand,
    SkillTrend,
    TimeSeriesPoint,
    TrendWindow,
)
from app.models.enums import SkillCategory, TrendDirection
from app.storage.repositories.analytics import AnalyticsRepository

log = get_logger(__name__)


class TrendEngine:
    """Computes demand trends for skills."""

    def __init__(
        self,
        repository: AnalyticsRepository,
        settings: AnalyticsSettings | None = None,
    ) -> None:
        self._repo = repository
        self._settings = settings or AnalyticsSettings()

    async def skill_trend(
        self,
        slug: str,
        *,
        name: str | None = None,
        category: SkillCategory = SkillCategory.OTHER,
        windows: tuple[int, ...] | None = None,
        reference: datetime | None = None,
    ) -> SkillTrend:
        """Full multi-window trend for a single skill."""
        windows = windows or self._settings.trend_windows_days
        now = reference or utcnow()
        longest = max(windows)
        # Two full windows are needed so the oldest comparison has a baseline.
        history_start = now - timedelta(days=longest * 2)

        raw_series = await self._repo.skill_daily_counts(slug, start=history_start, end=now)
        dense = fill_missing_days(raw_series, start=to_date(history_start), end=to_date(now))
        counts = [count for _day, count in dense]
        total = sum(counts)

        trend_windows: list[TrendWindow] = []
        for days in sorted(windows):
            current, previous = self._window_counts(dense, days=days, reference=now)
            change = percent_change(current, previous)
            trend_windows.append(
                TrendWindow(
                    days=days,
                    current_count=current,
                    previous_count=previous,
                    change_pct=round(change, 2) if change is not None else None,
                    direction=classify_trend(
                        change_pct=change,
                        series=self._tail(counts, days),
                        rising_threshold=self._settings.rising_threshold_pct,
                        declining_threshold=self._settings.declining_threshold_pct,
                        volatility_threshold=self._settings.volatility_cv_threshold,
                        min_samples=self._settings.min_sample_size,
                    ),
                )
            )

        headline = self._headline_window(trend_windows)
        smoothed = moving_average([float(c) for c in counts], self._settings.moving_average_window)

        return SkillTrend(
            slug=slug,
            name=name or slug,
            category=category,
            windows=trend_windows,
            direction=headline.direction if headline else TrendDirection.INSUFFICIENT_DATA,
            moving_average=round(smoothed[-1], 3) if smoothed else None,
            volatility=round(coefficient_of_variation([float(c) for c in counts]), 4),
            series=[
                TimeSeriesPoint(day=day, value=float(count), count=count)
                for day, count in dense[-longest:]
            ],
            sample_size=total,
            confidence=confidence_from_sample(total),
        )

    async def top_trends(
        self,
        flt: AnalyticsFilter,
        *,
        limit: int = 10,
        direction: TrendDirection | None = None,
    ) -> list[SkillTrend]:
        """Trends for the most in-demand skills, optionally filtered by label."""
        demand = await self._repo.skill_counts(flt, limit=max(limit * 3, 30))
        trends: list[SkillTrend] = []
        for slug, name, category, _count, _confidence in demand:
            trend = await self.skill_trend(slug, name=name, category=_safe_category(category))
            if direction is not None and trend.direction is not direction:
                continue
            trends.append(trend)
            if len(trends) >= limit:
                break
        return trends

    async def fastest_growing(
        self, flt: AnalyticsFilter, *, window_days: int = 30, limit: int = 5
    ) -> list[SkillTrend]:
        """Skills with the largest positive change over ``window_days``."""
        demand = await self._repo.skill_counts(flt, limit=max(limit * 6, 40))
        scored: list[tuple[float, SkillTrend]] = []
        for slug, name, category, count, _confidence in demand:
            if count < self._settings.min_sample_size:
                continue
            trend = await self.skill_trend(
                slug, name=name, category=_safe_category(category), windows=(window_days,)
            )
            window = next((w for w in trend.windows if w.days == window_days), None)
            if window is None or window.change_pct is None:
                continue
            scored.append((window.change_pct, trend))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [trend for _change, trend in scored[:limit]]

    async def demand_snapshot(
        self, flt: AnalyticsFilter, *, limit: int | None = None
    ) -> list[SkillDemand]:
        """Ranked skill demand for the filtered window."""
        rows = await self._repo.skill_counts(flt, limit=limit or flt.limit)
        total = await self._repo.jobs_with_skill_count(flt)
        return [
            SkillDemand(
                slug=slug,
                name=name,
                category=_safe_category(category),
                job_count=count,
                share=round(share(count, total), 4),
                rank=index + 1,
                average_confidence=round(confidence, 3),
            )
            for index, (slug, name, category, count, confidence) in enumerate(rows)
        ]

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    @staticmethod
    def _window_counts(
        dense: Sequence[tuple[date, int]], *, days: int, reference: datetime
    ) -> tuple[int, int]:
        """Sum the trailing window and the equally long window before it."""
        counts = [count for _day, count in dense]
        current = sum(counts[-days:]) if len(counts) >= days else sum(counts)
        previous_slice = counts[-2 * days : -days] if len(counts) >= 2 * days else []
        previous = sum(previous_slice)
        return current, previous

    @staticmethod
    def _tail(counts: list[int], days: int) -> list[float]:
        return [float(value) for value in counts[-days:]]

    @staticmethod
    def _headline_window(windows: list[TrendWindow]) -> TrendWindow | None:
        """Prefer the 30-day view as the headline, else the longest available."""
        if not windows:
            return None
        for window in windows:
            if window.days == 30:
                return window
        return max(windows, key=lambda w: w.days)


def _safe_category(value: str) -> SkillCategory:
    try:
        return SkillCategory(value)
    except ValueError:
        return SkillCategory.OTHER


__all__ = ["TrendEngine"]
