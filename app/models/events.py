"""Events exchanged over the message broker.

Events are intentionally small: they carry identifiers and a compact payload,
never whole job documents. Workers re-read the authoritative record from the
database, which keeps the queue cheap and avoids stale copies.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from app.core.timeutils import utcnow
from app.models.enums import EventType


class PipelineEvent(BaseModel):
    """A single message on the event bus."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    type: EventType
    occurred_at: datetime = Field(default_factory=utcnow)
    source: str | None = None
    job_id: str | None = None
    ingestion_run_id: str | None = None
    correlation_id: str | None = None
    attempt: int = Field(default=0, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_wire(self) -> dict[str, str]:
        """Flatten into the string map Redis Streams expects."""
        return {"data": self.model_dump_json()}

    @classmethod
    def from_wire(cls, fields: dict[str, str]) -> Self:
        """Rebuild an event from its wire representation."""
        return cls.model_validate_json(fields["data"])

    def retried(self) -> PipelineEvent:
        """Return a copy with an incremented attempt counter."""
        return self.model_copy(update={"attempt": self.attempt + 1})


__all__ = ["PipelineEvent"]
