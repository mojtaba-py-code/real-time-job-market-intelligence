"""The source abstraction.

Every data source - a public API, an RSS feed, a company career endpoint, a
CSV dataset or the synthetic generator - implements the same tiny contract.
That is what makes the ingestion layer replaceable: adding a source means
adding one class and one configuration entry, and touching nothing else.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from app.core.errors import SourceConfigurationError
from app.core.logging import BoundLogger, get_logger
from app.models.enums import SourceKind
from app.models.raw import RawBatch, RawJob


@dataclass(slots=True)
class SourceContext:
    """Everything a source needs to know about the run it participates in."""

    #: Cursor stored by the previous successful run (opaque to the platform).
    cursor: str | None = None
    #: Newest posting timestamp seen so far, for incremental fetching.
    since: datetime | None = None
    #: HTTP entity tag of the previous fetch, for conditional requests.
    etag: str | None = None
    #: Upper bound on how many records the caller wants.
    limit: int = 500
    #: Free-form per-source configuration from ``configs/sources.yaml``.
    options: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None


@runtime_checkable
class JobSource(Protocol):
    """Structural contract implemented by every adapter."""

    name: str
    kind: SourceKind

    async def fetch_jobs(self, context: SourceContext) -> RawBatch:
        """Return a batch of raw postings."""
        ...


class BaseJobSource(ABC):
    """Convenience base class with the plumbing every adapter shares."""

    #: Unique identifier used in configuration, storage and the CLI.
    name: str = "base"
    kind: SourceKind = SourceKind.API

    def __init__(self, *, name: str | None = None, options: dict[str, Any] | None = None) -> None:
        if name:
            self.name = name
        self.options = dict(options or {})
        self.log: BoundLogger = get_logger(type(self).__module__).bind(source=self.name)
        self.validate_options()

    def validate_options(self) -> None:
        """Fail fast on misconfiguration. Overridden by adapters that need it."""
        return

    def require_option(self, key: str) -> Any:
        """Read a mandatory configuration value."""
        if key not in self.options or self.options[key] in (None, ""):
            raise SourceConfigurationError(
                f"source {self.name!r} requires the {key!r} option",
                details={"source": self.name, "option": key},
            )
        return self.options[key]

    @abstractmethod
    async def fetch_jobs(self, context: SourceContext) -> RawBatch:
        """Fetch raw postings. Implemented by every adapter."""
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release adapter-owned resources. Overridden when needed."""
        return

    def build_batch(
        self,
        jobs: list[RawJob],
        *,
        cursor: str | None = None,
        has_more: bool = False,
        warnings: list[str] | None = None,
    ) -> RawBatch:
        """Wrap raw jobs into the batch envelope."""
        return RawBatch(
            source=self.name,
            source_kind=self.kind,
            jobs=jobs,
            cursor=cursor,
            has_more=has_more,
            warnings=warnings or [],
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<{type(self).__name__} name={self.name!r} kind={self.kind}>"


__all__ = ["BaseJobSource", "JobSource", "SourceContext"]
