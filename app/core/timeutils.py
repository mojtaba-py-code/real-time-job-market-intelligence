"""Timezone-safe date/time helpers.

Every timestamp inside the platform is timezone-aware UTC. Sources send us a
zoo of formats (ISO-8601, RFC-2822 from RSS, epoch seconds, bare dates); this
module funnels all of them into a single representation.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime

_ISO_FALLBACK_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d %b %Y",
    "%d %B %Y",
)

#: Epoch values outside this range are treated as nonsense rather than dates.
_MIN_EPOCH = 0
_MAX_EPOCH = 4_102_444_800  # 2100-01-01


def utcnow() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Attach UTC to a naive datetime, or convert an aware one to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_datetime(value: object) -> datetime | None:
    """Best-effort parsing of a timestamp coming from an external source.

    Returns ``None`` instead of raising: an unparseable date is a data-quality
    signal, not a crash.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 1e11:  # milliseconds
            number /= 1000.0
        if not _MIN_EPOCH <= number <= _MAX_EPOCH:
            return None
        return datetime.fromtimestamp(number, tz=UTC)
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None

    if text.isdigit() and len(text) in (10, 13):
        return parse_datetime(int(text))

    candidate = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        return ensure_utc(datetime.fromisoformat(candidate))
    except ValueError:
        pass

    for fmt in _ISO_FALLBACK_FORMATS:
        try:
            # These formats are deliberately offset-free; ensure_utc()
            # attaches UTC, which is the platform's documented assumption.
            return ensure_utc(datetime.strptime(text, fmt))  # noqa: DTZ007
        except ValueError:
            continue

    try:  # RFC-2822, used by RSS feeds
        return ensure_utc(parsedate_to_datetime(text))
    except (TypeError, ValueError):
        return None


def day_start(value: datetime) -> datetime:
    """Truncate a timestamp to midnight UTC."""
    aware = ensure_utc(value)
    return aware.replace(hour=0, minute=0, second=0, microsecond=0)


def days_ago(days: int, *, reference: datetime | None = None) -> datetime:
    """Timestamp ``days`` before ``reference`` (default: now)."""
    return (reference or utcnow()) - timedelta(days=days)


def date_range(start: date, end: date) -> Iterator[date]:
    """Yield every day from ``start`` to ``end`` inclusive."""
    if end < start:
        return
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def to_date(value: datetime | date) -> date:
    """Normalise a datetime/date to a plain UTC date."""
    if isinstance(value, datetime):
        return ensure_utc(value).date()
    return value


def isoformat(value: datetime | None) -> str | None:
    """Render a timestamp as an ISO-8601 string, or ``None``."""
    return ensure_utc(value).isoformat() if value is not None else None


def humanize_duration(seconds: float) -> str:
    """Render a duration for logs and CLI output."""
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


__all__ = [
    "date_range",
    "day_start",
    "days_ago",
    "ensure_utc",
    "humanize_duration",
    "isoformat",
    "parse_datetime",
    "to_date",
    "utcnow",
]
