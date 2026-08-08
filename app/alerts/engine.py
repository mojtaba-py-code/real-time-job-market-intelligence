"""Alert rule evaluation.

A rule names a metric, a subject, a comparison and a threshold. The engine
resolves the metric against the analytics layer, applies the comparison and
produces an :class:`AlertEvaluation`. Delivery is somebody else's job - keeping
evaluation pure means the rules can be tested without SMTP, webhooks or a
database.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.analytics.geography.analyzer import GeographyAnalytics
from app.analytics.salary.analyzer import SalaryAnalytics
from app.analytics.trends.emerging import EmergingTechnologyDetector
from app.analytics.trends.engine import TrendEngine
from app.core.config import AnalyticsSettings
from app.core.logging import get_logger
from app.models.alerts import AlertEvaluation, AlertRule
from app.models.analytics import AnalyticsFilter
from app.models.enums import AlertMetric
from app.storage.repositories.analytics import AnalyticsRepository

log = get_logger(__name__)


@dataclass(slots=True)
class MetricReading:
    """A resolved metric value plus the context that explains it."""

    value: float | None
    context: dict[str, object]
    description: str


class AlertEngine:
    """Evaluates alert rules against current market analytics."""

    def __init__(
        self,
        repository: AnalyticsRepository,
        *,
        settings: AnalyticsSettings | None = None,
        trends: TrendEngine | None = None,
        emerging: EmergingTechnologyDetector | None = None,
        salaries: SalaryAnalytics | None = None,
        geography: GeographyAnalytics | None = None,
    ) -> None:
        self._repo = repository
        self._settings = settings or AnalyticsSettings()
        self._trends = trends or TrendEngine(repository, self._settings)
        self._emerging = emerging or EmergingTechnologyDetector(repository, self._settings)
        self._salaries = salaries or SalaryAnalytics(repository, self._settings)
        self._geography = geography or GeographyAnalytics(repository, self._settings)

    async def evaluate(self, rule: AlertRule) -> AlertEvaluation:
        """Resolve one rule into a triggered / not-triggered verdict."""
        reading = await self._read(rule)
        if reading.value is None:
            return AlertEvaluation(
                rule_id=rule.id,
                rule_name=rule.name,
                triggered=False,
                threshold=rule.threshold,
                message=f"{reading.description}: no data available",
                context=reading.context,
            )

        triggered = rule.operator.compare(reading.value, rule.threshold)
        message = (
            f"{reading.description} is {reading.value:,.2f} "
            f"({rule.operator.value} {rule.threshold:,.2f})"
        )
        return AlertEvaluation(
            rule_id=rule.id,
            rule_name=rule.name,
            triggered=triggered,
            observed_value=round(reading.value, 4),
            threshold=rule.threshold,
            message=message,
            context=reading.context,
        )

    async def evaluate_many(self, rules: list[AlertRule]) -> list[AlertEvaluation]:
        """Evaluate every enabled rule that is not in cooldown."""
        results: list[AlertEvaluation] = []
        for rule in rules:
            if not rule.enabled or rule.is_in_cooldown():
                continue
            try:
                results.append(await self.evaluate(rule))
            except Exception as exc:
                log.exception("alerts.rule_failed", rule_id=rule.id, error=str(exc))
        return results

    # ------------------------------------------------------------------ #
    # Metric resolution
    # ------------------------------------------------------------------ #
    async def _read(self, rule: AlertRule) -> MetricReading:
        flt = AnalyticsFilter(window_days=rule.window_days, limit=50)
        subject = (rule.subject or "").strip()

        match rule.metric:
            case AlertMetric.SKILL_DEMAND_CHANGE_PCT:
                trend = await self._trends.skill_trend(subject, windows=(rule.window_days,))
                window = next((w for w in trend.windows if w.days == rule.window_days), None)
                return MetricReading(
                    value=window.change_pct if window else None,
                    context={
                        "skill": subject,
                        "window_days": rule.window_days,
                        "current": window.current_count if window else 0,
                        "previous": window.previous_count if window else 0,
                        "direction": str(trend.direction),
                    },
                    description=f"demand change for {subject!r} over {rule.window_days} days",
                )

            case AlertMetric.SKILL_JOB_COUNT:
                counts = await self._repo.skill_counts(
                    flt.model_copy(update={"skill": None, "limit": 300}), limit=300
                )
                match = next((row for row in counts if row[0] == subject), None)
                return MetricReading(
                    value=float(match[3]) if match else 0.0,
                    context={"skill": subject, "window_days": rule.window_days},
                    description=f"job count for {subject!r}",
                )

            case AlertMetric.EMERGING_SKILL:
                emerging = await self._emerging.detect(window_days=rule.window_days, limit=10)
                if subject:
                    hit = next((item for item in emerging if item.slug == subject), None)
                    value = hit.confidence if hit else 0.0
                    context: dict[str, object] = {"skill": subject}
                    if hit:
                        context.update(
                            {"growth_pct": hit.growth_pct, "current_count": hit.current_count}
                        )
                    return MetricReading(
                        value=value,
                        context=context,
                        description=f"emerging confidence for {subject!r}",
                    )
                return MetricReading(
                    value=float(len(emerging)),
                    context={"skills": [item.slug for item in emerging]},
                    description="number of emerging technologies",
                )

            case AlertMetric.COMPANY_JOB_COUNT:
                companies = await self._repo.company_counts(flt, limit=200)
                company = next(
                    (row for row in companies if row[1].lower() == subject.lower()), None
                )
                return MetricReading(
                    value=float(company[2]) if company else 0.0,
                    context={"company": subject, "window_days": rule.window_days},
                    description=f"postings by {subject!r}",
                )

            case AlertMetric.AVERAGE_SALARY:
                stats = await self._salaries.overview(flt)
                return MetricReading(
                    value=stats.average,
                    context={
                        "sample_size": stats.sample_size,
                        "currency": stats.currency,
                        "median": stats.median,
                    },
                    description="average disclosed salary",
                )

            case AlertMetric.REMOTE_SHARE_PCT:
                remote = await self._geography.remote_breakdown(flt)
                return MetricReading(
                    value=remote.remote_share * 100,
                    context={"total": remote.total, "change_pct": remote.change_pct},
                    description="share of remote postings",
                )

            case AlertMetric.TOTAL_ACTIVE_JOBS:
                total = await self._repo.active_job_count()
                return MetricReading(
                    value=float(total),
                    context={},
                    description="active postings",
                )

        raise ValueError(f"unsupported alert metric: {rule.metric}")  # pragma: no cover


__all__ = ["AlertEngine", "MetricReading"]
