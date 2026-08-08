"""Caching with a pluggable backend.

Redis is the production backend; an in-process TTL cache with the same
interface keeps local development, tests and single-container deployments
working with no infrastructure at all.

Two rules the rest of the platform relies on:

* **A cache failure is never a request failure.** Every operation degrades to a
  miss and logs, rather than propagating.
* **Keys are namespaced and versioned.** Invalidating a whole family of derived
  values is a prefix delete, not a guessing game.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypeVar, cast, runtime_checkable

from app.core.config import CacheSettings
from app.core.hashing import short_hash
from app.core.logging import get_logger

log = get_logger(__name__)

T = TypeVar("T")

#: Bump when the shape of cached analytics payloads changes.
CACHE_SCHEMA_VERSION = "v1"


def build_key(namespace: str, family: str, *parts: str) -> str:
    """Compose a namespaced, versioned cache key.

    Long or user-supplied fragments are hashed so a key can never carry raw
    input into the cache server.
    """
    tail = ":".join(parts)
    if len(tail) > 120:
        tail = short_hash(tail, length=32)
    return (
        f"{namespace}:{CACHE_SCHEMA_VERSION}:{family}:{tail}"
        if tail
        else (f"{namespace}:{CACHE_SCHEMA_VERSION}:{family}")
    )


@runtime_checkable
class CacheBackend(Protocol):
    """The cache contract used across the platform."""

    async def get(self, key: str) -> Any | None: ...

    async def set(self, key: str, value: Any, *, ttl_seconds: int | None = None) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def delete_prefix(self, prefix: str) -> int: ...

    async def incr(self, key: str, *, amount: int = 1, ttl_seconds: int | None = None) -> int: ...

    async def ping(self) -> bool: ...

    async def aclose(self) -> None: ...


class InMemoryCache:
    """A TTL cache living in the process. Default outside production."""

    def __init__(self, *, default_ttl_seconds: int = 300, max_entries: int = 10_000) -> None:
        self._store: dict[str, tuple[float, Any]] = {}
        self._default_ttl = default_ttl_seconds
        self._max_entries = max_entries

    async def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at < time.monotonic():
            self._store.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: Any, *, ttl_seconds: int | None = None) -> None:
        if len(self._store) >= self._max_entries:
            self._evict()
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        self._store[key] = (time.monotonic() + ttl, value)

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def delete_prefix(self, prefix: str) -> int:
        keys = [key for key in self._store if key.startswith(prefix)]
        for key in keys:
            self._store.pop(key, None)
        return len(keys)

    async def incr(self, key: str, *, amount: int = 1, ttl_seconds: int | None = None) -> int:
        current = await self.get(key)
        value = int(current or 0) + amount
        await self.set(key, value, ttl_seconds=ttl_seconds)
        return value

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        self._store.clear()

    def _evict(self) -> None:
        """Drop expired entries first, then the oldest ones."""
        now = time.monotonic()
        expired = [key for key, (expires_at, _) in self._store.items() if expires_at < now]
        for key in expired:
            self._store.pop(key, None)
        if len(self._store) >= self._max_entries:
            overflow = len(self._store) - self._max_entries + 1
            for key in sorted(self._store, key=lambda k: self._store[k][0])[:overflow]:
                self._store.pop(key, None)

    def __len__(self) -> int:
        return len(self._store)


class RedisCache:
    """Redis-backed cache. Values are JSON encoded."""

    def __init__(self, client: Any, *, default_ttl_seconds: int = 300) -> None:
        self._client = client
        self._default_ttl = default_ttl_seconds

    @classmethod
    def from_url(cls, url: str, *, default_ttl_seconds: int = 300) -> RedisCache:
        """Build a cache from a Redis URL."""
        import redis.asyncio as redis  # imported lazily: an optional dependency

        client = redis.from_url(url, encoding="utf-8", decode_responses=True)
        return cls(client, default_ttl_seconds=default_ttl_seconds)

    async def get(self, key: str) -> Any | None:
        try:
            raw = await self._client.get(key)
        except Exception as exc:
            log.warning("cache.get_failed", key=key, error=str(exc))
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw

    async def set(self, key: str, value: Any, *, ttl_seconds: int | None = None) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        try:
            await self._client.set(key, json.dumps(value, default=str), ex=ttl)
        except Exception as exc:
            log.warning("cache.set_failed", key=key, error=str(exc))

    async def delete(self, key: str) -> None:
        try:
            await self._client.delete(key)
        except Exception as exc:
            log.warning("cache.delete_failed", key=key, error=str(exc))

    async def delete_prefix(self, prefix: str) -> int:
        """Delete every key under a prefix using a non-blocking scan."""
        removed = 0
        try:
            async for key in self._client.scan_iter(match=f"{prefix}*", count=500):
                await self._client.delete(key)
                removed += 1
        except Exception as exc:
            log.warning("cache.delete_prefix_failed", prefix=prefix, error=str(exc))
        return removed

    async def incr(self, key: str, *, amount: int = 1, ttl_seconds: int | None = None) -> int:
        try:
            value = int(await self._client.incrby(key, amount))
            if ttl_seconds is not None and value == amount:
                await self._client.expire(key, ttl_seconds)
            return value
        except Exception as exc:
            log.warning("cache.incr_failed", key=key, error=str(exc))
            return 0

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception:
            return False

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):
            await self._client.aclose()


def build_cache(settings: CacheSettings) -> CacheBackend:
    """Create the configured cache backend, falling back to memory."""
    if not settings.enabled:
        return NullCache()
    if settings.url:
        try:
            return RedisCache.from_url(
                settings.url, default_ttl_seconds=settings.default_ttl_seconds
            )
        except ImportError:
            log.warning("cache.redis_unavailable", detail="redis package is not installed")
        except Exception as exc:
            log.warning("cache.redis_init_failed", error=str(exc))
    return InMemoryCache(default_ttl_seconds=settings.default_ttl_seconds)


class NullCache:
    """A cache that stores nothing. Used when caching is switched off."""

    async def get(self, key: str) -> Any | None:
        return None

    async def set(self, key: str, value: Any, *, ttl_seconds: int | None = None) -> None:
        return None

    async def delete(self, key: str) -> None:
        return None

    async def delete_prefix(self, prefix: str) -> int:
        return 0

    async def incr(self, key: str, *, amount: int = 1, ttl_seconds: int | None = None) -> int:
        return amount

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


async def cached(
    backend: CacheBackend,
    key: str,
    factory: Callable[[], Awaitable[T]],
    *,
    ttl_seconds: int | None = None,
) -> T:
    """Read-through helper: return the cached value or compute and store it."""
    hit = await backend.get(key)
    if hit is not None:
        return cast("T", hit)
    value = await factory()
    await backend.set(key, value, ttl_seconds=ttl_seconds)
    return value


__all__ = [
    "CACHE_SCHEMA_VERSION",
    "CacheBackend",
    "InMemoryCache",
    "NullCache",
    "RedisCache",
    "build_cache",
    "build_key",
    "cached",
]
