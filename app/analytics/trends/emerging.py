"""Emerging-technology detection.

"Emerging" is defined statistically rather than by editorial opinion. A skill
qualifies when all of the following hold:

* its **baseline share** of the market was small (it is not already mainstream);
* its **growth** between the baseline window and the current window exceeds a
  configured percentage;
* its **current volume** clears a floor, so a jump from 1 to 3 postings does
  not become a headline;
* its growth is an **outlier** relative to the growth of every other skill,
  measured with a z-score - which is what separates a genuine shift from the
  whole market expanding.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.analytics.statistics import confidence_from_sample, percent_change, z_score
from app.core.config import AnalyticsSettings
from app.core.logging import get_logger
from app.core.timeutils import utcnow
from app.models.analytics import AnalyticsFilter, EmergingSkill
from app.models.enums import SkillCategory
from app.storage.repositories.analytics import AnalyticsRepository

log = get_logger(__name__)

#: Upper bound of ``AnalyticsFilter.window_days``.
MAX_WINDOW_DAYS = 1825


class EmergingTechnologyDetector:
    """Finds technologies whose demand is accelerating from a small base."""

    def __init__(
        self,
        repository: AnalyticsRepository,
        settings: AnalyticsSettings | None = None,
    ) -> None:
        self._repo = repository
        self._settings = settings or AnalyticsSettings()

    async def detect(
        self,
        *,
        window_days: int = 30,
        limit: int = 10,
        reference: datetime | None = None,
    ) -> list[EmergingSkill]:
        """Return the emerging technologies, most confident first."""
        now = reference or utcnow()
        current_start = now - timedelta(days=window_days)
        baseline_start = current_start - timedelta(days=window_days)

        # Candidates come from both windows; the filter's own upper bound
        # keeps a very long look-back from overflowing it.
        lookback = min(window_days * 2, MAX_WINDOW_DAYS)
        candidates = await self._repo.skill_counts(
            AnalyticsFilter(window_days=lookback, limit=500), limit=500
        )
        slugs = [row[0] for row in candidates]
        names = {row[0]: (row[1], row[2]) for row in candidates}
        if not slugs:
            return []

        current = await self._repo.skill_counts_between(slugs, start=current_start, end=now)
        baseline = await self._repo.skill_counts_between(
            slugs, start=baseline_start, end=current_start
        )
        total_current = sum(current.values()) or 1
        total_baseline = sum(baseline.values()) or 1

        growths: dict[str, float] = {}
        for slug in slugs:
            change = percent_change(current.get(slug, 0), baseline.get(slug, 0))
            if change is not None:
                growths[slug] = change
        growth_population = list(growths.values())

        results: list[EmergingSkill] = []
        for slug in slugs:
            current_count = current.get(slug, 0)
            baseline_count = baseline.get(slug, 0)
            if current_count < self._settings.emerging_min_current_count:
                continue

            baseline_share = baseline_count / total_baseline
            if baseline_share > self._settings.emerging_max_baseline_share:
                continue  # already mainstream

            growth = growths.get(slug)
            if growth is None:
                # No baseline at all: a brand-new technology. Treat the arrival
                # itself as the signal, scaled by how much of it we see.
                arrival_confidence = confidence_from_sample(current_count, full_confidence_at=30)
                results.append(
                    EmergingSkill(
                        slug=slug,
                        name=names.get(slug, (slug, "other"))[0],
                        category=_safe_category(names.get(slug, (slug, "other"))[1]),
                        current_count=current_count,
                        previous_count=0,
                        growth_pct=None,
                        baseline_share=round(baseline_share, 6),
                        z_score=None,
                        confidence=round(arrival_confidence * 0.8, 4),
                    )
                )
                continue

            if growth < self._settings.emerging_min_growth_pct:
                continue

            score = z_score(growth, growth_population)
            if score is not None and score < self._settings.emerging_zscore_threshold:
                continue

            confidence = self._confidence(
                growth=growth,
                z=score,
                current_count=current_count,
                current_share=current_count / total_current,
            )
            results.append(
                EmergingSkill(
                    slug=slug,
                    name=names.get(slug, (slug, "other"))[0],
                    category=_safe_category(names.get(slug, (slug, "other"))[1]),
                    current_count=current_count,
                    previous_count=baseline_count,
                    growth_pct=round(growth, 2),
                    baseline_share=round(baseline_share, 6),
                    z_score=round(score, 3) if score is not None else None,
                    confidence=confidence,
                )
            )

        results.sort(key=lambda item: (item.confidence, item.current_count), reverse=True)
        return results[:limit]

    def _confidence(
        self, *, growth: float, z: float | None, current_count: int, current_share: float
    ) -> float:
        """Blend growth magnitude, statistical outlier-ness and sample size."""
        volume_confidence = confidence_from_sample(current_count, full_confidence_at=40)
        growth_confidence = min(1.0, growth / (self._settings.emerging_min_growth_pct * 3))
        outlier_confidence = min(1.0, (z or 0.0) / (self._settings.emerging_zscore_threshold * 2))
        # A technology that is already 5% of the market is not "emerging", so a
        # large current share damps the score.
        maturity_penalty = max(0.0, 1.0 - current_share * 10)
        blended = (
            0.4 * volume_confidence + 0.3 * growth_confidence + 0.3 * outlier_confidence
        ) * max(0.3, maturity_penalty)
        return round(min(1.0, blended), 4)


def _safe_category(value: str) -> SkillCategory:
    try:
        return SkillCategory(value)
    except ValueError:
        return SkillCategory.OTHER


__all__ = ["EmergingTechnologyDetector"]
