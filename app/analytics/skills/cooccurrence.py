"""Skill co-occurrence analysis.

Which technologies are asked for together? The answer is an association-rule
problem, so the standard measures are reported rather than a raw count:

    support(A,B)    = P(A and B)                 - how common is the pair?
    confidence(A→B) = P(B | A)                   - how predictive is A of B?
    lift(A,B)       = P(A and B) / (P(A) P(B))   - is the pairing more than chance?

Lift is the one that matters: ``Python + English`` has enormous support and no
information, because both are everywhere. ``Python + FastAPI`` has less support
and far more lift.
"""

from __future__ import annotations

import math

from app.core.config import AnalyticsSettings
from app.core.logging import get_logger
from app.models.analytics import AnalyticsFilter, SkillCooccurrence, SkillDemand, SkillGraph
from app.models.enums import SkillCategory
from app.storage.repositories.analytics import AnalyticsRepository

log = get_logger(__name__)


class CooccurrenceAnalyzer:
    """Builds the skill relationship graph."""

    def __init__(
        self,
        repository: AnalyticsRepository,
        settings: AnalyticsSettings | None = None,
    ) -> None:
        self._repo = repository
        self._settings = settings or AnalyticsSettings()

    async def pairs(
        self,
        flt: AnalyticsFilter,
        *,
        limit: int = 50,
        min_count: int = 3,
        skill: str | None = None,
    ) -> list[SkillCooccurrence]:
        """Strongest skill pairings in the filtered window."""
        total_jobs = await self._repo.jobs_with_skill_count(flt)
        if total_jobs == 0:
            return []

        counts = {row[0]: row[3] for row in await self._repo.skill_counts(flt, limit=400)}
        raw_pairs = await self._repo.skill_pairs(
            flt, min_count=min_count, limit=max(limit * 6, 200)
        )

        results: list[SkillCooccurrence] = []
        for left, right, pair_count in raw_pairs:
            if skill and skill not in (left, right):
                continue
            count_a = counts.get(left, 0)
            count_b = counts.get(right, 0)
            if count_a == 0 or count_b == 0:
                continue

            support = pair_count / total_jobs
            if support < self._settings.min_cooccurrence_support:
                continue

            confidence_ab = pair_count / count_a
            confidence_ba = pair_count / count_b
            expected = (count_a / total_jobs) * (count_b / total_jobs)
            lift = support / expected if expected > 0 else 0.0

            results.append(
                SkillCooccurrence(
                    skill_a=left,
                    skill_b=right,
                    cooccurrence_count=pair_count,
                    support=round(support, 5),
                    confidence_a_to_b=round(confidence_ab, 4),
                    confidence_b_to_a=round(confidence_ba, 4),
                    lift=round(lift, 4),
                    association_score=round(self._association_score(support, lift), 4),
                )
            )

        results.sort(key=lambda item: item.association_score, reverse=True)
        return results[:limit]

    async def related(
        self, slug: str, flt: AnalyticsFilter, *, limit: int = 10
    ) -> list[SkillCooccurrence]:
        """Skills most strongly associated with one skill."""
        return await self.pairs(flt, limit=limit, min_count=2, skill=slug)

    async def graph(self, flt: AnalyticsFilter, *, limit: int = 40) -> SkillGraph:
        """Nodes plus weighted edges, ready for a force-directed layout."""
        demand_rows = await self._repo.skill_counts(flt, limit=limit)
        total_jobs = await self._repo.jobs_with_skill_count(flt)
        nodes = [
            SkillDemand(
                slug=slug,
                name=name,
                category=_safe_category(category),
                job_count=count,
                share=round(count / total_jobs, 4) if total_jobs else 0.0,
                rank=index + 1,
                average_confidence=round(confidence, 3),
            )
            for index, (slug, name, category, count, confidence) in enumerate(demand_rows)
        ]
        known = {node.slug for node in nodes}
        edges = [
            edge
            for edge in await self.pairs(flt, limit=limit * 4, min_count=2)
            if edge.skill_a in known and edge.skill_b in known
        ]
        return SkillGraph(nodes=nodes, edges=edges, total_jobs=total_jobs)

    @staticmethod
    def _association_score(support: float, lift: float) -> float:
        """Rank pairings by lift, tempered by how much evidence supports them.

        Using ``log1p(lift)`` keeps a single freak pairing with lift 200 from
        dominating the ranking, while ``sqrt(support)`` rewards pairs that are
        actually common.
        """
        if lift <= 0:
            return 0.0
        return math.log1p(lift) * math.sqrt(support)


def _safe_category(value: str) -> SkillCategory:
    try:
        return SkillCategory(value)
    except ValueError:
        return SkillCategory.OTHER


__all__ = ["CooccurrenceAnalyzer"]
