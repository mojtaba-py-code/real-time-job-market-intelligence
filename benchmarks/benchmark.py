"""Performance benchmarks.

Run everything:

    python benchmarks/benchmark.py

Run one stage at a larger scale:

    python benchmarks/benchmark.py --stage pipeline --scale 100000

The output is a table of throughput and latency numbers plus, optionally, a
JSON file for tracking regressions over time. Nothing here touches the network.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import tracemalloc
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import DatabaseSettings, Environment, Settings, StorageSettings
from app.core.text import shingles, tokenize
from app.ingestion.registry import SourceDefinition, SourceRegistry
from app.ingestion.sources.synthetic_source import (
    SyntheticConfig,
    SyntheticJobGenerator,
)
from app.models.analytics import AnalyticsFilter
from app.models.enums import SourceKind
from app.nlp.pipeline import NlpPipeline
from app.processing.deduplication.minhash import LshIndex, MinHasher
from app.processing.pipeline import ProcessingPipeline
from app.services.analytics_service import AnalyticsService
from app.services.ingestion_service import IngestionService
from app.storage.parquet import AnalyticalStore, ParquetExporter
from app.storage.session import Database

DEFAULT_SCALES = (1_000, 10_000)


@dataclass(slots=True)
class Measurement:
    """One benchmark result."""

    stage: str
    scale: int
    seconds: float
    records: int
    peak_memory_mb: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def throughput(self) -> float:
        return self.records / self.seconds if self.seconds > 0 else 0.0

    @property
    def per_record_ms(self) -> float:
        return (self.seconds * 1000) / self.records if self.records else 0.0

    def row(self) -> tuple[str, str, str, str, str, str]:
        return (
            self.stage,
            f"{self.scale:,}",
            f"{self.seconds:.2f}s",
            f"{self.throughput:,.0f}/s",
            f"{self.per_record_ms:.2f} ms",
            f"{self.peak_memory_mb:.0f} MB" if self.peak_memory_mb else "-",
        )


#: Memory tracing costs 2-4x in wall-clock time, so it is opt-in: throughput
#: numbers are meaningless while ``tracemalloc`` is running.
TRACE_MEMORY = False


def measure(stage: str, scale: int, records: int, **details: Any):
    """Context manager timing a block, optionally recording peak memory."""

    class _Timer:
        def __enter__(self) -> _Timer:
            if TRACE_MEMORY:
                tracemalloc.start()
            self.started = time.perf_counter()
            return self

        def __exit__(self, *_exc: object) -> None:
            elapsed = time.perf_counter() - self.started
            peak = 0
            if TRACE_MEMORY:
                _current, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
            self.result = Measurement(
                stage=stage,
                scale=scale,
                seconds=elapsed,
                records=records,
                peak_memory_mb=peak / (1024 * 1024),
                details=details,
            )

    return _Timer()


# --------------------------------------------------------------------------- #
# Stages
# --------------------------------------------------------------------------- #
def bench_generator(scale: int) -> Measurement:
    generator = SyntheticJobGenerator(SyntheticConfig(count=scale, days=180, seed=11))
    with measure("generate", scale, scale) as timer:
        jobs = generator.generate()
    timer.result.records = len(jobs)
    return timer.result


def bench_nlp(scale: int) -> Measurement:
    jobs = SyntheticJobGenerator(SyntheticConfig(count=min(scale, 5_000), seed=12)).generate()
    pipeline = NlpPipeline()
    sample = jobs[: min(scale, 5_000)]
    with measure("nlp", len(sample), len(sample)) as timer:
        for job in sample:
            pipeline.analyze(title=job.title or "", description=job.description)
    return timer.result


def bench_minhash(scale: int) -> Measurement:
    hasher = MinHasher(128)
    document = "we are hiring a senior python engineer for our data platform team " * 20
    tokens = shingles(tokenize(document), 5)
    iterations = min(scale, 5_000)
    with measure("minhash", iterations, iterations) as timer:
        for _ in range(iterations):
            hasher.signature(tokens)
    return timer.result


def bench_lsh(scale: int) -> Measurement:
    hasher = MinHasher(128)
    index = LshIndex(bands=16, permutations=128)
    corpus = min(scale, 20_000)
    signatures = [
        hasher.signature(shingles(tokenize(f"posting {i} python backend engineer " * 6), 4))
        for i in range(corpus)
    ]
    for position, signature in enumerate(signatures):
        index.add(f"job-{position}", signature)

    queries = min(500, corpus)
    with measure("lsh_query", corpus, queries, corpus=corpus) as timer:
        for signature in signatures[:queries]:
            index.query(signature, threshold=0.8)
    return timer.result


def bench_pipeline(scale: int) -> Measurement:
    jobs = SyntheticJobGenerator(SyntheticConfig(count=scale, days=180, seed=13)).generate()
    pipeline = ProcessingPipeline(settings=_settings())
    with measure("pipeline", scale, len(jobs)) as timer:
        result = pipeline.process(jobs, source="synthetic")
    timer.result.details = {
        "accepted": result.accepted_count,
        "duplicates": len(result.duplicates),
        "rejected": len(result.rejected),
        "quality": round(result.quality.score, 4) if result.quality else 0.0,
    }
    return timer.result


def bench_parquet(scale: int) -> Measurement:
    settings = _settings()
    jobs = (
        ProcessingPipeline(settings=settings)
        .process(
            SyntheticJobGenerator(SyntheticConfig(count=min(scale, 20_000), seed=14)).generate()
        )
        .jobs
    )
    exporter = ParquetExporter(settings.storage)
    with measure("parquet_write", len(jobs), len(jobs)) as timer:
        exporter.export(jobs)

    store = AnalyticalStore(settings.storage)
    started = time.perf_counter()
    store.skill_demand(limit=20)
    timer.result.details = {"duckdb_query_ms": round((time.perf_counter() - started) * 1000, 1)}
    return timer.result


async def bench_ingestion(scale: int) -> Measurement:
    settings = _settings()
    database = Database(settings)
    await database.create_all()
    registry = SourceRegistry(
        [
            SourceDefinition(
                name="synthetic",
                kind=SourceKind.SYNTHETIC,
                options={"count": scale, "days": 180, "seed": 15},
                batch_size=scale,
            )
        ],
        settings=settings,
    )
    service = IngestionService(database=database, registry=registry, settings=settings)
    try:
        with measure("ingestion", scale, scale) as timer:
            outcome = await service.run_source("synthetic")
        timer.result.records = outcome.run.records_received
        timer.result.details = {
            "created": outcome.run.jobs_created,
            "duplicates": outcome.run.duplicates_detected,
            "rejected": outcome.run.records_rejected,
        }

        analytics = AnalyticsService(database, settings=settings)
        flt = AnalyticsFilter(window_days=365, limit=25)
        latencies = []
        for _ in range(3):
            started = time.perf_counter()
            await analytics.overview(flt)
            latencies.append((time.perf_counter() - started) * 1000)
            await analytics.invalidate()
        timer.result.details["analytics_overview_ms"] = round(statistics.median(latencies), 1)
        return timer.result
    finally:
        await registry.aclose()
        await database.dispose()


def _settings() -> Settings:
    root = Path(__file__).resolve().parents[1] / "data" / "benchmarks"
    return Settings(
        environment=Environment.TESTING,
        database=DatabaseSettings(url="sqlite+aiosqlite:///:memory:"),
        storage=StorageSettings(
            data_dir=root,
            raw_dir=root / "raw",
            processed_dir=root / "processed",
            analytics_dir=root / "analytics",
        ),
    )


STAGES = {
    "generate": bench_generator,
    "nlp": bench_nlp,
    "minhash": bench_minhash,
    "lsh": bench_lsh,
    "pipeline": bench_pipeline,
    "parquet": bench_parquet,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Job Market Intelligence benchmarks")
    parser.add_argument(
        "--stage",
        choices=[*STAGES, "ingestion", "all"],
        default="all",
        help="Which stage to benchmark.",
    )
    parser.add_argument(
        "--scale",
        type=int,
        nargs="*",
        default=list(DEFAULT_SCALES),
        help="Record counts to benchmark.",
    )
    parser.add_argument("--json", type=Path, help="Write the raw results to a JSON file.")
    parser.add_argument(
        "--memory",
        action="store_true",
        help="Also record peak memory (slows every stage down by 2-4x).",
    )
    args = parser.parse_args()

    global TRACE_MEMORY
    TRACE_MEMORY = args.memory

    results: list[Measurement] = []
    stages = list(STAGES) if args.stage == "all" else [args.stage]

    for scale in args.scale:
        for stage in stages:
            if stage == "ingestion":
                continue
            print(f"running {stage} at {scale:,} ...", flush=True)
            results.append(STAGES[stage](scale))
        if args.stage in ("all", "ingestion"):
            print(f"running ingestion at {scale:,} ...", flush=True)
            results.append(asyncio.run(bench_ingestion(scale)))

    _print_table(results)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps([asdict(item) for item in results], indent=2, default=str),
            encoding="utf-8",
        )
        print(f"\nwrote {args.json}")
    return 0


def _print_table(results: list[Measurement]) -> None:
    headers = ("stage", "scale", "elapsed", "throughput", "per record", "peak mem")
    rows = [item.row() for item in results]
    widths = [
        max(len(headers[i]), max((len(row[i]) for row in rows), default=0))
        for i in range(len(headers))
    ]

    def line(values: tuple[str, ...]) -> str:
        return "  ".join(value.ljust(widths[i]) for i, value in enumerate(values))

    print()
    print(line(headers))
    print("  ".join("-" * width for width in widths))
    for item, row in zip(results, rows, strict=True):
        print(line(row))
        if item.details:
            print("    " + ", ".join(f"{k}={v}" for k, v in item.details.items()))


if __name__ == "__main__":
    raise SystemExit(main())
