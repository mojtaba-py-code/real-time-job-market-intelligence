"""Columnar analytical storage.

PostgreSQL is the operational store: it answers "show me this posting" and
"which jobs match these filters" in milliseconds. It is the wrong tool for
"scan four years of postings and group by skill and month".

So the platform keeps a second, append-only copy in Parquet, partitioned by
``year/month/day``, and queries it with DuckDB. Partition pruning means a
90-day question reads 90 small files instead of the whole corpus, and the
columnar layout means a query touching three columns reads three columns.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from app.core.config import StorageSettings
from app.core.errors import StorageError
from app.core.logging import get_logger
from app.core.timeutils import to_date, utcnow
from app.models.job import NormalizedJob

log = get_logger(__name__)

DATASET_NAME = "jobs"

#: Explicit schema: letting Arrow infer types from Python objects produces a
#: different schema whenever a batch happens to contain only nulls.
JOB_SCHEMA = pa.schema(
    [
        pa.field("id", pa.string()),
        pa.field("source", pa.string()),
        pa.field("source_job_id", pa.string()),
        pa.field("title", pa.string()),
        pa.field("normalized_title", pa.string()),
        pa.field("title_family", pa.string()),
        pa.field("specialization", pa.string()),
        pa.field("company_name", pa.string()),
        pa.field("company_slug", pa.string()),
        pa.field("industry", pa.string()),
        pa.field("segment", pa.string()),
        pa.field("country_code", pa.string()),
        pa.field("country", pa.string()),
        pa.field("region", pa.string()),
        pa.field("city", pa.string()),
        pa.field("remote_type", pa.string()),
        pa.field("employment_type", pa.string()),
        pa.field("experience_level", pa.string()),
        pa.field("experience_years_min", pa.float64()),
        pa.field("experience_years_max", pa.float64()),
        pa.field("salary_min", pa.float64()),
        pa.field("salary_max", pa.float64()),
        pa.field("salary_currency", pa.string()),
        pa.field("salary_period", pa.string()),
        pa.field("salary_provenance", pa.string()),
        pa.field("salary_annual_min", pa.float64()),
        pa.field("salary_annual_max", pa.float64()),
        pa.field("skills", pa.list_(pa.string())),
        pa.field("skill_count", pa.int32()),
        pa.field("language", pa.string()),
        pa.field("quality_score", pa.float64()),
        pa.field("status", pa.string()),
        pa.field("published_at", pa.timestamp("us", tz="UTC")),
        pa.field("first_seen_at", pa.timestamp("us", tz="UTC")),
        pa.field("year", pa.int32()),
        pa.field("month", pa.int32()),
        pa.field("day", pa.int32()),
    ]
)

PARTITION_COLUMNS = ("year", "month", "day")

# The analytical queries live here as named templates. The only thing ever
# interpolated into them is a constant clause chosen in code; every value is
# bound as a parameter.
_VIEW_SQL = "CREATE VIEW jobs AS SELECT * FROM read_parquet('{path}', hive_partitioning=true)"

_DATE_CLAUSE = "WHERE make_date(year, month, day) >= ?"

_DAILY_VOLUME_SQL = """
    SELECT make_date(year, month, day) AS day, COUNT(*) AS jobs
    FROM jobs
    {clause}
    GROUP BY 1
    ORDER BY 1
"""

_SKILL_DEMAND_SQL = """
    SELECT skill, COUNT(*) AS jobs
    FROM (SELECT UNNEST(skills) AS skill FROM jobs {clause})
    GROUP BY skill
    ORDER BY jobs DESC
    LIMIT ?
"""

_SALARY_BY_SEGMENT_SQL = """
    SELECT
        segment,
        COUNT(*) AS sample_size,
        MEDIAN((salary_annual_min + COALESCE(salary_annual_max, salary_annual_min)) / 2)
            AS median_salary
    FROM jobs
    WHERE salary_provenance = 'observed' AND salary_annual_min IS NOT NULL
    GROUP BY segment
    HAVING COUNT(*) >= ?
    ORDER BY median_salary DESC
"""

_MONTHLY_SKILL_SQL = """
    SELECT year, month, skill, COUNT(*) AS jobs
    FROM (SELECT year, month, UNNEST(skills) AS skill FROM jobs)
    WHERE skill IN ({placeholders})
    GROUP BY year, month, skill
    ORDER BY year DESC, month DESC
    LIMIT ?
