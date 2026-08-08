"""Market alert rules and notifications."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, status

from app.api.deps import AlertServiceDep, PrincipalDep, require_scope
from app.api.schemas import AlertRuleInput
from app.core.errors import NotFoundError
from app.models.alerts import AlertRule, Notification
from app.models.enums import Scope

router = APIRouter(prefix="/alerts", tags=["alerts"])

RuleId = Annotated[str, Path(min_length=8, max_length=32, pattern=r"^[0-9a-f]+$")]
NotificationId = Annotated[str, Path(min_length=8, max_length=32, pattern=r"^[0-9a-f]+$")]


def _owner(principal: PrincipalDep) -> str | None:
    """Scope rules to the API key that created them."""
    return principal.key_id if principal else None


@router.get(
    "/rules",
    response_model=list[AlertRule],
    summary="List alert rules",
    dependencies=[require_scope(Scope.ALERTS_READ)],
)
async def list_rules(
    alerts: AlertServiceDep,
    principal: PrincipalDep,
    enabled_only: bool = False,
) -> list[AlertRule]:
    """Rules owned by the calling key."""
    return await alerts.list_rules(owner=_owner(principal), enabled_only=enabled_only)


@router.post(
    "/rules",
    response_model=AlertRule,
    status_code=status.HTTP_201_CREATED,
    summary="Create an alert rule",
    dependencies=[require_scope(Scope.ALERTS_WRITE)],
)
async def create_rule(
    alerts: AlertServiceDep, principal: PrincipalDep, payload: AlertRuleInput
) -> AlertRule:
    """Create a rule. Validation of channel prerequisites happens in the model."""
    rule = AlertRule(**payload.model_dump(), owner=_owner(principal))
    return await alerts.create_rule(rule)


@router.get(
    "/rules/{rule_id}",
    response_model=AlertRule,
    summary="Get one alert rule",
    dependencies=[require_scope(Scope.ALERTS_READ)],
)
async def get_rule(alerts: AlertServiceDep, rule_id: RuleId) -> AlertRule:
    return await alerts.get_rule(rule_id)


@router.put(
    "/rules/{rule_id}",
    response_model=AlertRule,
    summary="Update an alert rule",
    dependencies=[require_scope(Scope.ALERTS_WRITE)],
)
async def update_rule(
    alerts: AlertServiceDep,
    principal: PrincipalDep,
    rule_id: RuleId,
    payload: AlertRuleInput,
) -> AlertRule:
    """Replace a rule's configuration, keeping its identity and history."""
    existing = await alerts.get_rule(rule_id)
    updated = AlertRule(
        **payload.model_dump(),
        id=existing.id,
        owner=_owner(principal) or existing.owner,
        created_at=existing.created_at,
        last_triggered_at=existing.last_triggered_at,
    )
    return await alerts.update_rule(updated)


@router.delete(
    "/rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Delete an alert rule",
    dependencies=[require_scope(Scope.ALERTS_WRITE)],
)
async def delete_rule(alerts: AlertServiceDep, rule_id: RuleId) -> None:
    if not await alerts.delete_rule(rule_id):
        raise NotFoundError(f"alert rule {rule_id} was not found")


@router.post(
    "/evaluate",
    summary="Evaluate every rule now",
    dependencies=[require_scope(Scope.ALERTS_WRITE)],
)
async def evaluate(alerts: AlertServiceDep, principal: PrincipalDep) -> dict[str, Any]:
    """Run the alert engine immediately instead of waiting for the scheduler."""
    result = await alerts.evaluate_all(owner=_owner(principal))
    return {
        "evaluated": result.evaluated,
        "triggered": result.triggered,
        "notifications": len(result.notifications),
        "failures": result.failures[:10],
    }


@router.get(
    "/notifications",
    response_model=list[Notification],
    summary="List notifications",
    dependencies=[require_scope(Scope.ALERTS_READ)],
)
async def list_notifications(
    alerts: AlertServiceDep,
    rule_id: Annotated[str | None, Query(max_length=32)] = None,
    unread_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[Notification]:
    return await alerts.list_notifications(rule_id=rule_id, unread_only=unread_only, limit=limit)


@router.post(
    "/notifications/{notification_id}/read",
    summary="Mark a notification as read",
    dependencies=[require_scope(Scope.ALERTS_WRITE)],
)
async def mark_read(alerts: AlertServiceDep, notification_id: NotificationId) -> dict[str, bool]:
    return {"updated": await alerts.mark_read(notification_id)}


__all__ = ["router"]
