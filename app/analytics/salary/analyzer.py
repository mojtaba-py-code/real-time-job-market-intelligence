"""Salary analytics.

Only *observed* salaries reach this module - figures the platform read in a
posting, annualised and converted with a dated rate table. Nothing is imputed,
so a market where 40% of postings disclose pay produces statistics over that
40% and says so through ``observed_share`` and ``sample_size``.

Small samples are suppressed rather than published: a "median salary" computed
from three postings is noise wearing a statistic's clothes.
"""

from __future__ import annotations

from app.analytics.statistics import mean, median, percentile
from app.core.config import AnalyticsSettings
from app.core.logging import get_logger
from app.models.analytics import AnalyticsFilter, SalaryBreakdown, SalaryStatistics
from app.storage.repositories.analytics import AnalyticsRepository

log = get_logger(__name__)

#: Dimensions the analyser can group by.
SUPPORTED_DIMENSIONS = ("segment", "seniority", "country", "city", "company", "title", "remote")


class SalaryAnalytics:
    """Descriptive statistics over disclosed compensation."""

    def __init__(
        self,
        repository: AnalyticsRepository,
        settings: AnalyticsSettings | None = None,
        *,
        currency: str = "USD",
    ) -> None:
        self._repo = repository
        self._settings = settings or AnalyticsSettings()
        self._currency = currency

    @property
    def min_sample_size(self) -> int:
        return self._settings.min_sample_size

    async def overview(self, flt: AnalyticsFilter) -> SalaryStatistics:
        """Statistics over every disclosed salary in the window."""
        samples = await self._repo.salary_samples(flt)
        observed_share = await self._repo.observed_salary_share(flt)
        return self.describe(samples, observed_share=observed_share)

    async def by_dimension(
        self, flt: AnalyticsFilter, *, dimension: str, limit: int = 20
    ) -> list[SalaryBreakdown]:
        """Salary statistics grouped by segment, seniority, country, ..."""
        if dimension not in SUPPORTED_DIMENSIONS:
            raise ValueError(
                f"unsupported dimension {dimension!r}; expected one of {SUPPORTED_DIMENSIONS}"
            )
        rows = await self._repo.salary_samples_by(flt, dimension=dimension)
        grouped: dict[str, list[float]] = {}
        for key, value in rows:
            grouped.setdefault(key, []).append(value)

        breakdowns = [
            SalaryBreakdown(
                dimension=dimension,
                key=key,
                label=_humanize(key),
                statistics=self.describe(values),
            )
            for key, values in grouped.items()
            if len(values) >= self.min_sample_size
        ]
        breakdowns.sort(
            key=lambda item: item.statistics.median or 0.0,
            reverse=True,
        )
        return breakdowns[:limit]

    async def for_skill(self, slug: str, flt: AnalyticsFilter) -> SalaryStatistics:
        """Salary statistics for postings that require a given skill."""
        samples = await self._repo.salary_samples_for_skill(slug, flt)
        return self.describe(samples)

    def describe(self, samples: list[float], *, observed_share: float = 0.0) -> SalaryStatistics:
        """Turn a list of annualised salaries into a statistics object.

        Returns an empty result (``sample_size`` intact) when the sample is too
        small to publish, so callers can distinguish "no data" from "hidden".
        """
        cleaned = [value for value in samples if value and value > 0]
        if len(cleaned) < self.min_sample_size:
            return SalaryStatistics(
                sample_size=len(cleaned),
                currency=self._currency,
                observed_share=round(observed_share, 4),
            )
        return SalaryStatistics(
            sample_size=len(cleaned),
            currency=self._currency,
            average=_round(mean(cleaned)),
            median=_round(median(cleaned)),
            p25=_round(percentile(cleaned, 25)),
            p75=_round(percentile(cleaned, 75)),
            p90=_round(percentile(cleaned, 90)),
            minimum=_round(min(cleaned)),
            maximum=_round(max(cleaned)),
            observed_share=round(observed_share, 4),
        )


def _round(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def _humanize(key: str) -> str:
    """Turn an enum-ish key into a display label."""
    return key.replace("_", " ").title() if key else "Unknown"


__all__ = ["SUPPORTED_DIMENSIONS", "SalaryAnalytics"]
