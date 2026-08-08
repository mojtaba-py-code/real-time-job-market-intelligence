"""The event-processing worker.

Ingestion publishes small events; this worker reacts to them. Keeping the
reaction out of the ingestion path means a slow analytics refresh never slows
down data collection, and a crashed worker only delays derived data - the
postings are already durable.

Delivery is at-least-once, so every handler must be idempotent. They are:
cache invalidation, snapshot upsert and analytics recomputation can all run
twice with the same result.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.core.config import Settings, get_settings
from app.core.logging import get_logger, log_context
from app.events.bus import EventBus
from app.models.enums import EventType
from app.models.events import PipelineEvent
from app.services.admin_service import AdminService
from app.services.alert_service import AlertService
from app.services.analytics_service import AnalyticsService

log = get_logger(__name__)

Handler = Callable[[PipelineEvent], Awaitable[None]]

#: An event that keeps failing is acknowledged and logged rather than retried
#: forever, so one poison message cannot stall the stream.
MAX_ATTEMPTS = 3


@dataclass(slots=True)
class WorkerStats:
    """What the worker has done since it started."""

    consumed: int = 0
    handled: int = 0
    failed: int = 0
    skipped: int = 0
    by_type: dict[str, int] = field(default_factory=dict)

    def record(self, event: PipelineEvent, *, handled: bool) -> None:
        self.consumed += 1
        key = str(event.type)
        self.by_type[key] = self.by_type.get(key, 0) + 1
        if handled:
            self.handled += 1
        else:
            self.skipped += 1


class EventProcessor:
    """Consumes pipeline events and refreshes everything derived from them."""

    def __init__(
        self,
        *,
        bus: EventBus,
        analytics: AnalyticsService,
        alerts: AlertService | None = None,
        admin: AdminService | None = None,
        settings: Settings | None = None,
        consumer_name: str = "processor-1",
    ) -> None:
        self._bus = bus
        self._analytics = analytics
        self._alerts = alerts
        self._admin = admin
        self._settings = settings or get_settings()
        self._consumer = consumer_name
        self._stats = WorkerStats()
        self._stopping = asyncio.Event()
        self._handlers: dict[EventType, Handler] = {
            EventType.INGESTION_COMPLETED: self._on_ingestion_completed,
            EventType.JOB_CREATED: self._on_job_created,
            EventType.JOB_EXPIRED: self._on_invalidate,
            EventType.ANALYTICS_REFRESHED: self._on_invalidate,
        }

    @property
    def stats(self) -> WorkerStats:
        return self._stats

    def register(self, event_type: EventType, handler: Handler) -> None:
        """Add or replace the handler for an event type."""
        self._handlers[event_type] = handler

    async def run(self) -> WorkerStats:
        """Consume until :meth:`stop` is called."""
        log.info("worker.started", consumer=self._consumer)
        consumer = self._bus.consume(
            consumer=self._consumer,
            batch_size=self._settings.events.batch_size,
            block_ms=self._settings.events.block_timeout_ms,
        )
        try:
            async for message_id, event in consumer:
                if self._stopping.is_set():
                    break
                await self._handle(message_id, event)
        except asyncio.CancelledError:
            log.info("worker.cancelled", consumer=self._consumer)
        finally:
            with contextlib.suppress(Exception):
                await consumer.aclose()  # type: ignore[attr-defined]
        log.info("worker.stopped", consumer=self._consumer, **self._stats.by_type)
        return self._stats

    async def stop(self) -> None:
        """Ask the consume loop to finish."""
        self._stopping.set()

    async def _handle(self, message_id: str, event: PipelineEvent) -> None:
        handler = self._handlers.get(event.type)
        if handler is None:
            self._stats.record(event, handled=False)
            await self._bus.ack(message_id)
            return

        with log_context(event_id=event.event_id, event_type=str(event.type)):
            try:
                await handler(event)
                self._stats.record(event, handled=True)
            except Exception as exc:
                self._stats.failed += 1
                log.exception("worker.handler_failed", error=str(exc), attempt=event.attempt)
                if event.attempt + 1 < MAX_ATTEMPTS:
                    await self._bus.publish(event.retried())
            finally:
                await self._bus.ack(message_id)

    # ------------------------------------------------------------------ #
    # Handlers
    # ------------------------------------------------------------------ #
    async def _on_ingestion_completed(self, event: PipelineEvent) -> None:
        """New data landed: derived views are stale."""
        await self._analytics.invalidate()
        if self._admin is not None:
            await self._admin.refresh_company_counts()
        if self._alerts is not None and self._settings.alerts.enabled:
            await self._alerts.evaluate_all()

    async def _on_job_created(self, event: PipelineEvent) -> None:
        """Individual postings do not justify a full refresh on their own."""
        return

    async def _on_invalidate(self, event: PipelineEvent) -> None:
        await self._analytics.invalidate()


__all__ = ["MAX_ATTEMPTS", "EventProcessor", "WorkerStats"]
