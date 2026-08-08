"""Skill taxonomy, trends and the skill explorer."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query

from app.api.deps import AnalyticsFilterDep, AnalyticsServiceDep, require_scope
from app.core.errors import NotFoundError
from app.models.analytics import SkillDemand, SkillExplorerView, SkillTrend
from app.models.enums import Scope, SkillCategory
from app.nlp.skills.taxonomy import get_taxonomy

router = APIRouter(
    prefix="/skills", tags=["skills"], dependencies=[require_scope(Scope.ANALYTICS_READ)]
)

SkillSlug = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9\-.+#]*$")]


@router.get("", response_model=list[SkillDemand], summary="List skills by demand")
async def list_skills(analytics: AnalyticsServiceDep, flt: AnalyticsFilterDep) -> list[SkillDemand]:
    """Skills ranked by how many postings mention them."""
    return await analytics.skills(flt)


@router.get("/taxonomy", summary="The configured skill taxonomy")
async def taxonomy() -> dict[str, Any]:
    """The hierarchy the NLP layer recognises."""
    return get_taxonomy().tree()


@router.get("/{slug}/trend", response_model=SkillTrend, summary="Demand trend for a skill")
async def skill_trend(
    analytics: AnalyticsServiceDep,
    slug: SkillSlug,
    category: SkillCategory | None = None,
) -> SkillTrend:
    """Multi-window trend for one skill."""
    node = get_taxonomy().get(slug)
    return await analytics.skill_trend(
        slug,
        name=node.name if node else slug,
        category=category or (node.category if node else SkillCategory.OTHER),
    )


@router.get(
    "/{slug}/explorer", response_model=SkillExplorerView, summary="Everything about a skill"
)
async def skill_explorer(
    analytics: AnalyticsServiceDep,
    flt: AnalyticsFilterDep,
    slug: SkillSlug,
) -> SkillExplorerView:
    """Trend, related skills, companies, locations, salary and remote share."""
    view = await analytics.skill_explorer(slug, flt)
    if view is None:
        raise NotFoundError(
            f"no demand data for skill {slug!r} in this window", details={"skill": slug}
        )
    return view


@router.get("/{slug}/related", summary="Skills that appear alongside this one")
async def related_skills(
    analytics: AnalyticsServiceDep,
    flt: AnalyticsFilterDep,
    slug: SkillSlug,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[dict[str, Any]]:
    """Co-occurring skills, most strongly associated first."""
    pairs = await analytics.cooccurrence(flt, limit=limit, skill=slug)
    return [
        {
            "skill": pair.skill_b if pair.skill_a == slug else pair.skill_a,
            "cooccurrence_count": pair.cooccurrence_count,
            "support": pair.support,
            "lift": pair.lift,
            "association_score": pair.association_score,
        }
        for pair in pairs
    ]


__all__ = ["router"]
