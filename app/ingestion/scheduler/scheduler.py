"""A small async scheduler for periodic pipeline work.

The platform needs "run this every N seconds, forever, without overlapping
executions, and keep going when a task raises". That is a handful of lines of
asyncio - far less operational surface than pulling in a full task framework,
and it keeps the worker container dependency-free.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.core.timeutils import humanize_duration, utcnow

log = get_logger(__name__)

TaskCallable = Callable[[], Awaitable[Any]]


@dataclass(slots=True)
class ScheduledTask:
    """A periodic job plus its execution statistics."""

    name: str
    interval_seconds: float
    handler: TaskCallable
    run_immediately: bool = True
    jitter_seconds: float = 0.0
    enabled: bool = True

    runs: int = field(default=0, init=False)
    failures: int = field(default=0, init=False)
    last_started_at: float | None = field(default=None, init=False)
    last_duration_seconds: float = field(default=0.0, init=False)
    last_error: str | None = field(default=None, init=False)

    def next_delay(self) -> float:
        """Interval plus a small random offset so tasks do not align."""
        if self.jitter_seconds <= 0:
            return self.interval_seconds
        return self.interval_seconds + random.uniform(0, self.jitter_seconds)  # noqa: S311

    def snapshot(self) -> dict[str, Any]:
        """Serialisable status, exposed by the health endpoint."""
        return {
            "name": self.name,
            "interval_seconds": self.interval_seconds,
            "enabled": self.enabled,
            "runs": self.runs,
            "failures": self.failures,
            "last_duration": humanize_duration(self.last_duration_seconds),
            "last_error": self.last_error,
        }


class Scheduler:
    """Runs registered tasks on their own cadence until stopped."""

    def __init__(self) -> None:
        self._tasks: dict[str, ScheduledTask] = {}
        self._runners: dict[str, asyncio.Task[None]] = {}
        self._stopping = asyncio.Event()
        self._started_at: float | None = None

    def register(
        self,
        name: str,
        handler: TaskCallable,
        *,
        interval_seconds: float,
        run_immediately: bool = True,
        jitter_seconds: float = 0.0,
        enabled: bool = True,
    ) -> ScheduledTask:
        """Add a periodic task. Registering twice replaces the previous entry."""
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        task = ScheduledTask(
            name=name,
            interval_seconds=interval_seconds,
            handler=handler,
            run_immediately=run_immediately,
            jitter_seconds=jitter_seconds,
            enabled=enabled,
        )
        self._tasks[name] = task
        return task

    @property
    def tasks(self) -> list[ScheduledTask]:
        return list(self._tasks.values())

    @property
    def is_running(self) -> bool:
        return bool(self._runners) and not self._stopping.is_set()

    async def start(self) -> None:
        """Spawn one runner coroutine per enabled task."""
        if self._runners:
            return
        self._stopping.clear()
        self._started_at = time.monotonic()
        for task in self._tasks.values():
            if task.enabled:
                self._runners[task.name] = asyncio.create_task(
                    self._run_forever(task), name=f"scheduler:{task.name}"
                )
        log.info("scheduler.started", tasks=sorted(self._runners))

    async def stop(self, *, grace_seconds: float = 10.0) -> None:
        """Signal every runner to finish and wait for them."""
        self._stopping.set()
        runners = list(self._runners.values())
        for runner in runners:
            runner.cancel()
        if runners:
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*runners, return_exceptions=True), timeout=grace_seconds
                )
        self._runners.clear()
        log.info("scheduler.stopped")

    async def run_once(self, name: str) -> Any:
        """Execute a task immediately, outside its schedule (CLI/API trigger)."""
        task = self._tasks.get(name)
        if task is None:
            raise KeyError(f"unknown scheduled task: {name}")
        return await self._execute(task)

    async def _run_forever(self, task: ScheduledTask) -> None:
        if not task.run_immediately:
            await self._sleep(task.next_delay())
        while not self._stopping.is_set():
            await self._execute(task)
            await self._sleep(task.next_delay())

    async def _sleep(self, seconds: float) -> None:
        """Sleep, but wake up immediately when the scheduler is stopping."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    async def _execute(self, task: ScheduledTask) -> Any:
        started = time.monotonic()
        task.last_started_at = started
        task.runs += 1
        bound = log.bind(task=task.name, run=task.runs)
        try:
            result = await task.handler()
            task.last_error = None
            bound.info(
                "scheduler.task_completed",
                duration=humanize_duration(time.monotonic() - started),
                at=utcnow().isoformat(),
            )
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            task.failures += 1
            task.last_error = str(exc)[:500]
            bound.exception("scheduler.task_failed", error=str(exc))
            return None
        finally:
            task.last_duration_seconds = time.monotonic() - started

    def snapshot(self) -> dict[str, Any]:
        """Overall scheduler status."""
        uptime = time.monotonic() - self._started_at if self._started_at else 0.0
        return {
            "running": self.is_running,
            "uptime": humanize_duration(uptime),
            "tasks": [task.snapshot() for task in self._tasks.values()],
        }


__all__ = ["ScheduledTask", "Scheduler", "TaskCallable"]
