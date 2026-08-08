"""Source registry - turns configuration into live adapters.

Sources are declared in ``configs/sources.yaml``. Adding one is a
configuration change; the ingestion pipeline itself never learns about
individual providers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.core.config import Settings, get_settings
from app.core.errors import SourceConfigurationError
from app.core.logging import get_logger
from app.ingestion.base import BaseJobSource
from app.ingestion.http import SafeHttpClient
from app.ingestion.sources.api_source import ApiJobSource
from app.ingestion.sources.company_career import CompanyCareerSource
from app.ingestion.sources.dataset_source import DatasetSource
from app.ingestion.sources.rss_source import RssJobSource
from app.ingestion.sources.synthetic_source import SyntheticJobSource
from app.models.enums import SourceKind

log = get_logger(__name__)

DEFAULT_CONFIG_PATH = Path("configs") / "sources.yaml"

#: Adapters that need an HTTP client injected.
_NETWORK_KINDS = frozenset({SourceKind.API, SourceKind.RSS, SourceKind.CAREER_PAGE})


@dataclass(slots=True)
class SourceDefinition:
    """One entry of the sources configuration file."""

    name: str
    kind: SourceKind
    enabled: bool = True
    schedule_seconds: int | None = None
    batch_size: int | None = None
    options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> SourceDefinition:
        """Validate and build a definition from raw YAML."""
        name = str(payload.get("name") or "").strip()
        if not name:
            raise SourceConfigurationError("every source needs a name")
        raw_kind = str(payload.get("kind") or "api").strip().lower()
        try:
            kind = SourceKind(raw_kind)
        except ValueError as exc:
            raise SourceConfigurationError(
                f"source {name!r} has unknown kind {raw_kind!r}",
                details={"supported": [k.value for k in SourceKind]},
            ) from exc
        options = payload.get("options") or {}
        if not isinstance(options, dict):
            raise SourceConfigurationError(f"source {name!r}: options must be a mapping")
        schedule = payload.get("schedule_seconds")
        batch = payload.get("batch_size")
        return cls(
            name=name,
            kind=kind,
            enabled=bool(payload.get("enabled", True)),
            schedule_seconds=int(schedule) if schedule is not None else None,
            batch_size=int(batch) if batch is not None else None,
            options=dict(options),
        )


class SourceRegistry:
    """Creates and owns source adapters."""

    def __init__(
        self,
        definitions: list[SourceDefinition] | None = None,
        *,
        settings: Settings | None = None,
        http_client: SafeHttpClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._definitions = {d.name: d for d in (definitions or [])}
        self._http_client = http_client
        self._owns_http_client = http_client is None

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #
    @classmethod
    def from_file(
        cls,
        path: Path | str | None = None,
        *,
        settings: Settings | None = None,
        http_client: SafeHttpClient | None = None,
    ) -> SourceRegistry:
        """Load definitions from a YAML file.

        A missing file is not an error: the platform falls back to the
        synthetic source so a fresh checkout works with no configuration.
        """
        config_path = Path(path) if path else DEFAULT_CONFIG_PATH
        if not config_path.exists():
            log.warning("sources.config_missing", path=str(config_path))
            return cls(
                [SourceDefinition(name="synthetic", kind=SourceKind.SYNTHETIC)],
                settings=settings,
                http_client=http_client,
            )

        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        entries = payload.get("sources") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            raise SourceConfigurationError(
                f"{config_path} must contain a 'sources' list",
                details={"path": str(config_path)},
            )
        definitions = [SourceDefinition.from_mapping(entry) for entry in entries]
        seen: set[str] = set()
        for definition in definitions:
            if definition.name in seen:
                raise SourceConfigurationError(f"duplicate source name {definition.name!r}")
            seen.add(definition.name)
        return cls(definitions, settings=settings, http_client=http_client)

    @property
    def http_client(self) -> SafeHttpClient:
        """Lazily created shared HTTP client."""
        if self._http_client is None:
            self._http_client = SafeHttpClient(self._settings.ingestion)
        return self._http_client

    # ------------------------------------------------------------------ #
    # Access
    # ------------------------------------------------------------------ #
    def definitions(self, *, enabled_only: bool = False) -> list[SourceDefinition]:
        values = list(self._definitions.values())
        if enabled_only:
            values = [d for d in values if d.enabled]
        return sorted(values, key=lambda d: d.name)

    def get_definition(self, name: str) -> SourceDefinition:
        definition = self._definitions.get(name)
        if definition is None:
            raise SourceConfigurationError(
                f"unknown source {name!r}",
                details={"known": sorted(self._definitions)},
            )
        return definition

    def register(self, definition: SourceDefinition) -> None:
        """Add or replace a definition at runtime (used by tests and the CLI)."""
        self._definitions[definition.name] = definition

    def create(self, name: str) -> BaseJobSource:
        """Instantiate the adapter for a configured source."""
        definition = self.get_definition(name)
        return self._build(definition)

    def create_all(self, *, enabled_only: bool = True) -> list[BaseJobSource]:
        """Instantiate every configured adapter."""
        return [self._build(d) for d in self.definitions(enabled_only=enabled_only)]

    def _build(self, definition: SourceDefinition) -> BaseJobSource:
        options = dict(definition.options)
        match definition.kind:
            case SourceKind.SYNTHETIC:
                return SyntheticJobSource(name=definition.name, options=options)
            case SourceKind.DATASET:
                return DatasetSource(name=definition.name, options=options)
            case SourceKind.RSS:
                return RssJobSource(name=definition.name, options=options, client=self.http_client)
            case SourceKind.CAREER_PAGE:
                return CompanyCareerSource(
                    name=definition.name, options=options, client=self.http_client
                )
            case SourceKind.API:
                return ApiJobSource(name=definition.name, options=options, client=self.http_client)
        raise SourceConfigurationError(  # pragma: no cover - exhaustive match above
            f"no adapter for kind {definition.kind}"
        )

    async def aclose(self) -> None:
        """Release the shared HTTP client if the registry created it."""
        if self._http_client is not None and self._owns_http_client:
            await self._http_client.aclose()
            self._http_client = None


__all__ = ["DEFAULT_CONFIG_PATH", "SourceDefinition", "SourceRegistry"]
