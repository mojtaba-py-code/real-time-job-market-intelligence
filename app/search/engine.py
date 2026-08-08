"""Job search.

Search starts where the data already is: PostgreSQL. A dedicated
``search_text`` column is maintained at write time, full-text ranking is used
when the dialect supports it, and a portable ``LIKE`` path keeps SQLite
deployments working.

The whole thing sits behind :class:`SearchBackend` so that an Elasticsearch or
OpenSearch implementation can be added later by writing one class and changing
one line of wiring - no caller changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import func, select, text

from app.core.logging import get_logger
from app.core.text import canonical_key
from app.core.timeutils import days_ago
from app.models.job import NormalizedJob
from app.storage.models import Job, JobSkill
from app.storage.repositories.jobs import JobQuery, JobRepository, SortField

log = get_logger(__name__)

MAX_QUERY_LENGTH = 200
MAX_TERMS = 12


@dataclass(slots=True)
class SearchRequest:
    """A user-facing search request."""

    query: str | None = None
    skills: tuple[str, ...] = ()
    require_all_skills: bool = True
    company: str | None = None
    country_code: str | None = None
    city: str | None = None
    remote_types: tuple[str, ...] = ()
    employment_types: tuple[str, ...] = ()
    experience_levels: tuple[str, ...] = ()
    segments: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    salary_min: float | None = None
    salary_max: float | None = None
    has_salary: bool | None = None
    posted_within_days: int | None = None
    published_after: datetime | None = None
    published_before: datetime | None = None
    sort: SortField = "relevance"
    descending: bool = True
    limit: int = 25
    offset: int = 0

    def sanitized_query(self) -> str | None:
        """Trim, bound and normalise the free-text part of the request."""
        if not self.query:
            return None
        cleaned = self.query.strip()[:MAX_QUERY_LENGTH]
        return cleaned or None

    def to_job_query(self) -> JobQuery:
        """Translate into the repository's filter object."""
        return JobQuery(
            text=self.sanitized_query(),
            skills=self.skills,
            require_all_skills=self.require_all_skills,
            company_name=self.company,
            country_code=self.country_code,
            city=self.city,
            remote_types=self.remote_types,
            employment_types=self.employment_types,
            experience_levels=self.experience_levels,
            segments=self.segments,
            sources=self.sources,
            salary_min=self.salary_min,
            salary_max=self.salary_max,
            has_salary=self.has_salary,
            published_after=self.published_after,
            published_before=self.published_before,
            first_seen_after=(
                days_ago(self.posted_within_days) if self.posted_within_days else None
            ),
            sort=self.sort if self.sort != "relevance" else "published_at",
            descending=self.descending,
            limit=self.limit,
            offset=self.offset,
        )


@dataclass(slots=True)
class SearchResponse:
    """A page of search results."""

    jobs: list[NormalizedJob] = field(default_factory=list)
    total: int = 0
    limit: int = 25
    offset: int = 0
    backend: str = "sql"
    took_ms: float = 0.0
    scores: dict[str, float] = field(default_factory=dict)

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.jobs) < self.total


@runtime_checkable
class SearchBackend(Protocol):
    """What every search implementation must provide."""

    name: str

    async def search(self, request: SearchRequest) -> SearchResponse: ...

    async def suggest(self, prefix: str, *, limit: int = 10) -> list[str]: ...


class SqlSearchBackend:
    """Portable search over the operational database."""

    name = "sql"

    def __init__(self, repository: JobRepository) -> None:
        self._repo = repository

    async def search(self, request: SearchRequest) -> SearchResponse:
        """Filter, sort and page postings."""
        import time

        started = time.perf_counter()
        jobs, total = await self._repo.search(request.to_job_query())
        scores = self._score(jobs, request.sanitized_query())
        if request.sort == "relevance" and scores:
            jobs.sort(key=lambda job: scores.get(job.id, 0.0), reverse=True)
        return SearchResponse(
            jobs=jobs,
            total=total,
            limit=request.limit,
            offset=request.offset,
            backend=self.name,
            took_ms=round((time.perf_counter() - started) * 1000, 2),
            scores=scores,
        )

    async def suggest(self, prefix: str, *, limit: int = 10) -> list[str]:
        """Title suggestions for a prefix."""
        needle = canonical_key(prefix)
        if len(needle) < 2:
            return []
        stmt = (
            select(Job.normalized_title, func.count().label("uses"))
            .where(func.lower(Job.normalized_title).like(f"{needle}%"))
            .group_by(Job.normalized_title)
            .order_by(func.count().desc())
            .limit(limit)
        )
        rows = (await self._repo.session.execute(stmt)).all()
        return [row[0] for row in rows]

    @staticmethod
    def _score(jobs: list[NormalizedJob], query: str | None) -> dict[str, float]:
        """A transparent relevance score.

        Term hits in the title count for more than hits in the body, recent
        postings edge out older ones, and higher-quality records break ties.
        No hidden magic - the ranking can be explained to a user.
        """
        if not query:
            return {}
        terms = [term for term in canonical_key(query).split() if term][:MAX_TERMS]
        if not terms:
            return {}

        scores: dict[str, float] = {}
        for job in jobs:
            title = canonical_key(job.title)
            company = canonical_key(job.company_name)
            body = canonical_key(job.description_snippet)
            skills = {skill.slug for skill in job.skills}

            score = 0.0
            for term in terms:
                if term in title:
                    score += 3.0
                if term in skills:
                    score += 2.5
                if term in company:
                    score += 1.5
                if term in body:
                    score += 1.0
            score += job.quality_score
            scores[job.id] = round(score, 3)
        return scores


