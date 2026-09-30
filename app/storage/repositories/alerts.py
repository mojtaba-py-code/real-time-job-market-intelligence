"""Persistence for alert rules and the notifications they produce."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, insert, select, update

from app.core.timeutils import utcnow
from app.models.alerts import AlertRule, Notification
from app.storage.mappers import (
    alert_rule_to_domain,
    alert_rule_to_values,
    notification_to_domain,
)
from app.storage.models import AlertRuleRow, NotificationRow
from app.storage.repositories.base import BaseRepository, affected_rows


class AlertRepository(BaseRepository):
    """CRUD for alert rules plus notification bookkeeping."""

    async def create(self, rule: AlertRule) -> AlertRule:
        self.session.add(AlertRuleRow(**alert_rule_to_values(rule)))
        await self.session.flush()
        return rule

    async def update(self, rule: AlertRule) -> AlertRule:
        row = await self.session.get(AlertRuleRow, rule.id)
        if row is None:
            return await self.create(rule)
        for key, value in alert_rule_to_values(rule).items():
            setattr(row, key, value)
        await self.session.flush()
        return rule

    async def get(self, rule_id: str) -> AlertRule | None:
        row = await self.session.get(AlertRuleRow, rule_id)
        return alert_rule_to_domain(row) if row else None

    async def delete(self, rule_id: str) -> bool:
        result = await self.session.execute(delete(AlertRuleRow).where(AlertRuleRow.id == rule_id))
        return affected_rows(result) > 0

    async def list_rules(
        self, *, owner: str | None = None, enabled_only: bool = False, limit: int = 200
    ) -> list[AlertRule]:
        stmt = select(AlertRuleRow).order_by(AlertRuleRow.created_at.desc()).limit(limit)
        if owner is not None:
            stmt = stmt.where(AlertRuleRow.owner == owner)
        if enabled_only:
            stmt = stmt.where(AlertRuleRow.enabled.is_(True))
        rows = (await self.session.execute(stmt)).scalars()
        return [alert_rule_to_domain(row) for row in rows]

    async def mark_triggered(self, rule_id: str, *, at: datetime | None = None) -> None:
        await self.session.execute(
            update(AlertRuleRow)
            .where(AlertRuleRow.id == rule_id)
            .values(last_triggered_at=at or utcnow())
        )

    # ------------------------------------------------------------------ #
    # Notifications
    # ------------------------------------------------------------------ #
    async def add_notifications(self, notifications: list[Notification]) -> int:
        if not notifications:
            return 0
        rows = [
            {
                "id": item.id,
                "rule_id": item.rule_id,
                "channel": str(item.channel),
                "title": item.title[:200],
                "body": item.body[:4000],
                "delivered": item.delivered,
                "error": item.error,
                "created_at": item.created_at,
                "read_at": item.read_at,
                "context": dict(item.context),
            }
            for item in notifications
        ]
        for chunk in self._chunked(rows, 200):
            await self.session.execute(insert(NotificationRow), chunk)
        return len(rows)

    async def list_notifications(
        self, *, rule_id: str | None = None, unread_only: bool = False, limit: int = 100
    ) -> list[Notification]:
        stmt = select(NotificationRow).order_by(NotificationRow.created_at.desc()).limit(limit)
        if rule_id:
            stmt = stmt.where(NotificationRow.rule_id == rule_id)
        if unread_only:
            stmt = stmt.where(NotificationRow.read_at.is_(None))
        rows = (await self.session.execute(stmt)).scalars()
        return [notification_to_domain(row) for row in rows]

    async def mark_read(self, notification_id: str) -> bool:
        result = await self.session.execute(
            update(NotificationRow)
            .where(NotificationRow.id == notification_id)
            .where(NotificationRow.read_at.is_(None))
            .values(read_at=utcnow())
        )
        return affected_rows(result) > 0

    async def unread_count(self) -> int:
        stmt = (
            select(func.count())
            .select_from(NotificationRow)
            .where(NotificationRow.read_at.is_(None))
        )
        return int((await self.session.execute(stmt)).scalar_one())


__all__ = ["AlertRepository"]
