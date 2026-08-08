"""Alert lifecycle: rules in, notifications out."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.alerts.engine import AlertEngine
from app.alerts.notifiers import NotifierRegistry
from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.core.timeutils import utcnow
from app.events.bus import EventBus, job_event
from app.models.alerts import AlertEvaluation, AlertRule, Notification
from app.models.enums import AlertChannel, EventType
from app.storage.repositories.alerts import AlertRepository
from app.storage.repositories.analytics import AnalyticsRepository
from app.storage.session import Database

log = get_logger(__name__)


@dataclass(slots=True)
class AlertRunResult:
    """What one evaluation pass produced."""

    evaluated: int = 0
    triggered: int = 0
    notifications: list[Notification] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


class AlertService:
    """Stores rules, evaluates them and delivers the notifications."""

    def __init__(
        self,
        database: Database,
        *,
        settings: Settings | None = None,
        notifiers: NotifierRegistry | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._db = database
        self._settings = settings or get_settings()
        self._notifiers = notifiers or NotifierRegistry.default(self._settings.alerts)
        self._bus = event_bus

    # ------------------------------------------------------------------ #
    # Rules
    # ------------------------------------------------------------------ #
    async def create_rule(self, rule: AlertRule) -> AlertRule:
        async with self._db.session() as session:
            return await AlertRepository(session).create(rule)

    async def update_rule(self, rule: AlertRule) -> AlertRule:
        async with self._db.session() as session:
            return await AlertRepository(session).update(rule)

    async def get_rule(self, rule_id: str) -> AlertRule:
        async with self._db.read_session() as session:
            rule = await AlertRepository(session).get(rule_id)
        if rule is None:
            raise NotFoundError(f"alert rule {rule_id} was not found")
        return rule

    async def list_rules(
        self, *, owner: str | None = None, enabled_only: bool = False
    ) -> list[AlertRule]:
        async with self._db.read_session() as session:
            return await AlertRepository(session).list_rules(owner=owner, enabled_only=enabled_only)

    async def delete_rule(self, rule_id: str) -> bool:
        async with self._db.session() as session:
            return await AlertRepository(session).delete(rule_id)

    # ------------------------------------------------------------------ #
    # Notifications
    # ------------------------------------------------------------------ #
    async def list_notifications(
        self, *, rule_id: str | None = None, unread_only: bool = False, limit: int = 50
    ) -> list[Notification]:
        async with self._db.read_session() as session:
            return await AlertRepository(session).list_notifications(
                rule_id=rule_id, unread_only=unread_only, limit=limit
            )

    async def mark_read(self, notification_id: str) -> bool:
        async with self._db.session() as session:
            return await AlertRepository(session).mark_read(notification_id)

    # ------------------------------------------------------------------ #
    # Evaluation
    # ------------------------------------------------------------------ #
    async def evaluate_all(self, *, owner: str | None = None) -> AlertRunResult:
        """Evaluate every enabled rule and deliver what fired."""
        result = AlertRunResult()
        if not self._settings.alerts.enabled:
            return result

        async with self._db.read_session() as session:
            rules = await AlertRepository(session).list_rules(owner=owner, enabled_only=True)
            engine = AlertEngine(AnalyticsRepository(session), settings=self._settings.analytics)
            evaluations = await engine.evaluate_many(rules)

        result.evaluated = len(evaluations)
        by_id = {rule.id: rule for rule in rules}
        pending: list[Notification] = []

        for evaluation in evaluations:
            if not evaluation.triggered:
                continue
            rule = by_id.get(evaluation.rule_id)
            if rule is None:
                continue
            result.triggered += 1
            pending.extend(self._build_notifications(rule, evaluation))

        delivered = await self._deliver(pending[: self._settings.alerts.max_notifications_per_run])
        result.notifications = delivered
        result.failures = [n.error for n in delivered if n.error]

        if delivered:
            async with self._db.session() as session:
                repository = AlertRepository(session)
                await repository.add_notifications(delivered)
                for rule_id in {n.rule_id for n in delivered}:
                    await repository.mark_triggered(rule_id, at=utcnow())

        if self._bus is not None and result.triggered:
            await self._bus.publish(job_event(EventType.ALERT_TRIGGERED, count=result.triggered))
        log.info(
            "alerts.evaluated",
            evaluated=result.evaluated,
            triggered=result.triggered,
            delivered=len(delivered),
        )
        return result

    def _build_notifications(
        self, rule: AlertRule, evaluation: AlertEvaluation
    ) -> list[Notification]:
        """One notification per configured channel."""
        title = f"[{rule.name}] {evaluation.message[:150]}"
        body = _format_body(rule, evaluation)
        notifications: list[Notification] = []
        for channel in rule.channels:
            context: dict[str, object] = {**evaluation.context, "metric": str(rule.metric)}
            if channel is AlertChannel.WEBHOOK and rule.webhook_url:
                context["webhook_url"] = str(rule.webhook_url)
            if channel is AlertChannel.EMAIL and rule.email_to:
                context["email_to"] = rule.email_to
            notifications.append(
                Notification(
                    rule_id=rule.id,
                    channel=channel,
                    title=title[:200],
                    body=body[:4000],
                    context=context,
                )
            )
        return notifications

    async def _deliver(self, notifications: list[Notification]) -> list[Notification]:
        delivered: list[Notification] = []
        for notification in notifications:
            try:
                delivered.append(await self._notifiers.deliver(notification))
            except Exception as exc:
                log.warning(
                    "alerts.delivery_failed",
                    channel=str(notification.channel),
                    error=str(exc),
                )
                delivered.append(
                    notification.model_copy(update={"delivered": False, "error": str(exc)[:512]})
                )
        return delivered


def _format_body(rule: AlertRule, evaluation: AlertEvaluation) -> str:
    """Plain-text alert body."""
    lines = [
        evaluation.message,
        "",
        f"Rule:      {rule.name}",
        f"Metric:    {rule.metric}",
        f"Subject:   {rule.subject or '-'}",
        f"Condition: {rule.operator.value} {rule.threshold}",
        f"Window:    {rule.window_days} days",
        f"Observed:  {evaluation.observed_value}",
        f"Evaluated: {evaluation.evaluated_at.isoformat()}",
    ]
    if evaluation.context:
        lines.append("")
        lines.append("Context:")
        lines.extend(f"  {key}: {value}" for key, value in sorted(evaluation.context.items()))
    return "\n".join(lines)


__all__ = ["AlertRunResult", "AlertService"]
