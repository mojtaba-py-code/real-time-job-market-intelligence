"""The worker process.

One container, two concurrent responsibilities:

* the **scheduler**, which runs ingestion, expiry, analytics refresh and alert
  evaluation on their own cadences;
* the **event processor**, which reacts to what ingestion publishes.

Both are cancelled cleanly on SIGTERM so a rolling deployment never leaves a
half-written batch behind.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.events.bus import EventBus, build_event_bus
from app.ingestion.registry import SourceRegistry
from app.ingestion.scheduler.scheduler import Scheduler
from app.services.admin_service import AdminService
from app.services.alert_service import AlertService
from app.services.analytics_service import AnalyticsService
from app.services.ingestion_service import IngestionService
from app.storage.cache import CacheBackend, build_cache
from app.storage.session import Database, get_database
from app.workers.processor import EventProcessor

log = get_logger(__name__)


@dataclass(slots=True)
class WorkerContext:
    """Everything a worker needs, wired once."""

    settings: Settings
    database: Database
    cache: CacheBackend
    bus: EventBus
    registry: SourceRegistry
    ingestion: IngestionService
    analytics: AnalyticsService
    alerts: AlertService
    admin: AdminService


def build_context(settings: Settings | None = None) -> WorkerContext:
    """Construct the dependency graph for a worker."""
    resolved = settings or get_settings()
    resolved.ensure_directories()
    database = get_database(resolved)
    cache = build_cache(resolved.cache)
    bus = build_event_bus(resolved.events, redis_url=resolved.cache.url)
    registry = SourceRegistry.from_file(settings=resolved)

    analytics = AnalyticsService(database, cache=cache, settings=resolved)
    alerts = AlertService(database, settings=resolved, event_bus=bus)
    admin = AdminService(database, settings=resolved, cache=cache)
    ingestion = IngestionService(
        database=database, registry=registry, event_bus=bus, settings=resolved
    )
    return WorkerContext(
        settings=resolved,
        database=database,
        cache=cache,
        bus=bus,
        registry=registry,
        ingestion=ingestion,
        analytics=analytics,
        alerts=alerts,
        admin=admin,
    )


def build_scheduler(context: WorkerContext) -> Scheduler:
    """Register the periodic pipeline tasks."""
    settings = context.settings.scheduler
    scheduler = Scheduler()

    async def ingest() -> dict[str, Any]:
        outcomes = await context.ingestion.run_all()
        return {"sources": len(outcomes), "ok": sum(1 for o in outcomes if o.succeeded)}

    async def expire() -> int:
        return await context.ingestion.expire_stale()

    async def refresh_analytics() -> dict[str, Any]:
        await context.analytics.invalidate()
        exported = await context.admin.export_analytics()
        snapshot = await context.admin.snapshot_market()
        return {"exported_rows": exported, "active_jobs": snapshot.get("active_jobs", 0)}

    async def evaluate_alerts() -> dict[str, Any]:
        result = await context.alerts.evaluate_all()
        return {"evaluated": result.evaluated, "triggered": result.triggered}

    scheduler.register(
        "ingest",
        ingest,
        interval_seconds=settings.ingestion_interval_seconds,
        jitter_seconds=settings.jitter_seconds,
    )
    scheduler.register(
        "expire",
        expire,
        interval_seconds=settings.expiry_interval_seconds,
        run_immediately=False,
        jitter_seconds=settings.jitter_seconds,
    )
    scheduler.register(
        "analytics",
        refresh_analytics,
        interval_seconds=settings.analytics_interval_seconds,
        run_immediately=False,
        jitter_seconds=settings.jitter_seconds,
    )
    scheduler.register(
        "alerts",
        evaluate_alerts,
        interval_seconds=context.settings.alerts.evaluation_interval_seconds,
        run_immediately=False,
        enabled=context.settings.alerts.enabled,
        jitter_seconds=settings.jitter_seconds,
    )
    return scheduler


async def run_worker(settings: Settings | None = None) -> None:
    """Entry point for ``jobintel worker`` and the worker container."""
    context = build_context(settings)
    configure_logging(
        level=context.settings.observability.log_level,
        fmt=context.settings.observability.log_format,
        log_file=context.settings.observability.log_file,
        service="jobintel-worker",
    )

    scheduler = build_scheduler(context)
    processor = EventProcessor(
        bus=context.bus,
        analytics=context.analytics,
        alerts=context.alerts,
        admin=context.admin,
        settings=context.settings,
    )

    stop = asyncio.Event()
    _install_signal_handlers(stop)

    await scheduler.start()
    consumer = asyncio.create_task(processor.run(), name="event-processor")
    log.info("worker.ready", tasks=[task.name for task in scheduler.tasks])

    try:
        await stop.wait()
    finally:
        log.info("worker.shutting_down")
        await processor.stop()
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer
        await scheduler.stop()
        await context.bus.aclose()
        await context.cache.aclose()
        await context.registry.aclose()
        await context.database.dispose()


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """Ask the loop to stop on SIGINT/SIGTERM where the platform supports it."""
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)


__all__ = ["WorkerContext", "build_context", "build_scheduler", "run_worker"]
