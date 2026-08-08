"""Performance regressions.

These are skipped by default; run them with ``JOBINTEL_RUN_PERF=1``. The
thresholds are deliberately loose - they exist to catch an order-of-magnitude
regression (an accidental O(n^2), a per-record database round trip), not to
benchmark the host.
"""

from __future__ import annotations

import time

import pytest

from app.core.text import shingles, tokenize
from app.ingestion.sources.synthetic_source import SyntheticConfig, SyntheticJobGenerator
from app.models.analytics import AnalyticsFilter
from app.processing.deduplication.minhash import LshIndex, MinHasher
from app.processing.pipeline import ProcessingPipeline
from app.services.analytics_service import AnalyticsService
from app.services.ingestion_service import IngestionService
from app.storage.session import Database

pytestmark = pytest.mark.performance

#: Minimum records per second the processing pipeline must sustain.
MIN_PROCESSING_THROUGHPUT = 40.0
#: Minimum documents per second the similarity signature must sustain.
MIN_SIGNATURE_THROUGHPUT = 100.0


def test_generator_throughput() -> None:
    started = time.perf_counter()
    jobs = SyntheticJobGenerator(SyntheticConfig(count=5_000, days=180, seed=1)).generate()
    elapsed = time.perf_counter() - started
    rate = len(jobs) / elapsed
    print(f"\ngenerator: {rate:,.0f} postings/s ({len(jobs)} in {elapsed:.2f}s)")
    assert rate > 500


def test_processing_pipeline_throughput(pipeline: ProcessingPipeline) -> None:
    jobs = SyntheticJobGenerator(SyntheticConfig(count=1_000, days=120, seed=2)).generate()

    started = time.perf_counter()
    result = pipeline.process(jobs, source="synthetic")
    elapsed = time.perf_counter() - started

    rate = len(jobs) / elapsed
    print(
        f"\npipeline: {rate:,.0f} records/s "
        f"({result.accepted_count} accepted, {len(result.duplicates)} duplicates)"
    )
    assert rate > MIN_PROCESSING_THROUGHPUT


def test_signature_throughput() -> None:
    hasher = MinHasher(128)
    document = "we are hiring a senior python engineer to build data pipelines " * 30
    tokens = shingles(tokenize(document), 5)

    started = time.perf_counter()
    for _ in range(500):
        hasher.signature(tokens)
    elapsed = time.perf_counter() - started

    rate = 500 / elapsed
    print(f"\nminhash: {rate:,.0f} documents/s")
    assert rate > MIN_SIGNATURE_THROUGHPUT


def test_lsh_lookup_stays_sublinear() -> None:
    """Adding ten times the corpus must not make a lookup ten times slower."""
    hasher = MinHasher(128)
    index = LshIndex(bands=16, permutations=128)
    signatures = [
        hasher.signature(shingles(tokenize(f"job posting number {i} python backend " * 8), 4))
        for i in range(2_000)
    ]
    for position, signature in enumerate(signatures):
        index.add(f"job-{position}", signature)

    started = time.perf_counter()
    for signature in signatures[:200]:
        index.query(signature, threshold=0.8)
    elapsed = time.perf_counter() - started

    per_query_ms = (elapsed / 200) * 1000
    print(f"\nlsh: {per_query_ms:.2f} ms/query over {len(index)} documents")
    assert per_query_ms < 25


async def test_ingestion_scales_to_a_large_batch(database: Database, settings: object) -> None:
    from app.ingestion.registry import SourceDefinition, SourceRegistry
    from app.models.enums import SourceKind

    registry = SourceRegistry(
        [
            SourceDefinition(
                name="synthetic",
                kind=SourceKind.SYNTHETIC,
                options={"count": 2_000, "days": 180, "seed": 3},
                batch_size=2_000,
            )
        ],
        settings=settings,  # type: ignore[arg-type]
    )
    service = IngestionService(database=database, registry=registry, settings=settings)  # type: ignore[arg-type]

    started = time.perf_counter()
    outcome = await service.run_source("synthetic")
    elapsed = time.perf_counter() - started

    rate = outcome.run.records_received / elapsed
    print(
        f"\ningestion: {rate:,.0f} records/s ({outcome.run.jobs_created} created in {elapsed:.2f}s)"
    )
    assert outcome.run.jobs_created > 1_000
    assert rate > 30


async def test_analytics_latency(database: Database, settings: object) -> None:
    from app.ingestion.registry import SourceDefinition, SourceRegistry
    from app.models.enums import SourceKind

    registry = SourceRegistry(
        [
            SourceDefinition(
                name="synthetic",
                kind=SourceKind.SYNTHETIC,
                options={"count": 2_000, "days": 180, "seed": 4},
                batch_size=2_000,
            )
        ],
        settings=settings,  # type: ignore[arg-type]
    )
    await IngestionService(
        database=database,
        registry=registry,
        settings=settings,  # type: ignore[arg-type]
    ).run_source("synthetic")

    analytics = AnalyticsService(database, settings=settings)  # type: ignore[arg-type]
    flt = AnalyticsFilter(window_days=365, limit=25)

    started = time.perf_counter()
    await analytics.overview(flt)
    overview_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    await analytics.skills(flt)
    skills_ms = (time.perf_counter() - started) * 1000

    print(f"\nanalytics: overview {overview_ms:.0f} ms · skills {skills_ms:.0f} ms")
    assert overview_ms < 15_000
    assert skills_ms < 5_000
