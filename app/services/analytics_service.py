"""Analytics facade.

The engines know how to compute; this service knows *when* to compute. It owns
the session lifecycle, the cache keys and the invalidation, so routers and the
CLI can ask a single question and get an answer without touching SQLAlchemy or
Redis.
"""

from __future__ import annotations

from typing import Any

from app.analytics.company.analyzer import CompanyAnalytics
from app.analytics.geography.analyzer import GeographyAnalytics
from app.analytics.market.overview import MarketAnalytics
from app.analytics.salary.analyzer import SalaryAnalytics
from app.analytics.skills.cooccurrence import CooccurrenceAnalyzer
from app.analytics.trends.emerging import EmergingTechnologyDetector
from app.analytics.trends.engine import TrendEngine
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.models.analytics import (
    AnalyticsFilter,
    CompanyHiring,
    EmergingSkill,
    LocationDemand,
    MarketOverview,
    RemoteBreakdown,
    SalaryBreakdown,
    SalaryStatistics,
    SegmentSummary,
    SeniorityBreakdown,
    SkillCooccurrence,
    SkillDemand,
    SkillExplorerView,
    SkillGraph,
    SkillTrend,
    VolumeSeries,
)
from app.models.enums import SkillCategory
from app.storage.cache import CacheBackend, NullCache, build_key
from app.storage.repositories.analytics import AnalyticsRepository
from app.storage.session import Database

log = get_logger(__name__)

CACHE_FAMILY = "analytics"


