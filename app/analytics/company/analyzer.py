"""Company hiring intelligence."""

from __future__ import annotations

from datetime import timedelta

from app.analytics.statistics import percent_change, share
from app.core.config import AnalyticsSettings
from app.core.logging import get_logger
from app.core.text import slugify
from app.core.timeutils import utcnow
from app.models.analytics import AnalyticsFilter, CompanyHiring, SkillDemand
from app.storage.repositories.analytics import AnalyticsRepository

log = get_logger(__name__)


class CompanyAnalytics:
    """Who is hiring, how fast, and for what."""

    def __init__(
        self,
        repository: AnalyticsRepository,
        settings: AnalyticsSettings | None = None,
    ) -> None:
        self._repo = repository
        self._settings = settings or AnalyticsSettings()

    async def top_hiring(
        self, flt: AnalyticsFilter, *, limit: int = 20, with_skills: bool = False
    ) -> list[CompanyHiring]:
        """Companies with the most open postings in the window."""
        rows = await self._repo.company_counts(flt, limit=limit)
        growth = await self._growth_map(window_days=flt.window_days)

        profiles: list[CompanyHiring] = []
        for company_id, name, count, remote_count in rows:
            current, previous = growth.get(name, (0, 0))
            change = percent_change(current, previous)
            skills: list[SkillDemand] = []
            if with_skills:
                skills = await self._top_skills(name, limit=5)
            profiles.append(
                CompanyHiring(
                    company_id=company_id or slugify(name),
                    name=name,
                    slug=slugify(name),
                    active_job_count=count,
                    total_job_count=count,
                    jobs_last_30_days=current,
                    jobs_previous_30_days=previous,
                    hiring_growth_pct=round(change, 2) if change is not None else None,
                    remote_share=round(share(remote_count, count), 4),
                    top_skills=skills,
                )
            )
        return profiles

    async def profile(self, company_name: str, flt: AnalyticsFilter) -> CompanyHiring | None:
        """Full hiring profile for one company."""
        scoped = flt.model_copy(update={"limit": 1})
        rows = await self._repo.company_counts(scoped, limit=200)
        match = next((row for row in rows if row[1] == company_name), None)
        if match is None:
            return None

        company_id, name, count, remote_count = match
        growth = await self._growth_map(window_days=flt.window_days)
        current, previous = growth.get(name, (0, 0))
        change = percent_change(current, previous)

        return CompanyHiring(
            company_id=company_id or slugify(name),
            name=name,
            slug=slugify(name),
            active_job_count=count,
            total_job_count=count,
            jobs_last_30_days=current,
            jobs_previous_30_days=previous,
            hiring_growth_pct=round(change, 2) if change is not None else None,
            remote_share=round(share(remote_count, count), 4),
            top_skills=await self._top_skills(name, limit=10),
        )

    async def _growth_map(self, *, window_days: int) -> dict[str, tuple[int, int]]:
        """Per-company posting counts for the current and previous window."""
        now = utcnow()
        current = await self._repo.company_job_counts_between(
            start=now - timedelta(days=window_days), end=now, limit=300
        )
        previous = await self._repo.company_job_counts_between(
            start=now - timedelta(days=window_days * 2),
            end=now - timedelta(days=window_days),
            limit=300,
        )
        names = set(current) | set(previous)
        return {name: (current.get(name, 0), previous.get(name, 0)) for name in names}

    async def _top_skills(self, company_name: str, *, limit: int) -> list[SkillDemand]:
        rows = await self._repo.top_skills_for_company(company_name, limit=limit)
        total = max((row[2] for row in rows), default=0)
        return [
            SkillDemand(
                slug=slug,
                name=name,
                job_count=count,
                share=round(share(count, total), 4),
                rank=index + 1,
            )
            for index, (slug, name, count) in enumerate(rows)
        ]


__all__ = ["CompanyAnalytics"]
