"""Aggregation queries backing the analytics engines.

The repository returns plain tuples and dictionaries. Turning those into
trends, scores and rankings is the job of :mod:`app.analytics`, which keeps the
statistics testable without a database.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, TypeVarTuple

from sqlalchemy import ColumnElement, Select, case, func, select

from app.core.timeutils import days_ago, to_date, utcnow
from app.models.analytics import AnalyticsFilter
from app.storage.models import Company, Job, JobSkill, SalaryRecord, Skill
from app.storage.repositories.base import BaseRepository
from app.storage.repositories.jobs import LIVE_STATUSES

_Ts = TypeVarTuple("_Ts")

#: Postings are dated by their publication timestamp, falling back to the
#: moment we first saw them when the source does not provide one.
EVENT_DATE: ColumnElement[Any] = func.coalesce(Job.published_at, Job.first_seen_at)


def _as_date(value: Any) -> date:
    """Normalise whatever the driver returns from ``date()`` into a ``date``."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


class AnalyticsRepository(BaseRepository):
    """Read-only aggregation queries."""

    # ------------------------------------------------------------------ #
    # Filtering
    # ------------------------------------------------------------------ #
    def _base_filters(self, stmt: Select[*_Ts], flt: AnalyticsFilter) -> Select[*_Ts]:
        stmt = stmt.where(Job.status.in_(LIVE_STATUSES)).where(Job.duplicate_of.is_(None))
        start, end = self.window_bounds(flt)
        stmt = stmt.where(start <= EVENT_DATE)
        if end is not None:
            stmt = stmt.where(end >= EVENT_DATE)
        if flt.country_code:
            stmt = stmt.where(Job.country_code == flt.country_code.upper())
        if flt.city:
            stmt = stmt.where(func.lower(Job.city) == flt.city.lower())
        if flt.segment:
            stmt = stmt.where(Job.segment == str(flt.segment))
        if flt.seniority:
            stmt = stmt.where(Job.experience_level == str(flt.seniority))
        if flt.remote_type:
            stmt = stmt.where(Job.remote_type == str(flt.remote_type))
        if flt.source:
            stmt = stmt.where(Job.source == flt.source)
        if flt.company_slug:
            stmt = stmt.join(Company, Job.company_id == Company.id).where(
                Company.slug == flt.company_slug
            )
        if flt.skill:
            stmt = stmt.where(
                Job.id.in_(select(JobSkill.job_id).where(JobSkill.skill_slug == flt.skill))
            )
        return stmt

    @staticmethod
    def window_bounds(flt: AnalyticsFilter) -> tuple[datetime, datetime | None]:
        """Resolve the filter into concrete ``(start, end)`` timestamps."""
        start = flt.date_from or days_ago(flt.window_days)
        return start, flt.date_to

    # ------------------------------------------------------------------ #
    # Volume
    # ------------------------------------------------------------------ #
    async def daily_volume(self, flt: AnalyticsFilter) -> list[tuple[date, int]]:
        """Number of postings per calendar day."""
        day = func.date(EVENT_DATE).label("day")
        stmt = self._base_filters(select(day, func.count().label("total")), flt)
        stmt = stmt.group_by(day).order_by(day)
        rows = (await self.session.execute(stmt)).all()
        return [(_as_date(row.day), int(row.total)) for row in rows]

    async def total_jobs(self, flt: AnalyticsFilter) -> int:
        stmt = self._base_filters(select(func.count()).select_from(Job), flt)
        return int((await self.session.execute(stmt)).scalar_one())

    async def count_since(self, since: datetime) -> int:
        """Postings first seen since a timestamp (ignores the analytics filter)."""
        stmt = (
            select(func.count())
            .select_from(Job)
            .where(Job.status.in_(LIVE_STATUSES))
            .where(Job.duplicate_of.is_(None))
            .where(Job.first_seen_at >= since)
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def active_job_count(self) -> int:
        stmt = (
            select(func.count())
            .select_from(Job)
            .where(Job.status.in_(LIVE_STATUSES))
            .where(Job.duplicate_of.is_(None))
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def total_job_count(self) -> int:
        stmt = select(func.count()).select_from(Job)
        return int((await self.session.execute(stmt)).scalar_one())

    # ------------------------------------------------------------------ #
    # Skills
    # ------------------------------------------------------------------ #
    async def skill_counts(
        self, flt: AnalyticsFilter, *, limit: int | None = None
    ) -> list[tuple[str, str, str, int, float]]:
        """``(slug, name, category, job_count, average_confidence)`` per skill."""
        stmt = (
            select(
                JobSkill.skill_slug,
                func.coalesce(Skill.name, JobSkill.skill_slug).label("name"),
                func.coalesce(Skill.category, "other").label("category"),
                func.count(func.distinct(JobSkill.job_id)).label("job_count"),
                func.avg(JobSkill.confidence).label("avg_confidence"),
            )
            .select_from(Job)
            .join(JobSkill, JobSkill.job_id == Job.id)
            .join(Skill, Skill.slug == JobSkill.skill_slug, isouter=True)
        )
        stmt = self._base_filters(stmt, flt)
        stmt = stmt.group_by(JobSkill.skill_slug, Skill.name, Skill.category)
        stmt = stmt.order_by(func.count(func.distinct(JobSkill.job_id)).desc())
        stmt = stmt.limit(limit or flt.limit)
        rows = (await self.session.execute(stmt)).all()
        return [(row[0], row[1], row[2], int(row[3]), float(row[4] or 0.0)) for row in rows]

    async def skill_counts_between(
        self, slugs: list[str], *, start: datetime, end: datetime
    ) -> dict[str, int]:
        """Job counts per skill inside an explicit interval."""
        stmt = (
            select(JobSkill.skill_slug, func.count(func.distinct(JobSkill.job_id)))
            .select_from(Job)
            .join(JobSkill, JobSkill.job_id == Job.id)
            .where(Job.status.in_(LIVE_STATUSES))
            .where(Job.duplicate_of.is_(None))
            .where(start <= EVENT_DATE)
            .where(end > EVENT_DATE)
            .group_by(JobSkill.skill_slug)
        )
        if slugs:
            stmt = stmt.where(JobSkill.skill_slug.in_(slugs))
        rows = (await self.session.execute(stmt)).all()
        return {row[0]: int(row[1]) for row in rows}

    async def skill_daily_counts(
        self, slug: str, *, start: datetime, end: datetime | None = None
    ) -> list[tuple[date, int]]:
        """Daily demand series for one skill."""
        day = func.date(EVENT_DATE).label("day")
        stmt = (
            select(day, func.count(func.distinct(Job.id)))
            .select_from(Job)
            .join(JobSkill, JobSkill.job_id == Job.id)
            .where(JobSkill.skill_slug == slug)
            .where(Job.status.in_(LIVE_STATUSES))
            .where(Job.duplicate_of.is_(None))
            .where(start <= EVENT_DATE)
            .group_by(day)
            .order_by(day)
        )
        if end is not None:
            stmt = stmt.where(end >= EVENT_DATE)
        rows = (await self.session.execute(stmt)).all()
        return [(_as_date(row[0]), int(row[1])) for row in rows]

    async def jobs_with_skill_count(self, flt: AnalyticsFilter) -> int:
        """How many postings in the window mention at least one skill."""
        stmt = (
            select(func.count(func.distinct(Job.id)))
            .select_from(Job)
            .join(JobSkill, JobSkill.job_id == Job.id)
        )
        stmt = self._base_filters(stmt, flt)
        return int((await self.session.execute(stmt)).scalar_one())

    async def skill_pairs(
        self, flt: AnalyticsFilter, *, min_count: int = 2, limit: int = 200
    ) -> list[tuple[str, str, int]]:
        """Co-occurrence counts for skill pairs inside the filtered window.

        The self-join is constrained to ``a < b`` so each unordered pair is
        counted once.
        """
        left = JobSkill.__table__.alias("sk_a")
        right = JobSkill.__table__.alias("sk_b")
        stmt = (
            select(
                left.c.skill_slug,
                right.c.skill_slug,
                func.count(func.distinct(left.c.job_id)).label("pair_count"),
            )
            .select_from(Job)
            .join(left, left.c.job_id == Job.id)
            .join(right, (right.c.job_id == Job.id) & (right.c.skill_slug > left.c.skill_slug))
        )
        stmt = self._base_filters(stmt, flt)
        stmt = (
            stmt.group_by(left.c.skill_slug, right.c.skill_slug)
            .having(func.count(func.distinct(left.c.job_id)) >= min_count)
            .order_by(func.count(func.distinct(left.c.job_id)).desc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [(row[0], row[1], int(row[2])) for row in rows]

    # ------------------------------------------------------------------ #
    # Dimensions
    # ------------------------------------------------------------------ #
    async def remote_counts(self, flt: AnalyticsFilter) -> dict[str, int]:
        stmt = self._base_filters(
            select(Job.remote_type, func.count()).group_by(Job.remote_type), flt
        )
        rows = (await self.session.execute(stmt)).all()
        return {row[0]: int(row[1]) for row in rows}

    async def seniority_counts(self, flt: AnalyticsFilter) -> dict[str, int]:
        stmt = self._base_filters(
            select(Job.experience_level, func.count()).group_by(Job.experience_level), flt
        )
        rows = (await self.session.execute(stmt)).all()
        return {row[0]: int(row[1]) for row in rows}

    async def segment_counts(self, flt: AnalyticsFilter) -> dict[str, int]:
        stmt = self._base_filters(select(Job.segment, func.count()).group_by(Job.segment), flt)
        rows = (await self.session.execute(stmt)).all()
        return {row[0]: int(row[1]) for row in rows}

    async def employment_counts(self, flt: AnalyticsFilter) -> dict[str, int]:
        stmt = self._base_filters(
            select(Job.employment_type, func.count()).group_by(Job.employment_type), flt
        )
        rows = (await self.session.execute(stmt)).all()
        return {row[0]: int(row[1]) for row in rows}

    async def average_experience_years(self, flt: AnalyticsFilter) -> float | None:
        stmt = self._base_filters(select(func.avg(Job.experience_years_min)), flt)
        value = (await self.session.execute(stmt)).scalar_one_or_none()
        return float(value) if value is not None else None

    async def country_counts(
        self, flt: AnalyticsFilter, *, limit: int = 50
    ) -> list[tuple[str | None, str | None, int, int]]:
        """``(country_code, country, job_count, remote_count)`` per country."""
        remote_expr = func.sum(case((Job.remote_type == "remote", 1), else_=0))
        stmt = self._base_filters(
            select(Job.country_code, Job.country, func.count(), remote_expr), flt
        )
        stmt = (
            stmt.where(Job.country_code.is_not(None))
            .group_by(Job.country_code, Job.country)
            .order_by(func.count().desc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [(row[0], row[1], int(row[2]), int(row[3] or 0)) for row in rows]

    async def city_counts(
        self, flt: AnalyticsFilter, *, limit: int = 50
    ) -> list[tuple[str | None, str | None, str | None, int]]:
        stmt = self._base_filters(select(Job.city, Job.region, Job.country_code, func.count()), flt)
        stmt = (
            stmt.where(Job.city.is_not(None))
            .group_by(Job.city, Job.region, Job.country_code)
            .order_by(func.count().desc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [(row[0], row[1], row[2], int(row[3])) for row in rows]

    async def distinct_country_count(self, flt: AnalyticsFilter) -> int:
        stmt = self._base_filters(select(func.count(func.distinct(Job.country_code))), flt)
        return int((await self.session.execute(stmt)).scalar_one() or 0)

    async def distinct_company_count(self, flt: AnalyticsFilter) -> int:
        stmt = self._base_filters(select(func.count(func.distinct(Job.company_name))), flt)
        return int((await self.session.execute(stmt)).scalar_one() or 0)

    # ------------------------------------------------------------------ #
    # Companies
    # ------------------------------------------------------------------ #
    async def company_counts(
        self, flt: AnalyticsFilter, *, limit: int = 25
    ) -> list[tuple[str | None, str, int, int]]:
        """``(company_id, company_name, job_count, remote_count)``."""
        remote_expr = func.sum(case((Job.remote_type == "remote", 1), else_=0))
        stmt = self._base_filters(
            select(Job.company_id, Job.company_name, func.count(), remote_expr), flt
        )
        stmt = (
            stmt.group_by(Job.company_id, Job.company_name)
            .order_by(func.count().desc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [(row[0], row[1], int(row[2]), int(row[3] or 0)) for row in rows]

    async def company_job_counts_between(
        self, *, start: datetime, end: datetime, limit: int = 100
    ) -> dict[str, int]:
        stmt = (
            select(Job.company_name, func.count())
            .where(Job.status.in_(LIVE_STATUSES))
            .where(Job.duplicate_of.is_(None))
            .where(start <= EVENT_DATE)
            .where(end > EVENT_DATE)
            .group_by(Job.company_name)
            .order_by(func.count().desc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return {row[0]: int(row[1]) for row in rows}

    async def top_skills_for_company(
        self, company_name: str, *, limit: int = 10
    ) -> list[tuple[str, str, int]]:
        stmt = (
            select(
                JobSkill.skill_slug,
                func.coalesce(Skill.name, JobSkill.skill_slug),
                func.count(func.distinct(JobSkill.job_id)),
            )
            .select_from(Job)
            .join(JobSkill, JobSkill.job_id == Job.id)
            .join(Skill, Skill.slug == JobSkill.skill_slug, isouter=True)
            .where(Job.company_name == company_name)
            .where(Job.status.in_(LIVE_STATUSES))
            .group_by(JobSkill.skill_slug, Skill.name)
            .order_by(func.count(func.distinct(JobSkill.job_id)).desc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [(row[0], row[1], int(row[2])) for row in rows]

    # ------------------------------------------------------------------ #
    # Salaries
    # ------------------------------------------------------------------ #
    async def salary_samples(self, flt: AnalyticsFilter, *, limit: int = 50_000) -> list[float]:
        """Annualised midpoints of observed salaries in the window."""
        stmt = (
            select(SalaryRecord.annual_midpoint)
            .select_from(Job)
            .join(SalaryRecord, SalaryRecord.job_id == Job.id)
            .where(SalaryRecord.annual_midpoint.is_not(None))
        )
        stmt = self._base_filters(stmt, flt).limit(limit)
        rows = (await self.session.execute(stmt)).all()
        return [float(row[0]) for row in rows if row[0] is not None]

    async def salary_samples_by(
        self, flt: AnalyticsFilter, *, dimension: str, limit: int = 50_000
    ) -> list[tuple[str, float]]:
        """Salary observations tagged with a grouping key."""
        column = {
            "segment": Job.segment,
            "seniority": Job.experience_level,
            "country": Job.country_code,
            "city": Job.city,
            "company": Job.company_name,
            "title": Job.normalized_title,
            "remote": Job.remote_type,
        }[dimension]
        stmt = (
            select(column, SalaryRecord.annual_midpoint)
            .select_from(Job)
            .join(SalaryRecord, SalaryRecord.job_id == Job.id)
            .where(SalaryRecord.annual_midpoint.is_not(None))
            .where(column.is_not(None))
        )
        stmt = self._base_filters(stmt, flt).limit(limit)
        rows = (await self.session.execute(stmt)).all()
        # The WHERE clause already excludes NULL midpoints; the comprehension
        # repeats the check so the type checker can see it too.
        return [(str(key), float(mid)) for key, mid in rows if mid is not None]

    async def salary_samples_for_skill(
        self, slug: str, flt: AnalyticsFilter, *, limit: int = 20_000
    ) -> list[float]:
        stmt = (
            select(SalaryRecord.annual_midpoint)
            .select_from(Job)
            .join(SalaryRecord, SalaryRecord.job_id == Job.id)
            .join(JobSkill, JobSkill.job_id == Job.id)
            .where(JobSkill.skill_slug == slug)
            .where(SalaryRecord.annual_midpoint.is_not(None))
        )
        stmt = self._base_filters(stmt, flt).limit(limit)
        rows = (await self.session.execute(stmt)).all()
        return [float(row[0]) for row in rows if row[0] is not None]

    async def observed_salary_share(self, flt: AnalyticsFilter) -> float:
        """Fraction of postings in the window that disclose a salary."""
        total = await self.total_jobs(flt)
        if total == 0:
            return 0.0
        stmt = self._base_filters(
            select(func.count()).select_from(Job).where(Job.salary_provenance == "observed"), flt
        )
        observed = int((await self.session.execute(stmt)).scalar_one())
        return observed / total

    # ------------------------------------------------------------------ #
    # Quality / freshness
    # ------------------------------------------------------------------ #
    async def average_quality_score(self, flt: AnalyticsFilter) -> float:
        stmt = self._base_filters(select(func.avg(Job.quality_score)), flt)
        value = (await self.session.execute(stmt)).scalar_one_or_none()
        return float(value or 0.0)

    async def freshness_hours(self) -> float | None:
        """Age of the most recently ingested posting, in hours."""
        stmt = select(func.max(Job.first_seen_at))
        latest = (await self.session.execute(stmt)).scalar_one_or_none()
        if latest is None:
            return None
        from app.core.timeutils import ensure_utc

        return max(0.0, (utcnow() - ensure_utc(latest)).total_seconds() / 3600.0)

    async def source_counts(self) -> dict[str, int]:
        stmt = select(Job.source, func.count()).group_by(Job.source)
        rows = (await self.session.execute(stmt)).all()
        return {row[0]: int(row[1]) for row in rows}

    async def latest_snapshot_date(self) -> date | None:
        from app.storage.models import MarketSnapshot

        stmt = select(func.max(MarketSnapshot.snapshot_date))
        value = (await self.session.execute(stmt)).scalar_one_or_none()
        return to_date(value) if value is not None else None


__all__ = ["EVENT_DATE", "AnalyticsRepository"]
