"""Alert rules, evaluation results and notification records."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from app.core.timeutils import utcnow
from app.models.enums import AlertChannel, AlertMetric, ComparisonOperator


class AlertRule(BaseModel):
    """A user-configured market alert.

    Example: *"tell me when demand for Python grows by more than 10% over 7
    days"* becomes ``metric=SKILL_DEMAND_CHANGE_PCT, subject="python",
    operator=GT, threshold=10, window_days=7``.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, max_length=32)
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    metric: AlertMetric
    subject: str | None = Field(default=None, max_length=128)
    operator: ComparisonOperator = ComparisonOperator.GT
    threshold: float = 0.0
    window_days: int = Field(default=7, ge=1, le=365)
    channels: list[AlertChannel] = Field(default_factory=lambda: [AlertChannel.IN_APP])
    email_to: str | None = Field(default=None, max_length=254)
    webhook_url: HttpUrl | None = None
    cooldown_minutes: int = Field(default=60, ge=0, le=10_080)
    enabled: bool = True
    owner: str | None = Field(default=None, max_length=128)
    created_at: datetime = Field(default_factory=utcnow)
    last_triggered_at: datetime | None = None

    @model_validator(mode="after")
    def _validate_channels(self) -> Self:
        if AlertChannel.EMAIL in self.channels and not self.email_to:
            raise ValueError("email_to is required when the email channel is enabled")
        if AlertChannel.WEBHOOK in self.channels and self.webhook_url is None:
            raise ValueError("webhook_url is required when the webhook channel is enabled")
        if self.metric in _SUBJECT_REQUIRED and not self.subject:
            raise ValueError(f"subject is required for metric {self.metric}")
        return self

    def is_in_cooldown(self, *, now: datetime | None = None) -> bool:
        """Whether the rule fired too recently to fire again."""
        if self.last_triggered_at is None or self.cooldown_minutes == 0:
            return False
        reference = now or utcnow()
        elapsed = (reference - self.last_triggered_at).total_seconds()
        return elapsed < self.cooldown_minutes * 60


_SUBJECT_REQUIRED = frozenset(
    {
        AlertMetric.SKILL_DEMAND_CHANGE_PCT,
        AlertMetric.SKILL_JOB_COUNT,
        AlertMetric.COMPANY_JOB_COUNT,
    }
)


class AlertEvaluation(BaseModel):
    """Outcome of evaluating one rule."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    rule_name: str
    triggered: bool
    observed_value: float | None = None
    threshold: float | None = None
    message: str = ""
    context: dict[str, Any] = Field(default_factory=dict)
    evaluated_at: datetime = Field(default_factory=utcnow)


class Notification(BaseModel):
    """A delivered (or attempted) notification."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, max_length=32)
    rule_id: str
    channel: AlertChannel
    title: str = Field(max_length=200)
    body: str = Field(max_length=4000)
    delivered: bool = False
    error: str | None = Field(default=None, max_length=512)
    created_at: datetime = Field(default_factory=utcnow)
    read_at: datetime | None = None
    context: dict[str, Any] = Field(default_factory=dict)


__all__ = ["AlertEvaluation", "AlertRule", "Notification"]
