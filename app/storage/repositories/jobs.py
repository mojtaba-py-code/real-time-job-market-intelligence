"""Persistence for job postings.

This repository is the hot path of the platform, so it is written around
*batches*: resolving identities, inserting and updating happen with a bounded
number of statements no matter how many postings arrive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import Select, and_, delete, func, insert, or_, select, update
from sqlalchemy.orm import selectinload

from app.core.text import canonical_key
from app.core.timeutils import utcnow
from app.models.enums import DuplicateKind, JobStatus
from app.models.job import NormalizedJob
from app.storage.mappers import job_to_domain, job_to_values
from app.storage.models import Job, JobSkill, SalaryRecord
from app.storage.repositories.base import MAX_BIND_PARAMS, BaseRepository, affected_rows

SortField = Literal["published_at", "first_seen_at", "last_seen_at", "salary", "relevance", "title"]

LIVE_STATUSES: tuple[str, ...] = (JobStatus.ACTIVE.value, JobStatus.UPDATED.value)


@dataclass(slots=True)
class JobQuery:
    """Declarative filter over the ``jobs`` table."""

    text: str | None = None
    statuses: tuple[str, ...] = LIVE_STATUSES
    sources: tuple[str, ...] = ()
    company_slug: str | None = None
    company_name: str | None = None
    country_code: str | None = None
    city: str | None = None
    remote_types: tuple[str, ...] = ()
    employment_types: tuple[str, ...] = ()
    experience_levels: tuple[str, ...] = ()
    segments: tuple[str, ...] = ()
    title_contains: str | None = None
    skills: tuple[str, ...] = ()
    require_all_skills: bool = True
    salary_min: float | None = None
    salary_max: float | None = None
    has_salary: bool | None = None
    published_after: datetime | None = None
    published_before: datetime | None = None
    first_seen_after: datetime | None = None
    include_duplicates: bool = False
    sort: SortField = "published_at"
    descending: bool = True
    limit: int = 25
    offset: int = 0


@dataclass(slots=True)
class UpsertResult:
    """Outcome of a batch write."""

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.created) + len(self.updated)


class JobRepository(BaseRepository):
    """Read and write job postings."""

    # ------------------------------------------------------------------ #
    # Identity lookups (used by the deduplication engine)
    # ------------------------------------------------------------------ #
    async def get(self, job_id: str, *, with_skills: bool = True) -> NormalizedJob | None:
        """Fetch one posting as a domain object."""
        stmt = select(Job).where(Job.id == job_id)
        if with_skills:
            stmt = stmt.options(selectinload(Job.skill_links).selectinload(JobSkill.skill))
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        return job_to_domain(row, list(row.skill_links) if with_skills else [])

    async def get_row(self, job_id: str) -> Job | None:
        """Fetch the raw ORM row (internal use)."""
        return await self.session.get(Job, job_id)

    async def find_by_natural_keys(
        self, pairs: list[tuple[str, str]]
    ) -> dict[tuple[str, str], Job]:
        """Map ``(source, source_job_id)`` to existing rows."""
        if not pairs:
            return {}
        found: dict[tuple[str, str], Job] = {}
        for chunk in self._chunked(pairs, MAX_BIND_PARAMS // 2):
            conditions = [
                and_(Job.source == source, Job.source_job_id == sid) for source, sid in chunk
            ]
            result = await self.session.execute(select(Job).where(or_(*conditions)))
            for row in result.scalars():
                found[(row.source, row.source_job_id)] = row
        return found

    async def find_by_url_fingerprints(self, fingerprints: list[str]) -> dict[str, Job]:
        """Map canonical-URL fingerprints to existing rows."""
        values = [f for f in dict.fromkeys(fingerprints) if f]
        if not values:
            return {}
        found: dict[str, Job] = {}
        for chunk in self._chunked(values, MAX_BIND_PARAMS):
            result = await self.session.execute(select(Job).where(Job.url_fingerprint.in_(chunk)))
            for row in result.scalars():
                found.setdefault(row.url_fingerprint or "", row)
        return found

    async def find_by_content_hashes(self, hashes: list[str]) -> dict[str, Job]:
        """Map content fingerprints to existing rows."""
        values = [h for h in dict.fromkeys(hashes) if h]
        if not values:
            return {}
        found: dict[str, Job] = {}
        for chunk in self._chunked(values, MAX_BIND_PARAMS):
            result = await self.session.execute(select(Job).where(Job.content_hash.in_(chunk)))
            for row in result.scalars():
                found.setdefault(row.content_hash, row)
        return found

    async def recent_signatures(
        self, *, since: datetime, limit: int = 500, company_slugs: list[str] | None = None
    ) -> list[tuple[str, str, list[int]]]:
        """Return ``(job_id, content_hash, minhash signature)`` for recent postings.

        The near-duplicate detector compares new arrivals only against this
        window, which keeps the comparison cost bounded as the corpus grows.
        """
        stmt = (
            select(Job.id, Job.content_hash, Job.similarity_signature)
            .where(Job.first_seen_at >= since)
            .where(Job.status.notin_((JobStatus.REJECTED.value, JobStatus.DUPLICATE.value)))
            .order_by(Job.first_seen_at.desc())
            .limit(limit)
        )
        if company_slugs:
            stmt = stmt.where(Job.company_name.in_(company_slugs))
        result = await self.session.execute(stmt)
        return [(r[0], r[1] or "", list(r[2] or [])) for r in result.all()]

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    async def upsert_many(
        self, jobs: list[NormalizedJob], *, company_ids: dict[str, str] | None = None
    ) -> UpsertResult:
        """Insert new postings and update the ones already known.

        The operation is idempotent: replaying the same batch produces updates
        rather than duplicates, because identity is the ``(source,
        source_job_id)`` natural key.
        """
        result = UpsertResult()
        if not jobs:
            return result

        company_ids = company_ids or {}
        existing = await self.find_by_natural_keys(
            [(job.source, job.source_job_id) for job in jobs]
        )

        inserts: list[dict[str, Any]] = []
        updates: list[dict[str, Any]] = []

        for job in jobs:
            values = job_to_values(job, company_id=company_ids.get(job.company_slug))
            current = existing.get((job.source, job.source_job_id))
            if current is None:
                inserts.append(values)
                result.created.append(job.id)
            else:
                # Preserve discovery metadata; only refresh what can change.
                values["id"] = current.id
                values["first_seen_at"] = current.first_seen_at
                if current.status == JobStatus.EXPIRED.value:
                    # The source re-listed a posting we had already retired.
                    values["status"] = JobStatus.ACTIVE.value
                    values["expired_at"] = None
                elif current.content_hash != job.content_hash:
                    values["status"] = JobStatus.UPDATED.value
                else:
                    values["status"] = current.status
                values["updated_at"] = utcnow()
                updates.append(values)
                job.id = current.id
                result.updated.append(current.id)

        if inserts:
            for chunk in self._chunked(inserts, 100):
                await self.session.execute(insert(Job), chunk)
        if updates:
            for chunk in self._chunked(updates, 100):
                await self.session.execute(update(Job), chunk)

        self.session.expire_all()
        return result

    async def record_salaries(self, jobs: list[NormalizedJob]) -> int:
        """Append observed salary points to the analytical salary table."""
        rows = []
        for job in jobs:
            salary = job.salary
            currency = salary.normalized_currency or salary.currency
            # Without a currency an amount cannot be compared with anything,
            # so it is kept on the posting but excluded from salary analytics.
            if not salary.is_observed or salary.annual_midpoint is None or not currency:
                continue
            rows.append(
                {
                    "job_id": job.id,
                    "currency": currency,
                    "period": str(salary.period),
                    "min_amount": salary.min_amount,
                    "max_amount": salary.max_amount,
                    "annual_min": salary.annual_min,
                    "annual_max": salary.annual_max,
                    "annual_midpoint": salary.annual_midpoint,
                    "provenance": str(salary.provenance),
                    "country_code": job.location.country_code,
                    "segment": str(job.segment),
                    "experience_level": str(job.experience_level),
                    "observed_at": job.published_at or job.first_seen_at,
                }
            )
        if not rows:
            return 0
        job_ids = [r["job_id"] for r in rows]
        for chunk in self._chunked(job_ids, MAX_BIND_PARAMS):
            await self.session.execute(delete(SalaryRecord).where(SalaryRecord.job_id.in_(chunk)))
        for chunk in self._chunked(rows, 200):
            await self.session.execute(insert(SalaryRecord), chunk)
        return len(rows)

    async def mark_duplicate(self, job_id: str, *, duplicate_of: str, kind: DuplicateKind) -> None:
        """Flag a posting as a duplicate of another one."""
        await self.session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                status=JobStatus.DUPLICATE.value,
                duplicate_of=duplicate_of,
                duplicate_kind=str(kind),
                updated_at=utcnow(),
            )
        )

    async def touch_seen(self, job_ids: list[str], *, seen_at: datetime | None = None) -> int:
        """Record that postings were observed again in the current run."""
        if not job_ids:
            return 0
        moment = seen_at or utcnow()
        affected = 0
        for chunk in self._chunked(job_ids, MAX_BIND_PARAMS):
            result = await self.session.execute(
                update(Job).where(Job.id.in_(chunk)).values(last_seen_at=moment)
            )
            affected += affected_rows(result)
        return affected

    async def expire_stale(self, *, cutoff: datetime, source: str | None = None) -> int:
        """Expire postings that have not been seen since ``cutoff``."""
        stmt = (
            update(Job)
            .where(Job.status.in_(LIVE_STATUSES))
            .where(Job.last_seen_at < cutoff)
            .values(status=JobStatus.EXPIRED.value, expired_at=utcnow(), updated_at=utcnow())
        )
        if source:
            stmt = stmt.where(Job.source == source)
        result = await self.session.execute(stmt)
        return affected_rows(result)

    async def reactivate(self, job_ids: list[str]) -> int:
        """Bring expired postings back to life when a source re-lists them."""
        if not job_ids:
            return 0
        result = await self.session.execute(
            update(Job)
            .where(Job.id.in_(job_ids))
            .where(Job.status == JobStatus.EXPIRED.value)
            .values(status=JobStatus.ACTIVE.value, expired_at=None, updated_at=utcnow())
        )
        return affected_rows(result)

    async def delete_by_source(self, source: str) -> int:
        """Remove every posting from a source (used by ``jobintel purge``)."""
        result = await self.session.execute(delete(Job).where(Job.source == source))
        return affected_rows(result)

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def _apply_filters(self, stmt: Select[Any], query: JobQuery) -> Select[Any]:
        if query.statuses:
            stmt = stmt.where(Job.status.in_(query.statuses))
        if not query.include_duplicates:
            stmt = stmt.where(Job.duplicate_of.is_(None))
        if query.sources:
            stmt = stmt.where(Job.source.in_(query.sources))
        if query.company_name:
            stmt = stmt.where(Job.company_name == query.company_name)
        if query.company_slug:
            stmt = stmt.where(Job.company_id.in_(_company_ids_by_slug(query.company_slug)))
        if query.country_code:
            stmt = stmt.where(Job.country_code == query.country_code.upper())
        if query.city:
            stmt = stmt.where(func.lower(Job.city) == query.city.lower())
        if query.remote_types:
            stmt = stmt.where(Job.remote_type.in_(query.remote_types))
        if query.employment_types:
            stmt = stmt.where(Job.employment_type.in_(query.employment_types))
        if query.experience_levels:
            stmt = stmt.where(Job.experience_level.in_(query.experience_levels))
        if query.segments:
            stmt = stmt.where(Job.segment.in_(query.segments))
        if query.title_contains:
            needle = f"%{canonical_key(query.title_contains)}%"
            stmt = stmt.where(func.lower(Job.normalized_title).like(needle))
        if query.salary_min is not None:
            stmt = stmt.where(Job.salary_annual_max >= query.salary_min)
        if query.salary_max is not None:
            stmt = stmt.where(Job.salary_annual_min <= query.salary_max)
        if query.has_salary is True:
            stmt = stmt.where(Job.salary_provenance == "observed")
        elif query.has_salary is False:
            stmt = stmt.where(Job.salary_provenance != "observed")
        if query.published_after:
            stmt = stmt.where(Job.published_at >= query.published_after)
        if query.published_before:
            stmt = stmt.where(Job.published_at <= query.published_before)
        if query.first_seen_after:
            stmt = stmt.where(Job.first_seen_at >= query.first_seen_after)
        if query.text:
            needle = f"%{canonical_key(query.text)}%"
            stmt = stmt.where(Job.search_text.like(needle))
        if query.skills:
            slugs = tuple(dict.fromkeys(query.skills))
            if query.require_all_skills:
                subquery = (
                    select(JobSkill.job_id)
                    .where(JobSkill.skill_slug.in_(slugs))
                    .group_by(JobSkill.job_id)
                    .having(func.count(func.distinct(JobSkill.skill_slug)) == len(slugs))
                )
            else:
                subquery = select(JobSkill.job_id).where(JobSkill.skill_slug.in_(slugs))
            stmt = stmt.where(Job.id.in_(subquery))
        return stmt

    def _apply_sort(self, stmt: Select[Any], query: JobQuery) -> Select[Any]:
        column = {
            "published_at": Job.published_at,
            "first_seen_at": Job.first_seen_at,
            "last_seen_at": Job.last_seen_at,
            "salary": Job.salary_annual_max,
            "title": Job.normalized_title,
            "relevance": Job.first_seen_at,
        }[query.sort]
        order = column.desc() if query.descending else column.asc()
        # A deterministic tiebreaker keeps pagination stable.
        return stmt.order_by(order, Job.id.asc())

    async def count(self, query: JobQuery) -> int:
        """Number of postings matching a filter."""
        stmt = self._apply_filters(select(func.count()).select_from(Job), query)
        return int((await self.session.execute(stmt)).scalar_one())

    async def search(self, query: JobQuery) -> tuple[list[NormalizedJob], int]:
        """Return a page of postings plus the total match count."""
        total = await self.count(query)
        stmt = self._apply_filters(
            select(Job).options(selectinload(Job.skill_links).selectinload(JobSkill.skill)),
            query,
        )
        stmt = self._apply_sort(stmt, query).limit(query.limit).offset(query.offset)
        rows = (await self.session.execute(stmt)).scalars().unique().all()
        return [job_to_domain(row, list(row.skill_links)) for row in rows], total

    async def list_ids(self, query: JobQuery) -> list[str]:
        """Identifiers only - used by exporters and bulk operations."""
        stmt = self._apply_filters(select(Job.id), query)
        stmt = self._apply_sort(stmt, query).limit(query.limit).offset(query.offset)
        return [row[0] for row in (await self.session.execute(stmt)).all()]

    async def iter_rows(
        self, *, batch_size: int = 1000, statuses: tuple[str, ...] = LIVE_STATUSES
    ) -> list[Job]:
        """Fetch rows for analytical export (callers page with ``offset``)."""
        stmt = (
            select(Job)
            .where(Job.status.in_(statuses))
            .order_by(Job.first_seen_at.asc())
            .limit(batch_size)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def count_active(self) -> int:
        return await self.count(JobQuery(limit=1))

    async def exists_natural_key(self, source: str, source_job_id: str) -> bool:
        stmt = (
            select(func.count())
            .select_from(Job)
            .where(and_(Job.source == source, Job.source_job_id == source_job_id))
        )
        return int((await self.session.execute(stmt)).scalar_one()) > 0


def _company_ids_by_slug(slug: str) -> Select[Any]:
    """Sub-select resolving a company slug to its identifier."""
    from app.storage.models import Company

    return select(Company.id).where(Company.slug == slug)


__all__ = ["LIVE_STATUSES", "JobQuery", "JobRepository", "UpsertResult"]
