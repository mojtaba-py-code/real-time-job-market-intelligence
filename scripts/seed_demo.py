"""Seed a demo environment end to end.

    python scripts/seed_demo.py --count 5000 --days 180

Creates the schema, loads the skill taxonomy, ingests a synthetic corpus with
known market trends, refreshes the derived data and prints what the analytics
layer recovered — a one-command demo of the whole platform.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.ingestion.registry import SourceDefinition, SourceRegistry
from app.models.analytics import AnalyticsFilter
from app.models.enums import SourceKind
from app.services.admin_service import AdminService
from app.services.analytics_service import AnalyticsService
from app.services.ingestion_service import IngestionService
from app.storage.session import get_database


async def seed(count: int, days: int, seed_value: int) -> int:
    settings = get_settings()
    settings.ensure_directories()
    configure_logging(level="WARNING", fmt="console", service="jobintel-seed")

    database = get_database(settings)
    registry = SourceRegistry(
        [
            SourceDefinition(
                name="synthetic",
                kind=SourceKind.SYNTHETIC,
                options={"count": count, "days": days, "seed": seed_value},
                batch_size=count,
            )
        ],
        settings=settings,
    )
    admin = AdminService(database, settings=settings)
    ingestion = IngestionService(database=database, registry=registry, settings=settings)
    analytics = AnalyticsService(database, settings=settings)

    try:
        print("1/5  creating the schema ...", flush=True)
        await database.create_all()

        print("2/5  loading the skill taxonomy ...", flush=True)
        print(f"     {await admin.sync_taxonomy()} nodes")

        print(f"3/5  ingesting {count:,} synthetic postings ...", flush=True)
        outcome = await ingestion.run_source("synthetic")
        run = outcome.run
        print(
            f"     received {run.records_received:,} · created {run.jobs_created:,}"
            f" · duplicates {run.duplicates_detected:,} · rejected {run.records_rejected:,}"
            f" · quality {run.quality_score:.3f}"
        )

        print("4/5  refreshing derived data ...", flush=True)
        print(f"     exported {await admin.export_analytics(limit=count):,} rows to Parquet")
        await admin.refresh_company_counts()
        await admin.snapshot_market()

        print("5/5  what the analytics layer recovered:\n", flush=True)
        flt = AnalyticsFilter(window_days=min(days * 2, 1825), limit=10)
        overview = await analytics.overview(flt)
        print(
            f"     active postings   {overview.active_jobs:,}\n"
            f"     companies hiring  {overview.companies_hiring:,}"
            f" in {overview.countries_covered} countries\n"
            f"     remote share      {overview.remote_share * 100:.1f}%\n"
            f"     data quality      {overview.data_quality_score * 100:.0f}%"
        )
        if overview.median_salary:
            print(f"     median salary     USD {overview.median_salary:,.0f}")

        print("\n     top skills:")
        for skill in (await analytics.skills(flt))[:8]:
            print(f"       {skill.name:<18} {skill.job_count:>6,}  {skill.share * 100:>5.1f}%")

        print("\n     fastest movers:")
        for trend in await analytics.top_trends(flt, limit=6):
            window = next((w for w in trend.windows if w.days == 30), None)
            change = (
                "—" if not window or window.change_pct is None else f"{window.change_pct:+.0f}%"
            )
            print(f"       {trend.name:<18} {change:>7}  {trend.direction}")

        emerging = await analytics.emerging(window_days=min(days, 90), limit=5)
        if emerging:
            print("\n     emerging technologies:")
            for item in emerging:
                growth = "new" if item.growth_pct is None else f"{item.growth_pct:+.0f}%"
                print(f"       {item.name:<18} {growth:>7}  confidence {item.confidence:.2f}")

        print("\nDone. Start the API with:  python -m app serve")
        return 0
    finally:
        await registry.aclose()
        await database.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed a demo environment")
    parser.add_argument("--count", type=int, default=5_000, help="Postings to generate")
    parser.add_argument("--days", type=int, default=180, help="Period they span")
    parser.add_argument("--seed", type=int, default=20240101, help="Generator seed")
    args = parser.parse_args()
    return asyncio.run(seed(args.count, args.days, args.seed))


if __name__ == "__main__":
    raise SystemExit(main())
