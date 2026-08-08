"""Job search and retrieval."""

from __future__ import annotations

from typing import Any

from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.models.job import NormalizedJob
from app.search.engine import SearchRequest, SearchResponse, build_search_backend, facets
from app.storage.cache import CacheBackend, NullCache, build_key
from app.storage.repositories.jobs import JobQuery, JobRepository
from app.storage.session import Database

log = get_logger(__name__)


class JobService:
    """Everything the API needs to expose postings."""

    def __init__(
        self,
        database: Database,
        *,
        cache: CacheBackend | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._db = database
        self._settings = settings or get_settings()
        # `cache is not None` rather than a truthiness test: an empty
        # cache defines __len__ and would otherwise be discarded here.
        self._cache = cache if cache is not None else NullCache()

    async def get(self, job_id: str) -> NormalizedJob:
        """Fetch one posting, or raise :class:`NotFoundError`."""
        async with self._db.read_session() as session:
            job = await JobRepository(session).get(job_id)
        if job is None:
            raise NotFoundError(f"job {job_id} was not found", details={"job_id": job_id})
        return job

    async def list_jobs(self, query: JobQuery) -> tuple[list[NormalizedJob], int]:
        """Filtered, paginated listing."""
        async with self._db.read_session() as session:
            return await JobRepository(session).search(query)

    async def search(self, request: SearchRequest) -> SearchResponse:
        """Full search, using the best backend the database supports."""
        async with self._db.read_session() as session:
            backend = build_search_backend(JobRepository(session))
            return await backend.search(request)

    async def suggest(self, prefix: str, *, limit: int = 10) -> list[str]:
        """Title auto-completion."""
        async with self._db.read_session() as session:
            backend = build_search_backend(JobRepository(session))
            return await backend.suggest(prefix, limit=limit)

    async def facets(self) -> dict[str, Any]:
        """Counts per filter value, cached because they change slowly."""
        key = build_key(self._settings.cache.namespace, "facets", "jobs")
        cached = await self._cache.get(key)
        if cached is not None:
            return dict(cached)
        async with self._db.read_session() as session:
            values = await facets(JobRepository(session))
        await self._cache.set(key, values, ttl_seconds=self._settings.cache.default_ttl_seconds)
        return values

    async def similar(self, job_id: str, *, limit: int = 5) -> list[NormalizedJob]:
        """Other postings that share the most skills with this one."""
        job = await self.get(job_id)
        slugs = tuple(job.skill_slugs[:6])
        if not slugs:
            return []
        async with self._db.read_session() as session:
            jobs, _total = await JobRepository(session).search(
                JobQuery(
                    skills=slugs,
                    require_all_skills=False,
                    limit=limit + 1,
                    sort="published_at",
                )
            )
        return [item for item in jobs if item.id != job_id][:limit]

    async def count(self, query: JobQuery) -> int:
        async with self._db.read_session() as session:
            return await JobRepository(session).count(query)


__all__ = ["JobService"]