"""


def job_to_row(job: NormalizedJob) -> dict[str, Any]:
    """Flatten a posting into the analytical schema."""
    event_date = to_date(job.published_at or job.first_seen_at)
    return {
        "id": job.id,
        "source": job.source,
        "source_job_id": job.source_job_id,
        "title": job.title,
        "normalized_title": job.normalized_title,
        "title_family": job.title_family,
        "specialization": job.specialization,
        "company_name": job.company_name,
        "company_slug": job.company_slug,
        "industry": job.industry,
        "segment": str(job.segment),
        "country_code": job.location.country_code,
        "country": job.location.country,
        "region": job.location.region,
        "city": job.location.city,
        "remote_type": str(job.remote_type),
        "employment_type": str(job.employment_type),
        "experience_level": str(job.experience_level),
        "experience_years_min": job.experience_years_min,
        "experience_years_max": job.experience_years_max,
        "salary_min": job.salary.min_amount,
        "salary_max": job.salary.max_amount,
        "salary_currency": job.salary.normalized_currency or job.salary.currency,
        "salary_period": str(job.salary.period),
        "salary_provenance": str(job.salary.provenance),
        "salary_annual_min": job.salary.annual_min,
        "salary_annual_max": job.salary.annual_max,
        "skills": sorted({s.slug for s in job.skills}),
        "skill_count": len(job.skills),
        "language": job.language,
        "quality_score": job.quality_score,
        "status": str(job.status),
        "published_at": job.published_at,
        "first_seen_at": job.first_seen_at,
        "year": event_date.year,
        "month": event_date.month,
        "day": event_date.day,
    }


@dataclass(frozen=True, slots=True)
class ExportResult:
    """What one export wrote."""

    rows: int
    files: int
    dataset_path: Path
    duration_ms: float = 0.0


class ParquetExporter:
    """Appends postings to the partitioned Parquet dataset."""

    def __init__(self, settings: StorageSettings) -> None:
        self._settings = settings
        self._root = settings.analytics_dir / DATASET_NAME

    @property
    def dataset_path(self) -> Path:
        return self._root

    def export(self, jobs: Sequence[NormalizedJob]) -> ExportResult:
        """Write a batch, creating one file per partition."""
        import time

        started = time.perf_counter()
        if not jobs:
            return ExportResult(rows=0, files=0, dataset_path=self._root)

        rows = [job_to_row(job) for job in jobs]
        table = pa.Table.from_pylist(rows, schema=JOB_SCHEMA)
        self._root.mkdir(parents=True, exist_ok=True)

        compression = (
            self._settings.parquet_compression
            if self._settings.parquet_compression != "none"
            else None
        )
        stamp = utcnow().strftime("%Y%m%dT%H%M%S%f")
        try:
            pq.write_to_dataset(
                table,
                root_path=str(self._root),
                partition_cols=list(PARTITION_COLUMNS),
                compression=compression,
                row_group_size=self._settings.parquet_row_group_size,
                basename_template=f"part-{stamp}-{{i}}.parquet",
                existing_data_behavior="overwrite_or_ignore",
            )
        except Exception as exc:
            raise StorageError(f"parquet export failed: {exc}") from exc

        files = sum(1 for _ in self._root.rglob("*.parquet"))
        duration = (time.perf_counter() - started) * 1000
        log.info(
            "parquet.exported",
            rows=len(rows),
            files=files,
            path=str(self._root),
            duration_ms=round(duration, 2),
        )
        return ExportResult(
            rows=len(rows), files=files, dataset_path=self._root, duration_ms=duration
        )

    def partitions(self) -> list[tuple[int, int, int]]:
        """Every ``(year, month, day)`` partition currently on disk."""
        found: set[tuple[int, int, int]] = set()
        if not self._root.exists():
            return []
        for path in self._root.rglob("*.parquet"):
            parts = {
                piece.split("=", 1)[0]: piece.split("=", 1)[1]
                for piece in path.relative_to(self._root).parts
                if "=" in piece
            }
            try:
                found.add((int(parts["year"]), int(parts["month"]), int(parts["day"])))
            except (KeyError, ValueError):
                continue
        return sorted(found)

    def row_count(self) -> int:
        """Total number of rows in the dataset."""
        if not self._root.exists():
            return 0
        return sum(pq.ParquetFile(path).metadata.num_rows for path in self._root.rglob("*.parquet"))


class AnalyticalStore:
    """Runs SQL over the Parquet dataset with DuckDB."""

    def __init__(self, settings: StorageSettings) -> None:
        self._settings = settings
        self._root = settings.analytics_dir / DATASET_NAME

    @property
    def glob(self) -> str:
        """Hive-partitioned glob pattern DuckDB reads (POSIX separators)."""
        return (self._root / "**" / "*.parquet").as_posix()

    def available(self) -> bool:
        return self._root.exists() and any(self._root.rglob("*.parquet"))

    def query(self, sql: str, parameters: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        """Execute a read-only query against the dataset.

        ``sql`` must reference the dataset as ``jobs``; the view is created for
        the duration of the call, so callers cannot reach anything else.
        """
        import duckdb

        if not self.available():
            return []
        connection = duckdb.connect(database=":memory:")
        try:
            # DuckDB cannot bind a parameter inside CREATE VIEW, so the path
            # is inlined. It comes from configuration, never from a request,
            # and single quotes are escaped defensively.
            dataset = self.glob.replace("'", "''")
            connection.execute(_VIEW_SQL.format(path=dataset))  # nosec B608
            cursor = connection.execute(sql, list(parameters or []))
            columns = [description[0] for description in cursor.description or []]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
        except Exception as exc:
            raise StorageError(f"analytical query failed: {exc}") from exc
        finally:
            connection.close()

    # ------------------------------------------------------------------ #
    # Prepared analytical questions
    # ------------------------------------------------------------------ #
    def daily_volume(self, *, since: date | None = None) -> list[dict[str, Any]]:
        """Postings per day, straight from the columnar store."""
        clause = _DATE_CLAUSE if since else ""
        params: list[Any] = [since] if since else []
        return self.query(_DAILY_VOLUME_SQL.format(clause=clause), params)  # nosec B608

    def skill_demand(self, *, limit: int = 25, since: date | None = None) -> list[dict[str, Any]]:
        """Skill frequency, unnesting the list column."""
        clause = _DATE_CLAUSE if since else ""
        params: list[Any] = [since] if since else []
        params.append(limit)
        return self.query(_SKILL_DEMAND_SQL.format(clause=clause), params)  # nosec B608

    def salary_by_segment(self, *, min_sample: int = 5) -> list[dict[str, Any]]:
        """Median annual salary per market segment."""
        return self.query(_SALARY_BY_SEGMENT_SQL, [min_sample])

    def monthly_skill_matrix(
        self, skills: Sequence[str], *, months: int = 12
    ) -> list[dict[str, Any]]:
        """Monthly demand for a set of skills - the trend engine's bulk view."""
        if not skills:
            return []
        placeholders = ", ".join("?" for _ in skills)
        sql = _MONTHLY_SKILL_SQL.format(placeholders=placeholders)  # nosec B608
        return self.query(sql, [*skills, months * max(len(skills), 1)])

    def describe(self) -> dict[str, Any]:
        """Dataset statistics, used by the CLI and the health endpoint."""
        if not self.available():
            return {"available": False, "rows": 0}
        rows = self.query("SELECT COUNT(*) AS rows FROM jobs")
        span = self.query(
            "SELECT MIN(make_date(year, month, day)) AS first_day, "
            "MAX(make_date(year, month, day)) AS last_day FROM jobs"
        )
        return {
            "available": True,
            "rows": rows[0]["rows"] if rows else 0,
            "first_day": span[0]["first_day"] if span else None,
            "last_day": span[0]["last_day"] if span else None,
            "path": str(self._root),
        }


def latest_export_timestamp(root: Path) -> datetime | None:
    """Modification time of the newest Parquet file, or ``None``."""
    files = list(root.rglob("*.parquet")) if root.exists() else []
    if not files:
        return None
    newest = max(files, key=lambda path: path.stat().st_mtime)
    return datetime.fromtimestamp(newest.stat().st_mtime, tz=utcnow().tzinfo)


__all__ = [
    "DATASET_NAME",
    "JOB_SCHEMA",
    "PARTITION_COLUMNS",
    "AnalyticalStore",
    "ExportResult",
    "ParquetExporter",
    "job_to_row",
    "latest_export_timestamp",
]
