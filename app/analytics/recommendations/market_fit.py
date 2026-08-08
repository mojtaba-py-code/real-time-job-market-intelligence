"""Personal market intelligence.

Given a target role and a skill set, the engine measures how well that profile
matches what the market is currently asking for, and which missing skills would
move the needle most.

This is descriptive analytics over public postings. It is explicitly **not** a
prediction of hiring outcomes, and the result says so in its ``notes``.
"""

from __future__ import annotations

from app.analytics.salary.analyzer import SalaryAnalytics
from app.analytics.statistics import share
from app.core.config import AnalyticsSettings
from app.core.logging import get_logger
from app.core.text import slugify
from app.models.analytics import AnalyticsFilter, LocationDemand, MarketFitResult, SkillDemand
from app.models.enums import RemoteType
from app.models.profiles import CandidateProfile
from app.storage.repositories.analytics import AnalyticsRepository
from app.storage.repositories.jobs import JobQuery, JobRepository

log = get_logger(__name__)

DISCLAIMER = (
    "This score describes how a profile compares with current public job "
    "postings. It is an analytical indicator, not a prediction of employment."
)


class MarketFitEngine:
    """Compares a candidate profile with live market demand."""

    def __init__(
        self,
        analytics: AnalyticsRepository,
        jobs: JobRepository,
        *,
        settings: AnalyticsSettings | None = None,
        salaries: SalaryAnalytics | None = None,
    ) -> None:
        self._analytics = analytics
        self._jobs = jobs
        self._settings = settings or AnalyticsSettings()
        self._salaries = salaries or SalaryAnalytics(analytics, self._settings)

    async def evaluate(
        self, profile: CandidateProfile, *, window_days: int = 30
    ) -> MarketFitResult:
        """Score a profile against the market."""
        flt = AnalyticsFilter(
            window_days=window_days,
            country_code=profile.countries[0] if len(profile.countries) == 1 else None,
            remote_type=(
                profile.remote_preference
                if profile.remote_preference is not RemoteType.UNKNOWN
                else None
            ),
            limit=50,
        )

        matching = await self._matching_jobs(profile, window_days=window_days)
        demand = await self._analytics.skill_counts(flt.model_copy(update={"limit": 60}), limit=60)
        total_with_skills = await self._analytics.jobs_with_skill_count(flt)

        owned = profile.skill_set
        matched: list[str] = []
        missing: list[SkillDemand] = []
        covered_weight = 0.0
        total_weight = 0.0

        for rank, (slug, name, category, count, confidence) in enumerate(demand[:30]):
            weight = float(count)
            total_weight += weight
            if slug in owned:
                covered_weight += weight
                matched.append(slug)
            else:
                missing.append(
                    SkillDemand(
                        slug=slug,
                        name=name,
                        category=_safe_category(category),
                        job_count=count,
                        share=round(share(count, total_with_skills), 4),
                        rank=rank + 1,
                        average_confidence=round(confidence, 3),
                    )
                )

        coverage = share(covered_weight, total_weight)
        salary = await self._salaries.overview(flt)
        remote_share = await self._remote_share(flt)
        locations = await self._top_locations(flt)

        notes = [DISCLAIMER]
        if matching == 0:
            notes.append(
                "No live postings matched the target role and constraints; the "
                "score is based on overall market demand only."
            )
        if len(owned) == 0:
            notes.append("The profile lists no skills, so coverage cannot be computed.")

        return MarketFitResult(
            target_role=profile.target_role,
            matching_jobs=matching,
            market_fit_score=round(self._score(coverage, matching, len(owned)), 4),
            skill_coverage=round(coverage, 4),
            matched_skills=sorted(matched),
            missing_skills=missing[:10],
            salary=salary,
            remote_share=remote_share,
            top_locations=locations,
            notes=notes,
        )

    async def _matching_jobs(self, profile: CandidateProfile, *, window_days: int) -> int:
        """How many live postings fit the target role and constraints."""
        from app.core.timeutils import days_ago

        query = JobQuery(
            title_contains=profile.target_role,
            country_code=profile.countries[0] if len(profile.countries) == 1 else None,
            remote_types=(
                (str(profile.remote_preference),)
                if profile.remote_preference is not RemoteType.UNKNOWN
                else ()
            ),
            salary_min=profile.salary_min,
            first_seen_after=days_ago(window_days),
            limit=1,
        )
        return await self._jobs.count(query)

    async def _remote_share(self, flt: AnalyticsFilter) -> float:
        counts = await self._analytics.remote_counts(flt)
        total = sum(counts.values())
        return round(share(counts.get(str(RemoteType.REMOTE), 0), total), 4)

    async def _top_locations(self, flt: AnalyticsFilter) -> list[LocationDemand]:
        rows = await self._analytics.country_counts(flt, limit=5)
        total = sum(row[2] for row in rows)
        return [
            LocationDemand(
                country_code=code,
                country=name,
                job_count=count,
                share=round(share(count, total), 4),
                remote_share=round(share(remote, count), 4),
            )
            for code, name, count, remote in rows
        ]

    @staticmethod
    def _score(coverage: float, matching_jobs: int, skill_count: int) -> float:
        """Blend skill coverage with the size of the addressable market.

        Coverage dominates - it is the part the candidate controls - but a role
        with no live postings cannot score highly no matter how well the skills
        line up.
        """
        if skill_count == 0:
            return 0.0
        opportunity = min(1.0, matching_jobs / 50)
        breadth = min(1.0, skill_count / 12)
        return max(0.0, min(1.0, 0.6 * coverage + 0.25 * opportunity + 0.15 * breadth))


def _safe_category(value: str) -> object:
    from app.models.enums import SkillCategory

    try:
        return SkillCategory(value)
    except ValueError:
        return SkillCategory.OTHER


def profile_from_inputs(
    *,
    target_role: str,
    skills: list[str],
    experience_years: float = 0.0,
    countries: list[str] | None = None,
    remote_preference: RemoteType = RemoteType.UNKNOWN,
) -> CandidateProfile:
    """Convenience builder used by the CLI and the API."""
    return CandidateProfile(
        target_role=target_role,
        skills=[slugify(skill) for skill in skills],
        experience_years=experience_years,
        countries=countries or [],
        remote_preference=remote_preference,
    )


__all__ = ["DISCLAIMER", "MarketFitEngine", "profile_from_inputs"]
