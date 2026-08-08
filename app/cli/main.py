"""The ``jobintel`` command-line interface.

One entry point for every operational task: run the pipeline, inspect the
market, manage credentials, serve the API and start the worker. Output is
human-readable by default and machine-readable with ``--json``, so the same
commands work in a terminal and in a cron job.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, TypeVar

import typer
from rich.console import Console
from rich.table import Table

from app import __version__
from app.core.config import Settings, get_settings
from app.core.errors import JobIntelError
from app.core.logging import configure_logging, get_logger
from app.models.analytics import AnalyticsFilter
from app.models.enums import Scope, UserRole
from app.storage.session import Database, get_database

console = Console()
log = get_logger(__name__)

app = typer.Typer(
    name="jobintel",
    help="Real-Time Job Market Intelligence Platform.",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(help="Database schema management.")
keys_app = typer.Typer(help="API credential management.")
sources_app = typer.Typer(help="Data-source management.")
app.add_typer(db_app, name="db")
app.add_typer(keys_app, name="keys")
app.add_typer(sources_app, name="sources")

T = TypeVar("T")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _settings() -> Settings:
    settings = get_settings()
    configure_logging(
        level=settings.observability.log_level,
        fmt="console",
        service="jobintel-cli",
    )
    settings.ensure_directories()
    return settings


def _database(settings: Settings) -> Database:
    return get_database(settings)


def run_async(coroutine: Callable[[], Awaitable[T]]) -> T:
    """Run an async command body, turning platform errors into clean exits."""
    try:
        return asyncio.run(coroutine())  # type: ignore[arg-type]
    except JobIntelError as exc:
        console.print(f"[bold red]{exc.code}[/]: {exc.message}")
        raise typer.Exit(code=1) from exc
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        console.print("[yellow]interrupted[/]")
        raise typer.Exit(code=130) from None


def emit(payload: Any, *, as_json: bool) -> bool:
    """Print JSON when asked; report whether output was already produced."""
    if as_json:
        console.print_json(json.dumps(payload, default=str))
        return True
    return False


def table(title: str, columns: list[str]) -> Table:
    grid = Table(title=title, title_style="bold", header_style="bold cyan", expand=False)
    for index, column in enumerate(columns):
        grid.add_column(column, justify="right" if index else "left", overflow="fold")
    return grid


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
@app.command()
def ingest(
    source: Annotated[str | None, typer.Option(help="Source name; omit to run all.")] = None,
    limit: Annotated[int | None, typer.Option(help="Maximum records to fetch.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Fetch, process and store postings."""
    settings = _settings()

    async def body() -> list[dict[str, Any]]:
        from app.ingestion.registry import SourceRegistry
        from app.services.ingestion_service import IngestionService

        database = _database(settings)
        registry = SourceRegistry.from_file(settings=settings)
        service = IngestionService(database=database, registry=registry, settings=settings)
        try:
            outcomes = (
                [await service.run_source(source, limit=limit)]
                if source
                else await service.run_all(limit=limit)
            )
        finally:
            await registry.aclose()
            await database.dispose()
        return [
            {
                "source": outcome.run.source,
                "status": str(outcome.run.status),
                "received": outcome.run.records_received,
                "created": outcome.run.jobs_created,
                "updated": outcome.run.jobs_updated,
                "duplicates": outcome.run.duplicates_detected,
                "rejected": outcome.run.records_rejected,
                "quality": round(outcome.run.quality_score, 3),
                "seconds": round(outcome.run.duration_seconds, 2),
                "error": outcome.error,
            }
            for outcome in outcomes
        ]

    rows = run_async(body)
    if emit(rows, as_json=as_json):
        return
    grid = table(
        "Ingestion",
        [
            "Source",
            "Status",
            "Received",
            "Created",
            "Updated",
            "Dupes",
            "Rejected",
            "Quality",
            "Sec",
        ],
    )
    for row in rows:
        grid.add_row(
            row["source"],
            row["status"],
            str(row["received"]),
            str(row["created"]),
            str(row["updated"]),
            str(row["duplicates"]),
            str(row["rejected"]),
            f"{row['quality']:.3f}",
            f"{row['seconds']:.2f}",
        )
    console.print(grid)
    for row in rows:
        if row["error"]:
            console.print(f"[red]{row['source']}[/]: {row['error']}")


