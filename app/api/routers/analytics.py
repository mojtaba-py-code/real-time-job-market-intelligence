"""Market analytics endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.analytics.salary.analyzer import SUPPORTED_DIMENSIONS
from app.api.deps import AnalyticsFilterDep, AnalyticsServiceDep, require_scope
from app.core.errors import InvalidRequestError
from app.models.analytics import (
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
    SkillGraph,
    SkillTrend,
    VolumeSeries,
)
from app.models.enums import Scope

router = APIRouter(
    prefix="/analytics",
    tags=["analytics"],
    dependencies=[require_scope(Scope.ANALYTICS_READ)],
)


@router.get("/market", response_model=MarketOverview, summary="Market overview")
async def market(analytics: AnalyticsServiceDep, flt: AnalyticsFilterDep) -> MarketOverview:
    """Headline numbers for the dashboard."""
    return await analytics.overview(flt)


@router.get("/volume", response_model=VolumeSeries, summary="Posting volume over time")
async def volume(analytics: AnalyticsServiceDep, flt: AnalyticsFilterDep) -> VolumeSeries:
    """Daily posting volume with a trailing moving average."""
    return await analytics.volume(flt)


@router.get("/skills", response_model=list[SkillDemand], summary="Skill demand")
async def skills(analytics: AnalyticsServiceDep, flt: AnalyticsFilterDep) -> list[SkillDemand]:
    """Ranked skill demand for the selected window."""
    return await analytics.skills(flt)


@router.get("/skills/trends", response_model=list[SkillTrend], summary="Top skill trends")
async def skill_trends(
    analytics: AnalyticsServiceDep,
    flt: AnalyticsFilterDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[SkillTrend]:
    """Trend summaries for the most in-demand skills."""
    return await analytics.top_trends(flt, limit=limit)


@router.get("/emerging-skills", response_model=list[EmergingSkill], summary="Emerging technologies")
async def emerging(
    analytics: AnalyticsServiceDep,
    window_days: Annotated[int, Query(ge=7, le=1825)] = 30,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[EmergingSkill]:
    """Technologies growing sharply from a small base."""
    return await analytics.emerging(window_days=window_days, limit=limit)


@router.get("/co-occurrence", response_model=list[SkillCooccurrence], summary="Skill co-occurrence")
async def cooccurrence(
    analytics: AnalyticsServiceDep,
    flt: AnalyticsFilterDep,
    skill: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[SkillCooccurrence]:
    """Skill pairs ranked by association strength."""
    return await analytics.cooccurrence(flt, limit=limit, skill=skill)


@router.get("/skill-graph", response_model=SkillGraph, summary="Skill relationship graph")
async def skill_graph(
    analytics: AnalyticsServiceDep,
    flt: AnalyticsFilterDep,
    limit: Annotated[int, Query(ge=5, le=100)] = 40,
) -> SkillGraph:
    """Nodes and weighted edges for a force-directed view."""
    return await analytics.skill_graph(flt, limit=limit)


@router.get("/salaries", response_model=SalaryStatistics, summary="Salary statistics")
async def salaries(analytics: AnalyticsServiceDep, flt: AnalyticsFilterDep) -> SalaryStatistics:
    """Statistics over disclosed salaries only."""
    return await analytics.salaries(flt)


@router.get(
    "/salaries/by/{dimension}",
    response_model=list[SalaryBreakdown],
    summary="Salary by dimension",
)
async def salaries_by(
    analytics: AnalyticsServiceDep,
    flt: AnalyticsFilterDep,
    dimension: str,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[SalaryBreakdown]:
    """Salary statistics grouped by segment, seniority, country, city, ..."""
    if dimension not in SUPPORTED_DIMENSIONS:
        raise InvalidRequestError(
            f"unsupported dimension {dimension!r}",
            details={"supported": list(SUPPORTED_DIMENSIONS)},
        )
    return await analytics.salaries_by(flt, dimension=dimension, limit=limit)


@router.get("/locations", response_model=list[LocationDemand], summary="Geographic demand")
async def locations(
    analytics: AnalyticsServiceDep,
    flt: AnalyticsFilterDep,
    by: Annotated[str, Query(pattern="^(country|city)$")] = "country",
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> list[LocationDemand]:
    """Demand per country or per city."""
    return await analytics.locations(flt, by=by, limit=limit)


@router.get("/remote", response_model=RemoteBreakdown, summary="Remote-work breakdown")
async def remote(analytics: AnalyticsServiceDep, flt: AnalyticsFilterDep) -> RemoteBreakdown:
    """Remote / hybrid / on-site split and its recent change."""
    return await analytics.remote(flt)


@router.get("/seniority", response_model=SeniorityBreakdown, summary="Seniority breakdown")
async def seniority(analytics: AnalyticsServiceDep, flt: AnalyticsFilterDep) -> SeniorityBreakdown:
    """Distribution of experience levels."""
    return await analytics.seniority(flt)


@router.get("/segments", response_model=list[SegmentSummary], summary="Market segments")
async def segments(
    analytics: AnalyticsServiceDep,
    flt: AnalyticsFilterDep,
    limit: Annotated[int, Query(ge=1, le=30)] = 15,
) -> list[SegmentSummary]:
    """Comparable summary per market segment."""
    return await analytics.segments(flt, limit=limit)


@router.get("/companies", response_model=list[CompanyHiring], summary="Company hiring")
async def companies(
    analytics: AnalyticsServiceDep,
    flt: AnalyticsFilterDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    with_skills: bool = False,
) -> list[CompanyHiring]:
    """Companies ranked by open postings."""
    return await analytics.companies(flt, limit=limit, with_skills=with_skills)


__all__ = ["router"]
