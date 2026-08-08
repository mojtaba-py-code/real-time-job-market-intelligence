"""The event bus.

The real-time pipeline is event driven: ingestion publishes, workers consume.
Which broker sits in the middle is an implementation detail behind
:class:`EventBus`.

* :class:`InMemoryEventBus` - an asyncio queue. Zero infrastructure, used for
  local runs and tests.
* :class:`RedisStreamsEventBus` - Redis Streams with consumer groups, at-least
  once delivery and explicit acknowledgement. The default for deployments.

The interface was chosen so that a Kafka implementation slots in unchanged:
publish, subscribe with a consumer group, acknowledge, and a claim path for
messages a dead worker left pending.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from app.core.config import EventSettings
from app.core.errors import EventBusError
from app.core.logging import get_logger
from app.models.enums import EventType
from app.models.events import PipelineEvent

log = get_logger(__name__)


@runtime_checkable
class EventBus(Protocol):
    """Broker-agnostic publish/subscribe contract."""

    async def publish(self, event: PipelineEvent) -> str: ...

    async def publish_many(self, events: list[PipelineEvent]) -> int: ...

    def consume(
        self, *, consumer: str, batch_size: int = 10, block_ms: int = 2000
    ) -> AsyncIterator[tuple[str, PipelineEvent]]: ...

    async def ack(self, message_id: str) -> None: ...

    async def pending_count(self) -> int: ...

    async def ping(self) -> bool: ...

    async def aclose(self) -> None: ...


class InMemoryEventBus:
    """An in-process bus backed by an asyncio queue."""

    def __init__(self, *, maxsize: int = 10_000) -> None:
        self._queue: asyncio.Queue[tuple[str, PipelineEvent]] = asyncio.Queue(maxsize=maxsize)
        self._counter = 0
        self._unacked: dict[str, PipelineEvent] = {}
        self._closed = False
        self.published: list[PipelineEvent] = []

    async def publish(self, event: PipelineEvent) -> str:
        if self._closed:
            raise EventBusError("event bus is closed")
        self._counter += 1
        message_id = f"mem-{self._counter}"
        await self._queue.put((message_id, event))
        self.published.append(event)
        return message_id

    async def publish_many(self, events: list[PipelineEvent]) -> int:
        for event in events:
            await self.publish(event)
        return len(events)

    async def consume(
        self, *, consumer: str = "worker", batch_size: int = 10, block_ms: int = 2000
    ) -> AsyncIterator[tuple[str, PipelineEvent]]:
        """Yield messages until the bus is closed."""
        timeout = block_ms / 1000
        while not self._closed:
            try:
                message_id, event = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            except TimeoutError:
                continue
            self._unacked[message_id] = event
            yield message_id, event

    async def ack(self, message_id: str) -> None:
        self._unacked.pop(message_id, None)

    async def pending_count(self) -> int:
        return self._queue.qsize() + len(self._unacked)

    async def ping(self) -> bool:
        return not self._closed

    async def aclose(self) -> None:
        self._closed = True

    def drain(self) -> list[PipelineEvent]:
        """Return and clear everything published (tests and diagnostics)."""
        events = list(self.published)
        self.published.clear()
        return events


class RedisStreamsEventBus:
    """Redis Streams with a consumer group per worker pool."""

    def __init__(
        self,
        client: Any,
        *,
        stream: str,
        group: str,
        max_length: int = 100_000,
    ) -> None:
        self._client = client
        self._stream = stream
        self._group = group
        self._max_length = max_length
        self._group_ready = False

    @classmethod
    def from_url(cls, url: str, settings: EventSettings) -> RedisStreamsEventBus:
        import redis.asyncio as redis  # imported lazily: an optional dependency

        client = redis.from_url(url, encoding="utf-8", decode_responses=True)
        return cls(
            client,
            stream=f"{settings.stream_prefix}:jobs",
            group=settings.consumer_group,
            max_length=settings.max_stream_length,
        )

    async def _ensure_group(self) -> None:
        """Create the consumer group once, tolerating a concurrent creator."""
        if self._group_ready:
            return
        try:
            await self._client.xgroup_create(self._stream, self._group, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise EventBusError(f"cannot create consumer group: {exc}") from exc
        self._group_ready = True

    async def publish(self, event: PipelineEvent) -> str:
        await self._ensure_group()
        try:
            # approximate trimming keeps the stream bounded without blocking
            message_id = await self._client.xadd(
                self._stream, event.to_wire(), maxlen=self._max_length, approximate=True
            )
        except Exception as exc:
            raise EventBusError(f"publish failed: {exc}") from exc
        return str(message_id)

    async def publish_many(self, events: list[PipelineEvent]) -> int:
        if not events:
            return 0
        await self._ensure_group()
        try:
            pipe = self._client.pipeline()
            for event in events:
                pipe.xadd(self._stream, event.to_wire(), maxlen=self._max_length, approximate=True)
            await pipe.execute()
        except Exception as exc:
            raise EventBusError(f"batch publish failed: {exc}") from exc
        return len(events)

    async def consume(
        self, *, consumer: str = "worker", batch_size: int = 10, block_ms: int = 2000
    ) -> AsyncIterator[tuple[str, PipelineEvent]]:
        """Read new messages for this consumer group."""
        await self._ensure_group()
        while True:
            try:
                response = await self._client.xreadgroup(
                    self._group,
                    consumer,
                    {self._stream: ">"},
                    count=batch_size,
                    block=block_ms,
                )
            except Exception as exc:
                log.warning("events.read_failed", error=str(exc))
                await asyncio.sleep(1.0)
                continue
            if not response:
                continue
            for _stream, messages in response:
                for message_id, fields in messages:
                    try:
                        event = PipelineEvent.from_wire(fields)
                    except Exception as exc:
                        log.warning(
                            "events.decode_failed", message_id=str(message_id), error=str(exc)
                        )
                        await self.ack(str(message_id))
                        continue
                    yield str(message_id), event

    async def ack(self, message_id: str) -> None:
        with contextlib.suppress(Exception):
            await self._client.xack(self._stream, self._group, message_id)

    async def claim_stale(self, *, consumer: str, min_idle_ms: int = 60_000) -> int:
        """Take over messages a crashed worker never acknowledged."""
        try:
            claimed, _, _ = await self._client.xautoclaim(
                self._stream, self._group, consumer, min_idle_time=min_idle_ms, count=100
            )
        except Exception as exc:
            log.warning("events.claim_failed", error=str(exc))
            return 0
        return len(claimed or [])

    async def pending_count(self) -> int:
        try:
            info = await self._client.xpending(self._stream, self._group)
        except Exception:
            return 0
        if isinstance(info, dict):
            return int(info.get("pending", 0))
        return int(info[0]) if info else 0

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception:
            return False

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):
            await self._client.aclose()


def build_event_bus(settings: EventSettings, *, redis_url: str | None = None) -> EventBus:
    """Create the configured bus, degrading to the in-memory one."""
    if settings.backend == "redis" and redis_url:
        try:
            return RedisStreamsEventBus.from_url(redis_url, settings)
        except ImportError:
            log.warning("events.redis_unavailable", detail="redis package is not installed")
        except Exception as exc:
            log.warning("events.redis_init_failed", error=str(exc))
    return InMemoryEventBus()


def job_event(
    event_type: EventType,
    *,
    job_id: str | None = None,
    source: str | None = None,
    run_id: str | None = None,
    **payload: Any,
) -> PipelineEvent:
    """Build a pipeline event with the usual identifiers attached."""
    return PipelineEvent(
        type=event_type,
        job_id=job_id,
        source=source,
        ingestion_run_id=run_id,
        payload=payload,
    )


__all__ = [
    "EventBus",
    "InMemoryEventBus",
    "RedisStreamsEventBus",
    "build_event_bus",
    "job_event",
]