class PostgresFullTextSearchBackend(SqlSearchBackend):
    """Adds PostgreSQL full-text ranking on top of the SQL backend."""

    name = "postgres_fts"

    async def search(self, request: SearchRequest) -> SearchResponse:
        query = request.sanitized_query()
        if not query:
            return await super().search(request)

        import time

        started = time.perf_counter()
        job_query = request.to_job_query()
        # Let PostgreSQL do the matching and the ranking, then hydrate the page
        # through the repository so both backends return identical objects.
        ranked = await self._ranked_ids(query, job_query)
        if not ranked:
            return await super().search(request)

        ids = [job_id for job_id, _rank in ranked]
        page_query = JobQuery(
            statuses=job_query.statuses,
            include_duplicates=job_query.include_duplicates,
            limit=len(ids),
        )
        jobs, _total = await self._repo.search(page_query)
        by_id = {job.id: job for job in jobs}
        ordered = [by_id[job_id] for job_id in ids if job_id in by_id]

        return SearchResponse(
            jobs=ordered,
            total=len(ranked),
            limit=request.limit,
            offset=request.offset,
            backend=self.name,
            took_ms=round((time.perf_counter() - started) * 1000, 2),
            scores={job_id: round(float(rank), 4) for job_id, rank in ranked},
        )

    async def _ranked_ids(self, query: str, job_query: JobQuery) -> list[tuple[str, float]]:
        """Rank matching identifiers with ``ts_rank``."""
        statement = text(
            """
            SELECT id,
                   ts_rank(to_tsvector('simple', search_text),
                           plainto_tsquery('simple', :query)) AS rank
            FROM jobs
            WHERE to_tsvector('simple', search_text) @@ plainto_tsquery('simple', :query)
              AND status = ANY(:statuses)
              AND duplicate_of IS NULL
            ORDER BY rank DESC, first_seen_at DESC
            LIMIT :limit OFFSET :offset
            """
        )
        rows = (
            await self._repo.session.execute(
                statement,
                {
                    "query": query,
                    "statuses": list(job_query.statuses),
                    "limit": job_query.limit,
                    "offset": job_query.offset,
                },
            )
        ).all()
        return [(row[0], float(row[1])) for row in rows]


def build_search_backend(repository: JobRepository) -> SearchBackend:
    """Pick the best backend the current database can support."""
    if repository.supports_full_text_search:
        return PostgresFullTextSearchBackend(repository)
    return SqlSearchBackend(repository)


async def facets(repository: JobRepository) -> dict[str, Any]:
    """Aggregate counts used to render search filters."""
    session = repository.session
    result: dict[str, Any] = {}
    for name, column in (
        ("remote_type", Job.remote_type),
        ("employment_type", Job.employment_type),
        ("experience_level", Job.experience_level),
        ("segment", Job.segment),
        ("country_code", Job.country_code),
        ("source", Job.source),
    ):
        stmt = (
            select(column, func.count())
            .where(Job.status.in_(("active", "updated")))
            .where(Job.duplicate_of.is_(None))
            .group_by(column)
            .order_by(func.count().desc())
            .limit(25)
        )
        rows = (await session.execute(stmt)).all()
        result[name] = {str(row[0]): int(row[1]) for row in rows if row[0] is not None}

    skill_stmt = (
        select(JobSkill.skill_slug, func.count(func.distinct(JobSkill.job_id)))
        .join(Job, Job.id == JobSkill.job_id)
        .where(Job.status.in_(("active", "updated")))
        .group_by(JobSkill.skill_slug)
        .order_by(func.count(func.distinct(JobSkill.job_id)).desc())
        .limit(30)
    )
    skill_rows = (await session.execute(skill_stmt)).all()
    result["skills"] = {row[0]: int(row[1]) for row in skill_rows}
    return result


__all__ = [
    "MAX_QUERY_LENGTH",
    "PostgresFullTextSearchBackend",
    "SearchBackend",
    "SearchRequest",
    "SearchResponse",
    "SqlSearchBackend",
    "build_search_backend",
    "facets",
]