class AnalyticsService:
    """Cached access to every analytics engine."""

    def __init__(
        self,
        database: Database,
        *,
        cache: CacheBackend | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._db = database
        self._settings = settings or get_settings()
        # `cache is not None` rather than a truthiness test: an empty
        # cache defines __len__ and would otherwise be discarded here.
        self._cache = cache if cache is not None else NullCache()

    # ------------------------------------------------------------------ #
    # Cache plumbing
    # ------------------------------------------------------------------ #
    def _key(self, name: str, flt: AnalyticsFilter, *extra: str) -> str:
        return build_key(
            self._settings.cache.namespace, CACHE_FAMILY, name, flt.cache_key(), *extra
        )

    async def _cached(self, key: str, factory: Any) -> Any:
        hit = await self._cache.get(key)
        if hit is not None:
            return hit
        value = await factory()
        await self._cache.set(key, value, ttl_seconds=self._settings.cache.analytics_ttl_seconds)
        return value

    async def invalidate(self) -> int:
        """Drop every cached analytics answer (called after ingestion)."""
        prefix = build_key(self._settings.cache.namespace, CACHE_FAMILY)
        removed = await self._cache.delete_prefix(prefix)
        if removed:
            log.debug("analytics.cache_invalidated", entries=removed)
        return removed

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    async def overview(self, flt: AnalyticsFilter) -> MarketOverview:
        async def factory() -> dict[str, Any]:
            async with self._db.read_session() as session:
                engine = MarketAnalytics(
                    AnalyticsRepository(session), settings=self._settings.analytics
                )
                return (await engine.overview(flt)).model_dump(mode="json")

        payload = await self._cached(self._key("overview", flt), factory)
        return MarketOverview.model_validate(payload)

    async def volume(self, flt: AnalyticsFilter) -> VolumeSeries:
        async def factory() -> dict[str, Any]:
            async with self._db.read_session() as session:
                engine = MarketAnalytics(
                    AnalyticsRepository(session), settings=self._settings.analytics
                )
                return (await engine.volume(flt)).model_dump(mode="json")

        return VolumeSeries.model_validate(await self._cached(self._key("volume", flt), factory))

    async def skills(self, flt: AnalyticsFilter) -> list[SkillDemand]:
        async def factory() -> list[dict[str, Any]]:
            async with self._db.read_session() as session:
                engine = TrendEngine(AnalyticsRepository(session), self._settings.analytics)
                rows = await engine.demand_snapshot(flt)
                return [row.model_dump(mode="json") for row in rows]

        payload = await self._cached(self._key("skills", flt), factory)
        return [SkillDemand.model_validate(row) for row in payload]

    async def skill_trend(
        self, slug: str, *, name: str | None = None, category: SkillCategory | None = None
    ) -> SkillTrend:
        flt = AnalyticsFilter(skill=slug, limit=1)

        async def factory() -> dict[str, Any]:
            async with self._db.read_session() as session:
                engine = TrendEngine(AnalyticsRepository(session), self._settings.analytics)
                trend = await engine.skill_trend(
                    slug, name=name, category=category or SkillCategory.OTHER
                )
                return trend.model_dump(mode="json")

        return SkillTrend.model_validate(
            await self._cached(self._key("skill_trend", flt, slug), factory)
        )

    async def top_trends(self, flt: AnalyticsFilter, *, limit: int = 10) -> list[SkillTrend]:
        async def factory() -> list[dict[str, Any]]:
            async with self._db.read_session() as session:
                engine = TrendEngine(AnalyticsRepository(session), self._settings.analytics)
                trends = await engine.top_trends(flt, limit=limit)
                return [trend.model_dump(mode="json") for trend in trends]

        payload = await self._cached(self._key("top_trends", flt, str(limit)), factory)
        return [SkillTrend.model_validate(row) for row in payload]

    async def emerging(self, *, window_days: int = 30, limit: int = 10) -> list[EmergingSkill]:
        flt = AnalyticsFilter(window_days=window_days, limit=limit)

        async def factory() -> list[dict[str, Any]]:
            async with self._db.read_session() as session:
                detector = EmergingTechnologyDetector(
                    AnalyticsRepository(session), self._settings.analytics
                )
                rows = await detector.detect(window_days=window_days, limit=limit)
                return [row.model_dump(mode="json") for row in rows]

        payload = await self._cached(self._key("emerging", flt), factory)
        return [EmergingSkill.model_validate(row) for row in payload]

    async def cooccurrence(
        self, flt: AnalyticsFilter, *, limit: int = 50, skill: str | None = None
    ) -> list[SkillCooccurrence]:
        async def factory() -> list[dict[str, Any]]:
            async with self._db.read_session() as session:
                analyzer = CooccurrenceAnalyzer(
                    AnalyticsRepository(session), self._settings.analytics
                )
                rows = await analyzer.pairs(flt, limit=limit, skill=skill)
                return [row.model_dump(mode="json") for row in rows]

        payload = await self._cached(
            self._key("cooccurrence", flt, str(limit), skill or "-"), factory
        )
        return [SkillCooccurrence.model_validate(row) for row in payload]

    async def skill_graph(self, flt: AnalyticsFilter, *, limit: int = 40) -> SkillGraph:
        async def factory() -> dict[str, Any]:
            async with self._db.read_session() as session:
                analyzer = CooccurrenceAnalyzer(
                    AnalyticsRepository(session), self._settings.analytics
                )
                return (await analyzer.graph(flt, limit=limit)).model_dump(mode="json")

        return SkillGraph.model_validate(
            await self._cached(self._key("skill_graph", flt, str(limit)), factory)
        )

    async def salaries(self, flt: AnalyticsFilter) -> SalaryStatistics:
        async def factory() -> dict[str, Any]:
            async with self._db.read_session() as session:
                analyzer = SalaryAnalytics(AnalyticsRepository(session), self._settings.analytics)
                return (await analyzer.overview(flt)).model_dump(mode="json")

        return SalaryStatistics.model_validate(
            await self._cached(self._key("salaries", flt), factory)
        )

    async def salaries_by(
        self, flt: AnalyticsFilter, *, dimension: str, limit: int = 20
    ) -> list[SalaryBreakdown]:
        async def factory() -> list[dict[str, Any]]:
            async with self._db.read_session() as session:
                analyzer = SalaryAnalytics(AnalyticsRepository(session), self._settings.analytics)
                rows = await analyzer.by_dimension(flt, dimension=dimension, limit=limit)
                return [row.model_dump(mode="json") for row in rows]

        payload = await self._cached(self._key("salaries_by", flt, dimension), factory)
        return [SalaryBreakdown.model_validate(row) for row in payload]

    async def locations(
        self, flt: AnalyticsFilter, *, by: str = "country", limit: int = 25
    ) -> list[LocationDemand]:
        async def factory() -> list[dict[str, Any]]:
            async with self._db.read_session() as session:
                analyzer = GeographyAnalytics(
                    AnalyticsRepository(session), self._settings.analytics
                )
                rows = (
                    await analyzer.by_city(flt, limit=limit)
                    if by == "city"
                    else await analyzer.by_country(flt, limit=limit)
                )
                return [row.model_dump(mode="json") for row in rows]

        payload = await self._cached(self._key("locations", flt, by, str(limit)), factory)
        return [LocationDemand.model_validate(row) for row in payload]

    async def remote(self, flt: AnalyticsFilter) -> RemoteBreakdown:
        async def factory() -> dict[str, Any]:
            async with self._db.read_session() as session:
                analyzer = GeographyAnalytics(
                    AnalyticsRepository(session), self._settings.analytics
                )
                return (await analyzer.remote_breakdown(flt)).model_dump(mode="json")

        return RemoteBreakdown.model_validate(await self._cached(self._key("remote", flt), factory))

    async def seniority(self, flt: AnalyticsFilter) -> SeniorityBreakdown:
        async def factory() -> dict[str, Any]:
            async with self._db.read_session() as session:
                engine = MarketAnalytics(
                    AnalyticsRepository(session), settings=self._settings.analytics
                )
                return (await engine.seniority(flt)).model_dump(mode="json")

        return SeniorityBreakdown.model_validate(
            await self._cached(self._key("seniority", flt), factory)
        )

    async def segments(self, flt: AnalyticsFilter, *, limit: int = 15) -> list[SegmentSummary]:
        async def factory() -> list[dict[str, Any]]:
            async with self._db.read_session() as session:
                engine = MarketAnalytics(
                    AnalyticsRepository(session), settings=self._settings.analytics
                )
                rows = await engine.segments(flt, limit=limit)
                return [row.model_dump(mode="json") for row in rows]

        payload = await self._cached(self._key("segments", flt, str(limit)), factory)
        return [SegmentSummary.model_validate(row) for row in payload]

    async def companies(
        self, flt: AnalyticsFilter, *, limit: int = 20, with_skills: bool = False
    ) -> list[CompanyHiring]:
        async def factory() -> list[dict[str, Any]]:
            async with self._db.read_session() as session:
                analyzer = CompanyAnalytics(AnalyticsRepository(session), self._settings.analytics)
                rows = await analyzer.top_hiring(flt, limit=limit, with_skills=with_skills)
                return [row.model_dump(mode="json") for row in rows]

        payload = await self._cached(
            self._key("companies", flt, str(limit), str(with_skills)), factory
        )
        return [CompanyHiring.model_validate(row) for row in payload]

    async def company_profile(self, name: str, flt: AnalyticsFilter) -> CompanyHiring | None:
        async with self._db.read_session() as session:
            analyzer = CompanyAnalytics(AnalyticsRepository(session), self._settings.analytics)
            return await analyzer.profile(name, flt)

    async def skill_explorer(self, slug: str, flt: AnalyticsFilter) -> SkillExplorerView | None:
        """Everything the dashboard's skill explorer needs, in one call."""
        async with self._db.read_session() as session:
            repo = AnalyticsRepository(session)
            trends = TrendEngine(repo, self._settings.analytics)
            cooccurrence = CooccurrenceAnalyzer(repo, self._settings.analytics)
            salaries = SalaryAnalytics(repo, self._settings.analytics)
            geography = GeographyAnalytics(repo, self._settings.analytics)
            companies = CompanyAnalytics(repo, self._settings.analytics)
            market = MarketAnalytics(repo, settings=self._settings.analytics)

            demand = await trends.demand_snapshot(flt.model_copy(update={"limit": 400}), limit=400)
            skill = next((item for item in demand if item.slug == slug), None)
            if skill is None:
                return None

            scoped = flt.model_copy(update={"skill": slug})
            return SkillExplorerView(
                skill=skill,
                trend=await trends.skill_trend(slug, name=skill.name, category=skill.category),
                related_skills=await cooccurrence.related(slug, flt, limit=10),
                top_companies=await companies.top_hiring(scoped, limit=5),
                top_locations=await geography.by_country(scoped, limit=5),
                salary=await salaries.for_skill(slug, flt),
                remote=await geography.remote_breakdown(scoped),
                seniority=await market.seniority(scoped),
            )


__all__ = ["CACHE_FAMILY", "AnalyticsService"]
