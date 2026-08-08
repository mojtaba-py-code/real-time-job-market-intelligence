"""Shared test fixtures.

Every test runs against an isolated in-memory database and an in-process cache
and event bus, so the suite needs no external services and leaves no state
behind.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

os.environ.setdefault("JOBINTEL_ENVIRONMENT", "testing")
os.environ.setdefault("JOBINTEL_SECURITY__SECRET_KEY", "test-secret-key-" + "x" * 40)
os.environ.setdefault("JOBINTEL_OBSERVABILITY__LOG_LEVEL", "WARNING")

from app.core.config import (
    AlertSettings,
    ApiSettings,
    CacheSettings,
    DatabaseSettings,
    Environment,
    Settings,
    StorageSettings,
)
from app.events.bus import InMemoryEventBus
from app.ingestion.registry import SourceDefinition, SourceRegistry
from app.ingestion.sources.synthetic_source import (
    SyntheticConfig,
    SyntheticJobGenerator,
)
from app.models.enums import SourceKind
from app.models.job import NormalizedJob
from app.models.raw import RawJob
from app.processing.pipeline import ProcessingPipeline
from app.storage.cache import InMemoryCache
from app.storage.session import Database

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Isolated settings pointing at a temporary data directory."""
    return Settings(
        environment=Environment.TESTING,
        database=DatabaseSettings(url="sqlite+aiosqlite:///:memory:"),
        cache=CacheSettings(url=None, enabled=True),
        storage=StorageSettings(
            data_dir=tmp_path,
            raw_dir=tmp_path / "raw",
            processed_dir=tmp_path / "processed",
            analytics_dir=tmp_path / "analytics",
        ),
        api=ApiSettings(rate_limit_enabled=False, serve_dashboard=False, cors_origins=[]),
        alerts=AlertSettings(enabled=True, smtp_host=None),
    )


@pytest.fixture
async def database(settings: Settings) -> AsyncIterator[Database]:
    """A fresh in-memory database with the schema created."""
    db = Database(settings)
    await db.create_all()
    try:
        yield db
    finally:
        await db.dispose()


@pytest.fixture
def cache() -> InMemoryCache:
    return InMemoryCache(default_ttl_seconds=60)


@pytest.fixture
def event_bus() -> InMemoryEventBus:
    return InMemoryEventBus()


@pytest.fixture
def pipeline(settings: Settings) -> ProcessingPipeline:
    return ProcessingPipeline(settings=settings)


@pytest.fixture
def synthetic_registry(settings: Settings) -> SourceRegistry:
    """A registry with a small, deterministic synthetic source."""
    return SourceRegistry(
        [
            SourceDefinition(
                name="synthetic",
                kind=SourceKind.SYNTHETIC,
                options={"count": 120, "days": 90, "seed": 424242},
                batch_size=120,
            )
        ],
        settings=settings,
    )


@pytest.fixture
def raw_jobs() -> list[RawJob]:
    """A deterministic batch of raw postings."""
    return SyntheticJobGenerator(SyntheticConfig(count=60, days=90, seed=7)).generate()


@pytest.fixture
def sample_raw_job() -> RawJob:
    """One well-formed posting with a rich description."""
    return RawJob(
        source="test",
        source_job_id="job-1",
        url="https://jobs.example.com/senior-python-engineer?utm_source=x",
        title="Senior Backend Python Engineer (m/f/d)",
        company="Northwind Analytics GmbH",
        location="Berlin, Germany",
        salary="70,000 - 90,000 EUR per year",
        employment_type="Full-time",
        remote_status="hybrid",
        industry="Software",
        description=(
            "About Northwind Analytics\n"
            "We build data products used across Europe.\n\n"
            "Requirements\n"
            "- 6+ years of professional experience with Python and FastAPI\n"
            "- Strong PostgreSQL and Docker skills\n"
            "- Experience operating services on Kubernetes\n\n"
            "Nice to have\n"
            "- Terraform and AWS exposure\n"
            "- Familiarity with Apache Kafka\n"
        ),
    )


@pytest.fixture
def normalized_jobs(pipeline: ProcessingPipeline, raw_jobs: list[RawJob]) -> list[NormalizedJob]:
    """Raw postings pushed through the full processing pipeline."""
    return pipeline.process(raw_jobs, source="test", run_id="test-run").jobs


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip performance tests unless explicitly enabled."""
    if os.environ.get("JOBINTEL_RUN_PERF") == "1":
        return
    skip = pytest.mark.skip(reason="set JOBINTEL_RUN_PERF=1 to run performance tests")
    for item in items:
        if "performance" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _quiet_logging() -> Iterator[None]:
    """Keep the suite's output readable."""
    import logging

    logging.getLogger().setLevel(logging.CRITICAL)
    yield
