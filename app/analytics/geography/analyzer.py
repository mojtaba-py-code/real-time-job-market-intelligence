"""Geographic and remote-work analytics."""

from __future__ import annotations

from datetime import timedelta

from app.analytics.statistics import percent_change, share
from app.core.config import AnalyticsSettings
from app.core.logging import get_logger
from app.core.timeutils import utcnow
from app.models.analytics import AnalyticsFilter, LocationDemand, RemoteBreakdown
from app.models.enums import RemoteType
from app.processing.normalization.geography import country_name
from app.storage.repositories.analytics import AnalyticsRepository

log = get_logger(__name__)


class GeographyAnalytics:
    """Where the demand is, and how much of it is location-independent."""

    def __init__(
        self,
        repository: AnalyticsRepository,
        settings: AnalyticsSettings | None = None,
    ) -> None:
        self._repo = repository
        self._settings = settings or AnalyticsSettings()

    async def by_country(self, flt: AnalyticsFilter, *, limit: int = 25) -> list[LocationDemand]:
        """Demand per country, with each country's remote share."""
        rows = await self._repo.country_counts(flt, limit=limit)
        total = sum(row[2] for row in rows)
        return [
            LocationDemand(
                country_code=code,
                country=name or country_name(code),
                job_count=count,
                share=round(share(count, total), 4),
                remote_share=round(share(remote, count), 4),
            )
            for code, name, count, remote in rows
        ]

    async def by_city(self, flt: AnalyticsFilter, *, limit: int = 25) -> list[LocationDemand]:
        """Demand per city."""
        rows = await self._repo.city_counts(flt, limit=limit)
        total = sum(row[3] for row in rows)
        return [
            LocationDemand(
                city=city,
                region=region,
                country_code=code,
                country=country_name(code),
                job_count=count,
                share=round(share(count, total), 4),
            )
            for city, region, code, count in rows
        ]

    async def remote_breakdown(self, flt: AnalyticsFilter) -> RemoteBreakdown:
        """Distribution of remote / hybrid / on-site, plus its recent change."""
        counts_raw = await self._repo.remote_counts(flt)
        counts = {remote: counts_raw.get(str(remote), 0) for remote in RemoteType}
        total = sum(counts.values())

        change = await self._remote_change(flt)
        return RemoteBreakdown(
            counts=counts,
            total=total,
            remote_share=round(share(counts[RemoteType.REMOTE], total), 4),
            hybrid_share=round(share(counts[RemoteType.HYBRID], total), 4),
            onsite_share=round(share(counts[RemoteType.ONSITE], total), 4),
            unknown_share=round(share(counts[RemoteType.UNKNOWN], total), 4),
            change_pct=change,
        )

    async def _remote_change(self, flt: AnalyticsFilter) -> float | None:
        """Change in the remote share against the equally long previous window."""
        window = flt.window_days
        now = utcnow()
        current = flt.model_copy(update={"date_from": now - timedelta(days=window), "date_to": now})
        previous = flt.model_copy(
            update={
                "date_from": now - timedelta(days=window * 2),
                "date_to": now - timedelta(days=window),
            }
        )

        current_counts = await self._repo.remote_counts(current)
        previous_counts = await self._repo.remote_counts(previous)
        current_total = sum(current_counts.values())
        previous_total = sum(previous_counts.values())
        if current_total == 0 or previous_total == 0:
            return None

        current_share = current_counts.get(str(RemoteType.REMOTE), 0) / current_total
        previous_share = previous_counts.get(str(RemoteType.REMOTE), 0) / previous_total
        change = percent_change(current_share, previous_share)
        return round(change, 2) if change is not None else None

    async def coverage(self, flt: AnalyticsFilter) -> dict[str, int]:
        """How many distinct countries and cities the window covers."""
        return {
            "countries": await self._repo.distinct_country_count(flt),
            "companies": await self._repo.distinct_company_count(flt),
        }


__all__ = ["GeographyAnalytics"]
