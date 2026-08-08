"""Declarative field mapping from arbitrary payloads to :class:`RawJob`.

Rather than writing a bespoke parser per API, a source declares *where* each
canonical field lives::

    mapping:
      source_job_id: id
      title: title
      company: company.name          # dotted path into nested objects
      location: locations.0.name     # numeric segment indexes a list
      remote_status: "@remote"       # a literal, prefixed with '@'

This keeps new sources to a configuration change instead of a code change.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.core.errors import SourceConfigurationError

LITERAL_PREFIX = "@"

#: Fields a mapping may target. Anything else is a configuration error.
MAPPABLE_FIELDS = frozenset(
    {
        "source_job_id",
        "url",
        "title",
        "description",
        "company",
        "location",
        "salary",
        "employment_type",
        "remote_status",
        "industry",
        "published_at",
    }
)

#: Field names tried when a source does not declare an explicit mapping.
DEFAULT_CANDIDATES: dict[str, tuple[str, ...]] = {
    "source_job_id": ("id", "job_id", "guid", "slug", "reference", "uuid"),
    "url": ("url", "link", "absolute_url", "apply_url", "job_url", "applyUrl"),
    "title": ("title", "name", "position", "job_title", "jobTitle"),
    "description": ("description", "content", "summary", "job_description", "body"),
    "company": ("company", "company_name", "companyName", "employer", "organization"),
    "location": ("location", "candidate_required_location", "job_location", "city", "place"),
    "salary": ("salary", "salary_range", "compensation", "pay", "salaryRange"),
    "employment_type": ("job_type", "employment_type", "type", "contract_type", "employmentType"),
    "remote_status": ("remote", "is_remote", "workplace_type", "remote_status", "workplaceType"),
    "industry": ("industry", "category", "department", "sector"),
    "published_at": (
        "publication_date",
        "published_at",
        "created_at",
        "posted_at",
        "date",
        "pubDate",
        "updated_at",
    ),
}


def extract_path(payload: Any, path: str) -> Any:
    """Resolve a dotted path against nested dictionaries and lists.

    Returns ``None`` when any segment is missing - a missing field is a
    data-quality signal, not an error.
    """
    current = payload
    for segment in path.split("."):
        if current is None:
            return None
        if isinstance(current, Mapping):
            current = current.get(segment)
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes)):
            if not segment.lstrip("-").isdigit():
                return None
            index = int(segment)
            if -len(current) <= index < len(current):
                current = current[index]
            else:
                return None
        else:
            return None
    return current


class RecordMapper:
    """Turns a source-specific payload into canonical raw-job fields."""

    def __init__(self, mapping: Mapping[str, str] | None = None) -> None:
        self._mapping = dict(mapping or {})
        unknown = set(self._mapping) - MAPPABLE_FIELDS
        if unknown:
            raise SourceConfigurationError(
                f"unknown mapping target(s): {', '.join(sorted(unknown))}",
                details={"allowed": sorted(MAPPABLE_FIELDS)},
            )

    def resolve(self, field: str, payload: Any) -> Any:
        """Read one canonical field out of a payload."""
        expression = self._mapping.get(field)
        if expression is not None:
            if expression.startswith(LITERAL_PREFIX):
                return expression[len(LITERAL_PREFIX) :]
            return extract_path(payload, expression)
        for candidate in DEFAULT_CANDIDATES.get(field, ()):
            value = extract_path(payload, candidate)
            if value not in (None, ""):
                return value
        return None

    def to_fields(self, payload: Any) -> dict[str, Any]:
        """Extract every mappable field from a payload."""
        return {field: self.resolve(field, payload) for field in MAPPABLE_FIELDS}


def stringify(value: Any) -> str | None:
    """Coerce a mapped value into a trimmed string, or ``None``."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, Mapping):
        for key in ("name", "label", "title", "value", "text"):
            if key in value:
                return stringify(value[key])
        return None
    if isinstance(value, Sequence):
        parts = [stringify(item) for item in value]
        joined = ", ".join(p for p in parts if p)
        return joined or None
    return str(value)


__all__ = [
    "DEFAULT_CANDIDATES",
    "LITERAL_PREFIX",
    "MAPPABLE_FIELDS",
    "RecordMapper",
    "extract_path",
    "stringify",
]
