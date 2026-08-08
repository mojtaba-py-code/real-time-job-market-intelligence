"""Exception handling for the HTTP layer.

Every error leaves the API in the same shape, carries a stable machine-readable
code and never leaks an internal detail. Unexpected exceptions are logged with
their traceback and answered with a generic message plus the request id, which
is what an operator needs to find the log line.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from app.api.schemas import ErrorBody, ErrorResponse
from app.core.errors import JobIntelError, RateLimitExceededError
from app.core.logging import get_logger

log = get_logger(__name__)


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _response(
    status_code: int,
    *,
    code: str,
    message: str,
    request: Request,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    payload = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            details=details or {},
            request_id=_request_id(request),
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json"),
        headers=headers,
    )


async def jobintel_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Translate a domain error into its HTTP representation."""
    error = exc if isinstance(exc, JobIntelError) else JobIntelError(str(exc))
    headers: dict[str, str] | None = None
    if isinstance(error, RateLimitExceededError):
        headers = {"Retry-After": str(error.retry_after_seconds)}
    if error.http_status >= 500:
        log.error("api.domain_error", code=error.code, message=error.message)
    return _response(
        error.http_status,
        code=error.code,
        message=error.message,
        request=request,
        details=error.details,
        headers=headers,
    )


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Report request-validation failures without echoing raw input back."""
    errors: list[dict[str, Any]] = []
    if isinstance(exc, RequestValidationError):
        for item in exc.errors()[:20]:
            errors.append(
                {
                    "field": ".".join(str(part) for part in item.get("loc", ())),
                    "message": str(item.get("msg", "invalid value")),
                    "type": str(item.get("type", "value_error")),
                }
            )
    return _response(
        422,
        code="validation_error",
        message="the request did not pass validation",
        request=request,
        details={"errors": errors},
    )


async def model_validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """A domain model rejected the payload.

    This happens when a request is structurally valid but violates an
    invariant the domain model owns (for example an e-mail alert without a
    recipient). That is the caller's mistake, so it is a 422, not a 500.
    """
    errors: list[dict[str, Any]] = []
    if isinstance(exc, ValidationError):
        for item in exc.errors()[:20]:
            errors.append(
                {
                    "field": ".".join(str(part) for part in item.get("loc", ())),
                    "message": str(item.get("msg", "invalid value")),
                }
            )
    return _response(
        422,
        code="validation_error",
        message="the request violates a domain constraint",
        request=request,
        details={"errors": errors},
    )


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Give Starlette's HTTP errors the platform's error shape."""
    status_code = getattr(exc, "status_code", 500)
    detail = getattr(exc, "detail", "request failed")
    codes = {
        400: "bad_request",
        401: "authentication_failed",
        403: "permission_denied",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        413: "payload_too_large",
        415: "unsupported_media_type",
        429: "rate_limit_exceeded",
    }
    return _response(
        status_code,
        code=codes.get(status_code, "http_error"),
        message=str(detail),
        request=request,
        headers=getattr(exc, "headers", None),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last resort: log everything, disclose nothing."""
    log.exception("api.unhandled_error", error=str(exc), path=request.url.path)
    return _response(
        500,
        code="internal_error",
        message="an unexpected error occurred",
        request=request,
    )


def install_error_handlers(app: FastAPI) -> None:
    """Register every handler on the application."""
    app.add_exception_handler(JobIntelError, jobintel_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(ValidationError, model_validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)


__all__ = [
    "install_error_handlers",
    "jobintel_error_handler",
    "model_validation_error_handler",
    "unhandled_exception_handler",
    "validation_error_handler",
]
