"""A deliberately paranoid HTTP client for outbound data collection.

Fetching URLs that originate from configuration or from remote payloads is the
classic server-side request forgery (SSRF) vector. Everything in this module
exists to make that class of bug hard to write:

* every URL is validated *and* its hostname resolved before a socket is opened,
  so ``http://internal.example.com`` resolving to ``10.0.0.5`` is rejected;
* redirects are followed manually, re-validating each hop, because a remote
  server can otherwise redirect us into the private network;
* responses are streamed with a hard byte budget so a hostile endpoint cannot
  exhaust memory;
* ``robots.txt`` is honoured, requests are rate limited per host, and a
  descriptive User-Agent identifies the crawler.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from app.core.config import IngestionSettings
from app.core.errors import (
    ResponseTooLargeError,
    RobotsDisallowedError,
    SourceUnavailableError,
    UnsafeUrlError,
)
from app.core.logging import get_logger

log = get_logger(__name__)

MAX_REDIRECTS = 3
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Hostnames that must never be resolved, whatever DNS says.
BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "metadata",
        "metadata.google.internal",
        "instance-data",
    }
)

#: Cloud metadata endpoints - the single most valuable SSRF target.
BLOCKED_ADDRESSES = frozenset({"169.254.169.254", "fd00:ec2::254"})


def is_public_address(address: str) -> bool:
    """Whether an IP address is routable on the public internet."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if str(ip) in BLOCKED_ADDRESSES:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


@dataclass(frozen=True, slots=True)
class ValidatedUrl:
    """A URL that passed the outbound guard."""

    url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...]


class UrlGuard:
    """Validates outbound URLs before any connection is attempted."""

    def __init__(self, settings: IngestionSettings) -> None:
        self._settings = settings

    async def validate(self, url: str) -> ValidatedUrl:
        """Parse, check and DNS-resolve a URL.

        Raises:
            UnsafeUrlError: if the URL is malformed, uses a forbidden scheme or
                resolves to a non-public address.
        """
        if not url or len(url) > 2048:
            raise UnsafeUrlError("URL is empty or unreasonably long")

        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        if scheme not in self._settings.allowed_schemes:
            raise UnsafeUrlError(
                f"scheme {scheme!r} is not allowed",
                details={"allowed": list(self._settings.allowed_schemes)},
            )

        host = (parts.hostname or "").lower()
        if not host:
            raise UnsafeUrlError("URL has no hostname")
        if host in BLOCKED_HOSTNAMES:
            raise UnsafeUrlError(f"hostname {host!r} is blocked")
        if parts.username or parts.password:
            raise UnsafeUrlError("credentials embedded in URLs are not accepted")

        port = parts.port or (443 if scheme == "https" else 80)
        addresses = await self._resolve(host, port)

        if not self._settings.allow_private_networks:
            unsafe = [addr for addr in addresses if not is_public_address(addr)]
            if unsafe:
                raise UnsafeUrlError(
                    f"host {host!r} resolves to a non-public address",
                    details={"addresses": unsafe},
                )
        return ValidatedUrl(
            url=url, scheme=scheme, host=host, port=port, addresses=tuple(addresses)
        )

    async def _resolve(self, host: str, port: int) -> list[str]:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, port, proto=socket.IPPROTO_TCP
            )
        except (socket.gaierror, OSError) as exc:
            raise UnsafeUrlError(f"cannot resolve host {host!r}: {exc}") from exc
        addresses = [str(info[4][0]) for info in infos]
        if not addresses:
            raise UnsafeUrlError(f"host {host!r} did not resolve to any address")
        return addresses


class HostRateLimiter:
    """Enforces a minimum delay between requests to the same host."""

    def __init__(self, min_interval_seconds: float) -> None:
        self._min_interval = max(0.0, min_interval_seconds)
        self._last_request: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def acquire(self, host: str) -> None:
        """Sleep as long as necessary before the next request to ``host``."""
        if self._min_interval <= 0:
            return
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            previous = self._last_request.get(host)
            if previous is not None:
                wait = self._min_interval - (now - previous)
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last_request[host] = time.monotonic()


class RobotsPolicy:
    """Caches and applies ``robots.txt`` rules per host."""

    def __init__(self, user_agent: str, *, enabled: bool = True) -> None:
        self._user_agent = user_agent
        self._enabled = enabled
        self._cache: dict[str, RobotFileParser | None] = {}
        self._lock = asyncio.Lock()

    async def is_allowed(self, client: httpx.AsyncClient, validated: ValidatedUrl) -> bool:
        """Whether the crawler may fetch ``validated.url``."""
        if not self._enabled:
            return True
        key = f"{validated.scheme}://{validated.host}:{validated.port}"
        async with self._lock:
            if key not in self._cache:
                self._cache[key] = await self._load(client, key)
        parser = self._cache[key]
        if parser is None:
            # No robots.txt (or it was unreachable): the conservative reading of
            # RFC 9309 is that everything is allowed.
            return True
        return parser.can_fetch(self._user_agent, validated.url)

    async def _load(self, client: httpx.AsyncClient, origin: str) -> RobotFileParser | None:
        url = f"{origin}/robots.txt"
        try:
            response = await client.get(url, timeout=10.0)
        except httpx.HTTPError as exc:
            log.debug("robots.fetch_failed", origin=origin, error=str(exc))
            return None
        if response.status_code >= 400:
            return None
        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        return parser


