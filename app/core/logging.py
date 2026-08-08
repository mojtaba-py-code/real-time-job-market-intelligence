"""Structured, privacy-aware logging.

Two things matter here:

* **Structure** - every log line is a JSON object with a stable set of keys so
  that it can be shipped to any log platform without regex parsing.
* **Safety** - secrets and personal data must never reach the log stream. All
  key/value pairs pass through a redactor before serialisation.

Usage::

    log = get_logger(__name__)
    log.info("ingestion.completed", source="synthetic", records=1200)
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

# The default is an immutable mapping: a shared mutable default would leak
# context between unrelated requests.
_EMPTY_CONTEXT: Mapping[str, Any] = MappingProxyType({})
_LOG_CONTEXT: ContextVar[Mapping[str, Any]] = ContextVar(
    "jobintel_log_context", default=_EMPTY_CONTEXT
)

_FIELDS_ATTR = "jobintel_fields"

#: Keys whose values are always replaced with a placeholder.
SENSITIVE_KEY_PATTERN = re.compile(
    r"(pass(word|phrase)?|secret|token|api[_-]?key|authorization|cookie|session|"
    r"credential|private[_-]?key|access[_-]?key|salt|signature)",
    re.IGNORECASE,
)

#: Patterns masked inside free-form strings.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+\d{1,3}[\s-]?)?(?:\d[\s-]?){9,14}\d(?!\d)")
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")

REDACTED = "[redacted]"

_RESERVED_RECORD_KEYS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__)


def mask_email(value: str) -> str:
    """Replace the local part of any e-mail address with asterisks."""

    def _mask(match: re.Match[str]) -> str:
        local, _, domain = match.group(0).partition("@")
        keep = local[:1]
        return f"{keep}{'*' * max(len(local) - 1, 1)}@{domain}"

    return _EMAIL_RE.sub(_mask, value)


def scrub_text(value: str) -> str:
    """Mask personal data and credentials embedded in a free-form string."""
    scrubbed = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", value)
    scrubbed = mask_email(scrubbed)
    return _PHONE_RE.sub(REDACTED, scrubbed)


def redact(value: Any, *, key: str | None = None, _depth: int = 0) -> Any:
    """Recursively redact sensitive keys and mask PII inside a value."""
    if key is not None and SENSITIVE_KEY_PATTERN.search(key):
        return REDACTED
    if _depth > 6:
        return "[truncated]"
    if isinstance(value, Mapping):
        return {str(k): redact(v, key=str(k), _depth=_depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact(v, _depth=_depth + 1) for v in value]
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, (int, float, bool, type(None))):
        return value
    if isinstance(value, (datetime, Path)):
        return str(value)
    return scrub_text(repr(value))


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON documents."""

    def __init__(self, *, service: str = "jobintel") -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            "service": self.service,
        }
        context = _LOG_CONTEXT.get()
        if context:
            payload.update(redact(context))
        fields = getattr(record, _FIELDS_ATTR, None)
        if fields:
            payload.update(redact(fields))
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _RESERVED_RECORD_KEYS and k != _FIELDS_ATTR
        }
        if extras:
            payload.update(redact(extras))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Human-friendly formatter for local development."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).strftime("%H:%M:%S")
        merged: dict[str, Any] = {}
        merged.update(_LOG_CONTEXT.get())
        merged.update(getattr(record, _FIELDS_ATTR, {}) or {})
        suffix = ""
        if merged:
            redacted = redact(merged)
            suffix = " " + " ".join(f"{k}={v}" for k, v in redacted.items())
        line = f"{timestamp} {record.levelname:<8} {record.name:<32} {record.getMessage()}{suffix}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


class BoundLogger:
    """A thin, keyword-friendly wrapper around :class:`logging.Logger`."""

    __slots__ = ("_fields", "_logger")

    def __init__(self, logger: logging.Logger, fields: Mapping[str, Any] | None = None) -> None:
        self._logger = logger
        self._fields: dict[str, Any] = dict(fields or {})

    def bind(self, **fields: Any) -> BoundLogger:
        """Return a new logger carrying additional structured fields."""
        merged = {**self._fields, **fields}
        return BoundLogger(self._logger, merged)

    @property
    def stdlib(self) -> logging.Logger:
        """Escape hatch to the underlying standard-library logger."""
        return self._logger

    def isEnabledFor(self, level: int) -> bool:  # noqa: N802 - stdlib compatibility
        return self._logger.isEnabledFor(level)

    def _log(self, level: int, event: str, exc_info: Any = None, **fields: Any) -> None:
        if not self._logger.isEnabledFor(level):
            return
        payload = {**self._fields, **fields}
        self._logger.log(
            level, event, exc_info=exc_info, extra={_FIELDS_ATTR: payload}, stacklevel=3
        )

    def debug(self, event: str, **fields: Any) -> None:
        self._log(logging.DEBUG, event, **fields)

    def info(self, event: str, **fields: Any) -> None:
        self._log(logging.INFO, event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._log(logging.WARNING, event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self._log(logging.ERROR, event, **fields)

    def exception(self, event: str, **fields: Any) -> None:
        self._log(logging.ERROR, event, exc_info=True, **fields)

    def critical(self, event: str, **fields: Any) -> None:
        self._log(logging.CRITICAL, event, **fields)


def get_logger(name: str, **fields: Any) -> BoundLogger:
    """Return a structured logger for ``name``."""
    return BoundLogger(logging.getLogger(name), fields)


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Attach fields to every log line emitted inside the block."""
    token = _LOG_CONTEXT.set({**_LOG_CONTEXT.get(), **fields})
    try:
        yield
    finally:
        _LOG_CONTEXT.reset(token)


def bind_context(**fields: Any) -> None:
    """Attach fields to the current context permanently (e.g. per request)."""
    _LOG_CONTEXT.set({**_LOG_CONTEXT.get(), **fields})


def get_context() -> dict[str, Any]:
    """Return a copy of the current logging context."""
    return dict(_LOG_CONTEXT.get())


def new_correlation_id() -> str:
    """Generate a correlation identifier for a request or pipeline run."""
    return uuid.uuid4().hex


def configure_logging(
    *,
    level: str = "INFO",
    fmt: str = "json",
    log_file: Path | None = None,
    service: str = "jobintel",
) -> None:
    """Install the platform logging configuration on the root logger."""
    formatter: logging.Formatter = (
        JsonFormatter(service=service) if fmt == "json" else ConsoleFormatter()
    )

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    root.setLevel(level.upper())

    # Third-party loggers are noisy; keep them at WARNING unless we are debugging.
    noisy = ("httpx", "httpcore", "asyncio", "aiosqlite", "uvicorn.access", "alembic")
    quiet_level = logging.DEBUG if level.upper() == "DEBUG" else logging.WARNING
    for name in noisy:
        logging.getLogger(name).setLevel(quiet_level)
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if level.upper() == "DEBUG" else logging.WARNING
    )


__all__ = [
    "REDACTED",
    "BoundLogger",
    "ConsoleFormatter",
    "JsonFormatter",
    "bind_context",
    "configure_logging",
    "get_context",
    "get_logger",
    "log_context",
    "mask_email",
    "new_correlation_id",
    "redact",
    "scrub_text",
]
