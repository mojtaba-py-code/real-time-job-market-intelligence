"""Exception hierarchy shared by every layer of the platform.

Errors carry a stable machine-readable ``code`` plus an optional ``details``
mapping. The API layer turns them into RFC-7807-style JSON problem documents,
the CLI turns them into readable messages, and workers use them to decide
whether a failure is retryable.
"""

from __future__ import annotations

from typing import Any


class JobIntelError(Exception):
    """Base class for all platform errors."""

    code: str = "internal_error"
    http_status: int = 500
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}
        if code is not None:
            self.code = code

    def to_dict(self) -> dict[str, Any]:
        """Serialise the error for transport."""
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


# --------------------------------------------------------------------------- #
# Configuration / programming errors
# --------------------------------------------------------------------------- #
class ConfigurationError(JobIntelError):
    """Invalid or missing configuration."""

    code = "configuration_error"
    http_status = 500


class UnsupportedBackendError(ConfigurationError):
    """A backend was requested that is not available in this deployment."""

    code = "unsupported_backend"


# --------------------------------------------------------------------------- #
# Data errors
# --------------------------------------------------------------------------- #
class DataError(JobIntelError):
    """Base class for data-plane failures."""

    code = "data_error"
    http_status = 422


class RecordValidationError(DataError):
    """A raw record failed validation and was quarantined."""

    code = "record_validation_error"

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        merged = dict(details or {})
        if field:
            merged["field"] = field
        super().__init__(message, details=merged)
        self.field = field


class NormalizationError(DataError):
    """A record could not be normalized into the canonical schema."""

    code = "normalization_error"


class DeduplicationError(DataError):
    """The deduplication engine could not evaluate a record."""

    code = "deduplication_error"


class NlpError(DataError):
    """The NLP pipeline failed on a document."""

    code = "nlp_error"


class TaxonomyError(ConfigurationError):
    """The skill taxonomy file is missing or malformed."""

    code = "taxonomy_error"


# --------------------------------------------------------------------------- #
# Ingestion errors
# --------------------------------------------------------------------------- #
class IngestionError(JobIntelError):
    """Base class for ingestion failures."""

    code = "ingestion_error"
    http_status = 502
    retryable = True


class SourceUnavailableError(IngestionError):
    """A remote source could not be reached or returned an error status."""

    code = "source_unavailable"


class SourceConfigurationError(IngestionError):
    """A source adapter is misconfigured."""

    code = "source_configuration_error"
    http_status = 400
    retryable = False


class UnsafeUrlError(IngestionError):
    """A URL was rejected by the outbound request guard (SSRF protection)."""

    code = "unsafe_url"
    http_status = 400
    retryable = False


class RobotsDisallowedError(IngestionError):
    """The robots.txt policy of the target host forbids this request."""

    code = "robots_disallowed"
    http_status = 403
    retryable = False


class ResponseTooLargeError(IngestionError):
    """The remote response exceeded the configured size budget."""

    code = "response_too_large"
    retryable = False


# --------------------------------------------------------------------------- #
# Storage / infrastructure errors
# --------------------------------------------------------------------------- #
class StorageError(JobIntelError):
    """Persistence layer failure."""

    code = "storage_error"
    retryable = True


class CacheError(JobIntelError):
    """Cache backend failure. Callers should degrade gracefully."""

    code = "cache_error"
    retryable = True


class EventBusError(JobIntelError):
    """Message broker failure."""

    code = "event_bus_error"
    retryable = True


# --------------------------------------------------------------------------- #
# API errors
# --------------------------------------------------------------------------- #
class NotFoundError(JobIntelError):
    """The requested resource does not exist."""

    code = "not_found"
    http_status = 404


class ConflictError(JobIntelError):
    """The request conflicts with the current state of the resource."""

    code = "conflict"
    http_status = 409


class AuthenticationError(JobIntelError):
    """Missing or invalid credentials."""

    code = "authentication_failed"
    http_status = 401


class AuthorizationError(JobIntelError):
    """The caller is authenticated but lacks the required scope."""

    code = "permission_denied"
    http_status = 403


class RateLimitExceededError(JobIntelError):
    """The caller exceeded the configured request budget."""

    code = "rate_limit_exceeded"
    http_status = 429
    retryable = True

    def __init__(self, message: str, *, retry_after_seconds: int = 60) -> None:
        super().__init__(message, details={"retry_after_seconds": retry_after_seconds})
        self.retry_after_seconds = retry_after_seconds


class InvalidRequestError(JobIntelError):
    """The request was structurally valid but semantically wrong."""

    code = "invalid_request"
    http_status = 400


# --------------------------------------------------------------------------- #
# Alerting
# --------------------------------------------------------------------------- #
class AlertDeliveryError(JobIntelError):
    """A notification channel failed to deliver an alert."""

    code = "alert_delivery_failed"
    retryable = True


__all__ = [
    "AlertDeliveryError",
    "AuthenticationError",
    "AuthorizationError",
    "CacheError",
    "ConfigurationError",
    "ConflictError",
    "DataError",
    "DeduplicationError",
    "EventBusError",
    "IngestionError",
    "InvalidRequestError",
    "JobIntelError",
    "NlpError",
    "NormalizationError",
    "NotFoundError",
    "RateLimitExceededError",
    "RecordValidationError",
    "ResponseTooLargeError",
    "RobotsDisallowedError",
    "SourceConfigurationError",
    "SourceUnavailableError",
    "StorageError",
    "TaxonomyError",
    "UnsafeUrlError",
    "UnsupportedBackendError",
]
