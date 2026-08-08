"""HTTP middleware: correlation, security headers, body limits and rate limiting.

Everything here is defence in depth. None of it replaces validation in the
handlers, but each layer removes a class of problem before a request reaches
application code.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import ApiSettings, SecuritySettings
from app.core.logging import bind_context, get_logger, new_correlation_id
from app.storage.cache import CacheBackend, build_key

log = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"

#: Conservative defaults. The API serves JSON and a self-contained dashboard,
#: so it needs no third-party scripts, frames or plugins.
SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    ),
}

Handler = Callable[[Request], Awaitable[Response]]


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id, binds it to the logs and times the request."""

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or new_correlation_id()
        request.state.request_id = request_id
        bind_context(request_id=request_id, path=request.url.path, method=request.method)

        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - started) * 1000

        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers["X-Response-Time-ms"] = f"{duration_ms:.1f}"
        log.info(
            "http.request",
            status=response.status_code,
            duration_ms=round(duration_ms, 2),
        )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds the standard hardening headers to every response."""

    def __init__(self, app: object, *, settings: SecuritySettings) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._hsts = settings.hsts_enabled

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        if self._hsts and request.url.scheme == "https":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Rejects oversized request bodies before they are buffered."""

    def __init__(self, app: object, *, max_bytes: int) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self._max_bytes:
            return JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "code": "payload_too_large",
                        "message": f"request body exceeds {self._max_bytes} bytes",
                        "details": {"max_bytes": self._max_bytes},
                    }
                },
            )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """A fixed-window rate limiter backed by the cache.

    The window key includes the caller identity (API key id when present,
    client address otherwise) so one noisy client cannot exhaust everyone
    else's budget. With Redis configured the counter is shared across API
    replicas; with the in-memory cache it is per process, which is the correct
    behaviour for a single-container deployment.
    """

    def __init__(
        self,
        app: object,
        *,
        cache: CacheBackend,
        settings: ApiSettings,
        namespace: str = "jobintel",
        exempt_paths: tuple[str, ...] = ("/health", "/health/live", "/health/ready"),
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._cache = cache
        self._limit = settings.rate_limit_requests
        self._window = settings.rate_limit_window_seconds
        self._enabled = settings.rate_limit_enabled
        self._namespace = namespace
        self._exempt = exempt_paths

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        if not self._enabled or request.url.path in self._exempt:
            return await call_next(request)

        identity = self._identity(request)
        window = int(time.time() // self._window)
        key = build_key(self._namespace, "ratelimit", identity, str(window))
        count = await self._cache.incr(key, ttl_seconds=self._window * 2)

        if count > self._limit:
            retry_after = self._window - int(time.time() % self._window)
            log.warning("http.rate_limited", identity=identity, count=count)
            return JSONResponse(
                status_code=429,
                headers={
                    "Retry-After": str(max(retry_after, 1)),
                    "X-RateLimit-Limit": str(self._limit),
                    "X-RateLimit-Remaining": "0",
                },
                content={
                    "error": {
                        "code": "rate_limit_exceeded",
                        "message": "too many requests",
                        "details": {
                            "limit": self._limit,
                            "window_seconds": self._window,
                            "retry_after_seconds": max(retry_after, 1),
                        },
                    }
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self._limit)
        response.headers["X-RateLimit-Remaining"] = str(max(0, self._limit - count))
        return response

    @staticmethod
    def _identity(request: Request) -> str:
        """Identify the caller without logging the credential itself."""
        api_key = request.headers.get("x-api-key", "")
        if api_key:
            parts = api_key.split("_", 2)
            if len(parts) == 3:
                return f"key:{parts[1]}"
            return "key:malformed"
        client = request.client
        return f"ip:{client.host}" if client else "ip:unknown"


__all__ = [
    "REQUEST_ID_HEADER",
    "SECURITY_HEADERS",
    "BodySizeLimitMiddleware",
    "RateLimitMiddleware",
    "RequestContextMiddleware",
    "SecurityHeadersMiddleware",
]