@dataclass(slots=True)
class HttpResponse:
    """A fetched response, already size-checked and decoded."""

    url: str
    status_code: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        """Parse the body as JSON."""
        import json

        return json.loads(self.text)

    @property
    def etag(self) -> str | None:
        return self.headers.get("etag")


class SafeHttpClient:
    """The only way the platform talks to the outside world."""

    def __init__(
        self,
        settings: IngestionSettings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._guard = UrlGuard(settings)
        self._limiter = HostRateLimiter(settings.per_host_delay_seconds)
        self._robots = RobotsPolicy(settings.user_agent, enabled=settings.respect_robots_txt)
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_requests)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            follow_redirects=False,
            headers={
                "User-Agent": settings.user_agent,
                "Accept-Encoding": "gzip, deflate",
            },
            limits=httpx.Limits(max_connections=settings.max_concurrent_requests),
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying connection pool if we own it."""
        if self._owns_client:
            await self._client.aclose()

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> HttpResponse:
        """Fetch a URL with validation, rate limiting, retries and size caps."""
        attempt = 0
        last_error: Exception | None = None
        while attempt <= self._settings.max_retries:
            try:
                return await self._fetch_once(url, headers=headers, params=params)
            except (SourceUnavailableError, httpx.HTTPError) as exc:
                last_error = exc
                status = (
                    exc.details.get("status") if isinstance(exc, SourceUnavailableError) else None
                )
                if status is not None and status not in RETRYABLE_STATUS:
                    # 4xx responses (other than throttling) will not change on a
                    # retry; failing fast keeps us polite to the remote host.
                    raise
                attempt += 1
                if attempt > self._settings.max_retries:
                    break
                delay = self._settings.retry_backoff_seconds * (2 ** (attempt - 1))
                log.warning(
                    "http.retry", url=url, attempt=attempt, delay_seconds=delay, error=str(exc)
                )
                await asyncio.sleep(delay)
        raise SourceUnavailableError(
            f"request to {url} failed after {self._settings.max_retries + 1} attempts",
            details={"error": str(last_error) if last_error else "unknown"},
        )

    async def _fetch_once(
        self,
        url: str,
        *,
        headers: dict[str, str] | None,
        params: dict[str, Any] | None,
    ) -> HttpResponse:
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            validated = await self._guard.validate(current)
            if not await self._robots.is_allowed(self._client, validated):
                raise RobotsDisallowedError(
                    f"robots.txt of {validated.host} disallows {current}",
                    details={"host": validated.host},
                )
            await self._limiter.acquire(validated.host)

            async with self._semaphore:
                response = await self._send(
                    current, headers=headers, params=params if hop == 0 else None
                )

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if not location:
                    raise SourceUnavailableError(
                        f"redirect from {current} without a Location header",
                        details={"status": response.status_code},
                    )
                current = str(httpx.URL(current).join(location))
                continue

            if response.status_code >= 400:
                raise SourceUnavailableError(
                    f"{current} returned HTTP {response.status_code}",
                    details={"status": response.status_code, "url": current},
                )
            return response

        raise SourceUnavailableError(
            f"too many redirects while fetching {url}", details={"max": MAX_REDIRECTS}
        )

    async def _send(
        self,
        url: str,
        *,
        headers: dict[str, str] | None,
        params: dict[str, Any] | None,
    ) -> HttpResponse:
        budget = self._settings.max_response_bytes
        chunks: list[bytes] = []
        received = 0
        request = self._client.build_request("GET", url, headers=headers, params=params)
        response = await self._client.send(request, stream=True)
        try:
            declared = response.headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > budget:
                raise ResponseTooLargeError(
                    f"{url} declares {declared} bytes, budget is {budget}",
                    details={"declared": int(declared), "budget": budget},
                )
            if response.status_code < 400 and response.status_code not in (
                301,
                302,
                303,
                307,
                308,
            ):
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > budget:
                        raise ResponseTooLargeError(
                            f"{url} exceeded the {budget} byte budget",
                            details={"budget": budget},
                        )
                    chunks.append(chunk)
            encoding = response.charset_encoding or "utf-8"
            body = b"".join(chunks).decode(encoding, errors="replace")
            return HttpResponse(
                url=str(response.url),
                status_code=response.status_code,
                text=body,
                headers={k.lower(): v for k, v in response.headers.items()},
            )
        finally:
            await response.aclose()

    async def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Fetch and parse a JSON document."""
        response = await self.get(url, headers=headers, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise SourceUnavailableError(
                f"{url} did not return valid JSON", details={"error": str(exc)}
            ) from exc


__all__ = [
    "BLOCKED_ADDRESSES",
    "BLOCKED_HOSTNAMES",
    "MAX_REDIRECTS",
    "HostRateLimiter",
    "HttpResponse",
    "RobotsPolicy",
    "SafeHttpClient",
    "UrlGuard",
    "ValidatedUrl",
    "is_public_address",
]