@app.command()
def process(
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run the post-ingestion steps: expiry, counters, export and snapshot."""
    settings = _settings()

    async def body() -> dict[str, Any]:
        from app.ingestion.registry import SourceRegistry
        from app.services.admin_service import AdminService
        from app.services.ingestion_service import IngestionService

        database = _database(settings)
        registry = SourceRegistry.from_file(settings=settings)
        ingestion = IngestionService(database=database, registry=registry, settings=settings)
        admin = AdminService(database, settings=settings)
        try:
            expired = await ingestion.expire_stale()
            companies = await admin.refresh_company_counts()
            exported = await admin.export_analytics()
            snapshot = await admin.snapshot_market()
        finally:
            await registry.aclose()
            await database.dispose()
        return {
            "expired": expired,
            "companies_refreshed": companies,
            "rows_exported": exported,
            "active_jobs": snapshot.get("active_jobs", 0),
        }

    result = run_async(body)
    if emit(result, as_json=as_json):
        return
    console.print(
        f"expired [bold]{result['expired']}[/] · companies [bold]{result['companies_refreshed']}[/]"
        f" · exported [bold]{result['rows_exported']}[/] rows"
        f" · active [bold]{result['active_jobs']}[/]"
    )


@app.command()
def deduplicate(
    window_days: Annotated[int, typer.Option(help="How far back to compare.")] = 30,
    apply: Annotated[bool, typer.Option("--apply", help="Persist the verdicts.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Re-check stored postings for duplicates that slipped through."""
    settings = _settings()

    async def body() -> dict[str, Any]:
        from app.core.timeutils import days_ago
        from app.processing.deduplication.engine import DeduplicationEngine
        from app.storage.repositories.jobs import JobQuery, JobRepository

        database = _database(settings)
        engine = DeduplicationEngine(settings.deduplication)
        found: list[dict[str, Any]] = []
        try:
            async with database.session() as session:
                repository = JobRepository(session)
                jobs, _total = await repository.search(
                    JobQuery(
                        first_seen_after=days_ago(window_days), limit=2000, sort="first_seen_at"
                    )
                )
                for job in jobs:
                    engine.fingerprint(job)
                    verdict = engine.check(job)
                    if verdict.is_duplicate:
                        found.append(
                            {
                                "job_id": job.id,
                                "title": job.title,
                                "duplicate_of": verdict.duplicate_of,
                                "kind": str(verdict.kind),
                                "similarity": verdict.similarity,
                            }
                        )
                        if apply and verdict.duplicate_of:
                            await repository.mark_duplicate(
                                job.id, duplicate_of=verdict.duplicate_of, kind=verdict.kind
                            )
                    else:
                        engine.remember_job(job)
                return {"scanned": len(jobs), "duplicates": found, "applied": apply}
        finally:
            await database.dispose()

    result = run_async(body)
    if emit(result, as_json=as_json):
        return
    console.print(
        f"scanned [bold]{result['scanned']}[/] postings · "
        f"found [bold]{len(result['duplicates'])}[/] duplicates"
        + (" (persisted)" if result["applied"] else " (dry run, use --apply)")
    )
    grid = table("Duplicates", ["Job", "Title", "Duplicate of", "Kind", "Similarity"])
    for row in result["duplicates"][:25]:
        grid.add_row(
            row["job_id"][:10],
            row["title"][:48],
            str(row["duplicate_of"])[:10],
            row["kind"],
            f"{row['similarity']:.3f}",
        )
    if result["duplicates"]:
        console.print(grid)


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
@app.command()
def analyze(
    skill: Annotated[str, typer.Option(help="Skill slug, e.g. python.")],
    window_days: Annotated[int, typer.Option()] = 30,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show demand, trend, salary and related skills for one technology."""
    settings = _settings()

    async def body() -> dict[str, Any]:
        from app.services.analytics_service import AnalyticsService

        database = _database(settings)
        try:
            service = AnalyticsService(database, settings=settings)
            view = await service.skill_explorer(
                skill, AnalyticsFilter(window_days=window_days, limit=100)
            )
            if view is None:
                raise JobIntelError(
                    f"no demand data for {skill!r} in the last {window_days} days",
                    code="not_found",
                )
            return view.model_dump(mode="json")
        finally:
            await database.dispose()

    view = run_async(body)
    if emit(view, as_json=as_json):
        return

    demand = view["skill"]
    console.print(
        f"\n[bold]{demand['name']}[/]  ({demand['job_count']} postings, "
        f"{demand['share'] * 100:.1f}% of the market)\n"
    )

    grid = table("Trend", ["Window", "Current", "Previous", "Change", "Direction"])
    for window in view["trend"]["windows"]:
        change = window["change_pct"]
        grid.add_row(
            f"{window['days']}d",
            str(window["current_count"]),
            str(window["previous_count"]),
            "—" if change is None else f"{change:+.1f}%",
            window["direction"],
        )
    console.print(grid)

    related = view.get("related_skills") or []
    if related:
        pairs = table("Related skills", ["Skill", "Co-occurrence", "Lift"])
        for pair in related[:8]:
            other = pair["skill_b"] if pair["skill_a"] == skill else pair["skill_a"]
            pairs.add_row(other, str(pair["cooccurrence_count"]), f"{pair['lift']:.2f}")
        console.print(pairs)

    salary = view.get("salary") or {}
    if salary.get("median"):
        console.print(
            f"\nSalary (observed, {salary['sample_size']} postings): "
            f"median [bold]{salary['currency']} {salary['median']:,.0f}[/], "
            f"p25 {salary['p25']:,.0f} · p75 {salary['p75']:,.0f}\n"
        )
    else:
        console.print("\n[dim]Too few disclosed salaries to publish statistics.[/]\n")


@app.command()
def trends(
    days: Annotated[int, typer.Option(help="Look-back window.")] = 30,
    limit: Annotated[int, typer.Option()] = 15,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Rank skills by how fast their demand is changing."""
    settings = _settings()

    async def body() -> list[dict[str, Any]]:
        from app.services.analytics_service import AnalyticsService

        database = _database(settings)
        try:
            service = AnalyticsService(database, settings=settings)
            rows = await service.top_trends(
                AnalyticsFilter(window_days=days, limit=limit * 2), limit=limit
            )
            return [row.model_dump(mode="json") for row in rows]
        finally:
            await database.dispose()

    rows = run_async(body)
    if emit(rows, as_json=as_json):
        return
    grid = table(
        f"Skill trends ({days} days)", ["Skill", "Jobs", "Change", "Direction", "Confidence"]
    )
    for row in rows:
        window = next((w for w in row["windows"] if w["days"] == days), None) or (
            row["windows"][0] if row["windows"] else None
        )
        change = window["change_pct"] if window else None
        grid.add_row(
            row["name"],
            str(window["current_count"] if window else 0),
            "—" if change is None else f"{change:+.1f}%",
            row["direction"],
            f"{row['confidence']:.2f}",
        )
    console.print(grid)


@app.command()
def emerging(
    days: Annotated[int, typer.Option()] = 30,
    limit: Annotated[int, typer.Option()] = 10,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Detect technologies growing sharply from a small base."""
    settings = _settings()

    async def body() -> list[dict[str, Any]]:
        from app.services.analytics_service import AnalyticsService

        database = _database(settings)
        try:
            service = AnalyticsService(database, settings=settings)
            rows = await service.emerging(window_days=days, limit=limit)
            return [row.model_dump(mode="json") for row in rows]
        finally:
            await database.dispose()

    rows = run_async(body)
    if emit(rows, as_json=as_json):
        return
    if not rows:
        console.print("[dim]No technology cleared the emergence thresholds.[/]")
        return
    grid = table("Emerging technologies", ["Skill", "Now", "Before", "Growth", "z", "Confidence"])
    for row in rows:
        grid.add_row(
            row["name"],
            str(row["current_count"]),
            str(row["previous_count"]),
            "new" if row["growth_pct"] is None else f"{row['growth_pct']:+.0f}%",
            "—" if row["z_score"] is None else f"{row['z_score']:.2f}",
            f"{row['confidence']:.2f}",
        )
    console.print(grid)


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Free-text query.")],
    limit: Annotated[int, typer.Option()] = 10,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Search stored postings."""
    settings = _settings()

    async def body() -> dict[str, Any]:
        from app.search.engine import SearchRequest
        from app.services.job_service import JobService

        database = _database(settings)
        try:
            service = JobService(database, settings=settings)
            result = await service.search(SearchRequest(query=query, limit=limit))
            return {
                "total": result.total,
                "backend": result.backend,
                "took_ms": result.took_ms,
                "items": [
                    {
                        "id": job.id,
                        "title": job.title,
                        "company": job.company_name,
                        "location": job.location.display(),
                        "remote": str(job.remote_type),
                        "skills": job.skill_slugs[:6],
                    }
                    for job in result.jobs
                ],
            }
        finally:
            await database.dispose()

    result = run_async(body)
    if emit(result, as_json=as_json):
        return
    console.print(
        f"{result['total']} matches via {result['backend']} in {result['took_ms']:.1f} ms"
    )
    grid = table("Results", ["Title", "Company", "Location", "Remote", "Skills"])
    for item in result["items"]:
        grid.add_row(
            item["title"][:44],
            item["company"][:24],
            item["location"][:24],
            item["remote"],
            ", ".join(item["skills"]),
        )
    console.print(grid)


@app.command()
def fit(
    role: Annotated[str, typer.Option(help="Target role, e.g. 'Python Developer'.")],
    skills: Annotated[str, typer.Option(help="Comma-separated skill slugs.")] = "",
    window_days: Annotated[int, typer.Option()] = 30,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Score a candidate profile against current market demand."""
    settings = _settings()
    wanted = [item.strip() for item in skills.split(",") if item.strip()]

    async def body() -> dict[str, Any]:
        from app.analytics.recommendations.market_fit import profile_from_inputs
        from app.services.profile_service import ProfileService

        database = _database(settings)
        try:
            service = ProfileService(database, settings=settings)
            profile = profile_from_inputs(target_role=role, skills=wanted)
            result = await service.market_fit(profile, window_days=window_days)
            return result.model_dump(mode="json")
        finally:
            await database.dispose()

    result = run_async(body)
    if emit(result, as_json=as_json):
        return
    console.print(f"\n[bold]{result['target_role']}[/]")
    console.print(
        f"market fit [bold]{result['market_fit_score'] * 100:.0f}%[/] · "
        f"skill coverage [bold]{result['skill_coverage'] * 100:.0f}%[/] · "
        f"{result['matching_jobs']} matching postings\n"
    )
    if result["missing_skills"]:
        grid = table("High-value missing skills", ["Skill", "Postings", "Share"])
        for item in result["missing_skills"][:8]:
            grid.add_row(item["name"], str(item["job_count"]), f"{item['share'] * 100:.1f}%")
        console.print(grid)
    for note in result["notes"]:
        console.print(f"[dim]{note}[/]")


@app.command()
def quality(
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Report data-quality metrics per source."""
    settings = _settings()

    async def body() -> dict[str, Any]:
        from app.services.admin_service import AdminService

        database = _database(settings)
        try:
            return await AdminService(database, settings=settings).statistics()
        finally:
            await database.dispose()

    stats = run_async(body)
    if emit(stats, as_json=as_json):
        return

    ingestion = stats["ingestion"]
    console.print(
        f"\nlast 7 days: [bold]{int(ingestion['runs'])}[/] runs · "
        f"{int(ingestion['records_received'])} records · "
        f"{int(ingestion['records_rejected'])} rejected · "
        f"average quality [bold]{ingestion['average_quality']:.3f}[/]\n"
    )

    grid = table(
        "Sources", ["Source", "Enabled", "Processed", "Failed", "Failures", "Last success"]
    )
    for source in stats["sources"]:
        grid.add_row(
            source["source"],
            "yes" if source["enabled"] else "no",
            str(source["records_processed"]),
            str(source["records_failed"]),
            str(source["consecutive_failures"]),
            (source["last_successful_run_at"] or "never")[:19],
        )
    console.print(grid)

    if stats["rejections"]:
        rejects = table("Rejections (7 days)", ["Reason", "Count"])
        for reason, count in sorted(stats["rejections"].items(), key=lambda i: -i[1]):
            rejects.add_row(reason, str(count))
        console.print(rejects)


@app.command()
def report(
    window_days: Annotated[int, typer.Option()] = 30,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Print a full market report."""
    settings = _settings()

    async def body() -> dict[str, Any]:
        from app.services.analytics_service import AnalyticsService

        database = _database(settings)
        flt = AnalyticsFilter(window_days=window_days, limit=15)
        try:
            service = AnalyticsService(database, settings=settings)
            return {
                "overview": (await service.overview(flt)).model_dump(mode="json"),
                "skills": [s.model_dump(mode="json") for s in await service.skills(flt)],
                "locations": [
                    row.model_dump(mode="json") for row in await service.locations(flt, limit=10)
                ],
                "remote": (await service.remote(flt)).model_dump(mode="json"),
                "seniority": (await service.seniority(flt)).model_dump(mode="json"),
                "salaries": (await service.salaries(flt)).model_dump(mode="json"),
                "companies": [
                    row.model_dump(mode="json") for row in await service.companies(flt, limit=10)
                ],
            }
        finally:
            await database.dispose()

    data = run_async(body)
    if emit(data, as_json=as_json):
        return

    overview = data["overview"]
    console.print(f"\n[bold]Market report — last {window_days} days[/]")
    console.print(
        f"active [bold]{overview['active_jobs']}[/] · new today {overview['new_jobs_today']} · "
        f"this week {overview['new_jobs_this_week']} · "
        f"{overview['companies_hiring']} companies in {overview['countries_covered']} countries · "
        f"remote {overview['remote_share'] * 100:.1f}%\n"
    )

    skills = table("Top skills", ["Skill", "Jobs", "Share"])
    for row in data["skills"][:10]:
        skills.add_row(row["name"], str(row["job_count"]), f"{row['share'] * 100:.1f}%")
    console.print(skills)

    places = table("Top locations", ["Country", "Jobs", "Remote"])
    for row in data["locations"]:
        places.add_row(
            row["country"] or row["country_code"] or "Unknown",
            str(row["job_count"]),
            f"{row['remote_share'] * 100:.0f}%",
        )
    console.print(places)

    companies = table("Top employers", ["Company", "Jobs", "Growth", "Remote"])
    for row in data["companies"]:
        growth = row["hiring_growth_pct"]
        companies.add_row(
            row["name"],
            str(row["active_job_count"]),
            "—" if growth is None else f"{growth:+.0f}%",
            f"{row['remote_share'] * 100:.0f}%",
        )
    console.print(companies)

    salary = data["salaries"]
    if salary.get("median"):
        console.print(
            f"\nsalary (observed, n={salary['sample_size']}): "
            f"median {salary['currency']} {salary['median']:,.0f} · "
            f"p25 {salary['p25']:,.0f} · p75 {salary['p75']:,.0f}\n"
        )
    else:
        console.print("\n[dim]Too few disclosed salaries to publish statistics.[/]\n")


# --------------------------------------------------------------------------- #
# Data generation
# --------------------------------------------------------------------------- #
@app.command()
def generate(
    count: Annotated[int, typer.Option(help="How many postings to generate.")] = 10_000,
    days: Annotated[int, typer.Option(help="Period the postings span.")] = 180,
    seed: Annotated[int, typer.Option()] = 20240101,
    output: Annotated[Path, typer.Option(help="Destination .jsonl file.")] = Path(
        "data/raw/jobs.jsonl"
    ),
) -> None:
    """Generate a synthetic dataset with known market trends."""
    from app.ingestion.sources.synthetic_source import SyntheticConfig, SyntheticJobGenerator

    settings = _settings()
    destination = output if output.is_absolute() else settings.storage.data_dir.parent / output
    destination.parent.mkdir(parents=True, exist_ok=True)

    generator = SyntheticJobGenerator(SyntheticConfig(count=count, days=days, seed=seed))
    written = 0
    with destination.open("w", encoding="utf-8") as handle:
        for chunk_start in range(0, count, 5000):
            size = min(5000, count - chunk_start)
            for job in generator.generate(size):
                handle.write(job.model_dump_json() + "\n")
                written += 1
    console.print(f"wrote [bold]{written}[/] postings to {destination}")


# --------------------------------------------------------------------------- #
# Services
# --------------------------------------------------------------------------- #
@app.command()
def serve(
    host: Annotated[str | None, typer.Option()] = None,
    port: Annotated[int | None, typer.Option()] = None,
    reload: Annotated[bool, typer.Option("--reload", help="Auto-reload on changes.")] = False,
) -> None:
    """Run the HTTP API."""
    import uvicorn

    settings = _settings()
    uvicorn.run(
        "app.api.app:create_app",
        factory=True,
        host=host or settings.api.host,
        port=port or settings.api.port,
        reload=reload,
        log_config=None,
    )


@app.command()
def worker() -> None:
    """Run the scheduler and the event-processing worker."""
    from app.workers.runner import run_worker

    settings = _settings()
    run_async(lambda: run_worker(settings))


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
@db_app.command("upgrade")
def db_upgrade(revision: Annotated[str, typer.Argument()] = "head") -> None:
    """Apply migrations."""
    from alembic import command
    from alembic.config import Config

    _settings()
    command.upgrade(Config("alembic.ini"), revision)
    console.print(f"database upgraded to [bold]{revision}[/]")


@db_app.command("downgrade")
def db_downgrade(revision: Annotated[str, typer.Argument()] = "-1") -> None:
    """Roll migrations back."""
    from alembic import command
    from alembic.config import Config

    _settings()
    command.downgrade(Config("alembic.ini"), revision)
    console.print(f"database downgraded to [bold]{revision}[/]")


@db_app.command("current")
def db_current() -> None:
    """Show the applied revision."""
    from alembic import command
    from alembic.config import Config

    _settings()
    command.current(Config("alembic.ini"), verbose=True)


@db_app.command("init")
def db_init() -> None:
    """Create the schema directly (development shortcut for migrations)."""
    settings = _settings()

    async def body() -> None:
        database = _database(settings)
        try:
            await database.create_all()
        finally:
            await database.dispose()

    run_async(body)
    console.print("schema created")


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #
@keys_app.command("create")
def keys_create(
    name: Annotated[str, typer.Option(help="Human-readable label.")],
    role: Annotated[UserRole, typer.Option(help="viewer | analyst | admin.")] = UserRole.VIEWER,
) -> None:
    """Issue an API key. The plaintext is shown exactly once."""
    settings = _settings()

    async def body() -> dict[str, str]:
        from app.services.admin_service import AdminService

        database = _database(settings)
        try:
            generated = await AdminService(database, settings=settings).issue_api_key(
                name=name, role=role
            )
            return {"key_id": generated.key_id, "api_key": generated.full_key}
        finally:
            await database.dispose()

    created = run_async(body)
    console.print(f"\n[bold green]API key created[/] ({role})")
    console.print(f"  key id : {created['key_id']}")
    console.print(f"  key    : [bold]{created['api_key']}[/]")
    console.print("\n[yellow]Store it now - it cannot be retrieved again.[/]\n")


@keys_app.command("list")
def keys_list(include_inactive: Annotated[bool, typer.Option("--all")] = False) -> None:
    """List issued credentials (metadata only)."""
    settings = _settings()

    async def body() -> list[dict[str, Any]]:
        from app.services.admin_service import AdminService

        database = _database(settings)
        try:
            return await AdminService(database, settings=settings).list_api_keys(
                include_inactive=include_inactive
            )
        finally:
            await database.dispose()

    rows = run_async(body)
    grid = table("API keys", ["Key id", "Name", "Role", "Active", "Created", "Last used"])
    for row in rows:
        grid.add_row(
            row["key_id"],
            row["name"],
            row["role"],
            "yes" if row["active"] else "no",
            row["created_at"][:19],
            (row["last_used_at"] or "never")[:19],
        )
    console.print(grid)


@keys_app.command("revoke")
def keys_revoke(key_id: Annotated[str, typer.Argument()]) -> None:
    """Revoke a credential."""
    settings = _settings()

    async def body() -> bool:
        from app.services.admin_service import AdminService

        database = _database(settings)
        try:
            return await AdminService(database, settings=settings).revoke_api_key(key_id)
        finally:
            await database.dispose()

    if run_async(body):
        console.print(f"revoked [bold]{key_id}[/]")
    else:
        console.print(f"[red]no active key with id {key_id}[/]")
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #
@sources_app.command("list")
def sources_list() -> None:
    """List configured sources and their ingestion state."""
    settings = _settings()

    async def body() -> list[dict[str, Any]]:
        from app.ingestion.registry import SourceRegistry
        from app.storage.repositories.ingestion import SourceStateRepository

        registry = SourceRegistry.from_file(settings=settings)
        database = _database(settings)
        try:
            async with database.read_session() as session:
                states = {s.source: s for s in await SourceStateRepository(session).list_all()}
            rows = []
            for definition in registry.definitions():
                state = states.get(definition.name)
                rows.append(
                    {
                        "name": definition.name,
                        "kind": str(definition.kind),
                        "configured": definition.enabled,
                        "processed": state.records_processed if state else 0,
                        "failed": state.records_failed if state else 0,
                        "last_run": (
                            state.last_successful_run_at.isoformat()
                            if state and state.last_successful_run_at
                            else "never"
                        ),
                    }
                )
            return rows
        finally:
            await registry.aclose()
            await database.dispose()

    rows = run_async(body)
    grid = table("Sources", ["Name", "Kind", "Enabled", "Processed", "Failed", "Last run"])
    for row in rows:
        grid.add_row(
            row["name"],
            row["kind"],
            "yes" if row["configured"] else "no",
            str(row["processed"]),
            str(row["failed"]),
            row["last_run"][:19],
        )
    console.print(grid)


@sources_app.command("enable")
def sources_enable(name: Annotated[str, typer.Argument()]) -> None:
    """Enable a source."""
    _toggle_source(name, enabled=True)


@sources_app.command("disable")
def sources_disable(name: Annotated[str, typer.Argument()]) -> None:
    """Disable a source."""
    _toggle_source(name, enabled=False)


def _toggle_source(name: str, *, enabled: bool) -> None:
    settings = _settings()

    async def body() -> bool:
        from app.storage.repositories.ingestion import SourceStateRepository

        database = _database(settings)
        try:
            async with database.session() as session:
                return await SourceStateRepository(session).set_enabled(name, enabled)
        finally:
            await database.dispose()

    if run_async(body):
        console.print(f"source [bold]{name}[/] {'enabled' if enabled else 'disabled'}")
    else:
        console.print(f"[red]unknown source {name}[/]")
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #
@app.command()
def taxonomy(
    sync: Annotated[bool, typer.Option("--sync", help="Write it into the database.")] = False,
) -> None:
    """Show (and optionally persist) the configured skill taxonomy."""
    from app.nlp.skills.taxonomy import get_taxonomy

    settings = _settings()
    tree = get_taxonomy(str(settings.nlp.taxonomy_path))
    console.print(
        f"taxonomy v{tree.version}: [bold]{len(tree.skills)}[/] skills in "
        f"[bold]{len(tree.groups)}[/] groups"
    )
    grid = table("Categories", ["Category", "Skills"])
    counts: dict[str, int] = {}
    for node in tree.skills.values():
        counts[str(node.category)] = counts.get(str(node.category), 0) + 1
    for category, count in sorted(counts.items(), key=lambda item: -item[1]):
        grid.add_row(category, str(count))
    console.print(grid)

    if sync:

        async def body() -> int:
            from app.services.admin_service import AdminService

            database = _database(settings)
            try:
                return await AdminService(database, settings=settings).sync_taxonomy(tree)
            finally:
                await database.dispose()

        console.print(f"synced [bold]{run_async(body)}[/] nodes into the database")


@app.command()
def version() -> None:
    """Print the platform version and effective configuration summary."""
    settings = get_settings()
    console.print(f"jobintel [bold]{__version__}[/]")
    console.print(f"  environment : {settings.environment}")
    console.print(f"  database    : {settings.database.url.split('://', 1)[0]}")
    console.print(f"  cache       : {'redis' if settings.cache.url else 'in-memory'}")
    console.print(f"  event bus   : {settings.events.backend}")


@app.command()
def scopes() -> None:
    """List the permission scopes an API key can carry."""
    grid = table("Scopes", ["Scope", "Description"])
    descriptions = {
        Scope.JOBS_READ: "read postings and run searches",
        Scope.ANALYTICS_READ: "read market analytics",
        Scope.ALERTS_READ: "read alert rules and notifications",
        Scope.ALERTS_WRITE: "create, update and evaluate alert rules",
        Scope.PROFILES_WRITE: "manage candidate profiles and market-fit analysis",
        Scope.INGESTION_WRITE: "trigger ingestion runs",
        Scope.ADMIN: "everything, including credential management",
    }
    for scope, description in descriptions.items():
        grid.add_row(scope.value, description)
    console.print(grid)


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
