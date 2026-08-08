"""Repositories for the reference entities: companies, locations and skills."""

from __future__ import annotations

from sqlalchemy import case, delete, func, insert, select, update

from app.core.text import canonical_key, slugify
from app.core.timeutils import utcnow
from app.models.job import ExtractedSkill, LocationInfo
from app.storage.models import Company, JobSkill, Location, Skill
from app.storage.repositories.base import MAX_BIND_PARAMS, BaseRepository


class CompanyRepository(BaseRepository):
    """Resolve and maintain hiring organisations."""

    async def get_by_slug(self, slug: str) -> Company | None:
        result = await self.session.execute(select(Company).where(Company.slug == slug))
        return result.scalar_one_or_none()

    async def get_by_id(self, company_id: str) -> Company | None:
        return await self.session.get(Company, company_id)

    async def resolve_many(self, names: list[str]) -> dict[str, Company]:
        """Fetch (or create) a company row for every distinct name.

        Returns a mapping keyed by slug so callers can attach ``company_id``
        without issuing one query per posting.
        """
        cleaned = {slugify(name): name.strip() for name in names if name and name.strip()}
        if not cleaned:
            return {}

        found: dict[str, Company] = {}
        for chunk in self._chunked(list(cleaned), MAX_BIND_PARAMS):
            result = await self.session.execute(select(Company).where(Company.slug.in_(chunk)))
            for company in result.scalars():
                found[company.slug] = company

        now = utcnow()
        new_rows: list[Company] = []
        for slug, name in cleaned.items():
            existing = found.get(slug)
            if existing is None:
                company = Company(slug=slug, name=name, first_seen_at=now, last_seen_at=now)
                new_rows.append(company)
                found[slug] = company
            else:
                existing.last_seen_at = now

        if new_rows:
            self.session.add_all(new_rows)
            await self.session.flush()
        return found

    async def enrich(self, slug: str, *, industry: str | None, country_code: str | None) -> None:
        """Fill in company attributes discovered from postings."""
        company = await self.get_by_slug(slug)
        if company is None:
            return
        if industry and not company.industry:
            company.industry = industry
        if country_code and not company.country_code:
            company.country_code = country_code

    async def refresh_job_counts(self) -> int:
        """Recompute ``active_job_count`` / ``total_job_count`` for all companies."""
        from app.storage.models import Job

        totals = await self.session.execute(
            select(
                Job.company_id,
                func.count().label("total"),
                func.sum(case((Job.status.in_(("active", "updated")), 1), else_=0)).label("active"),
            )
            .where(Job.company_id.is_not(None))
            .group_by(Job.company_id)
        )
        updates = [
            {
                "id": row.company_id,
                "total_job_count": row.total,
                "active_job_count": row.active or 0,
            }
            for row in totals
        ]
        if not updates:
            return 0
        await self.session.execute(update(Company), updates)
        return len(updates)

    async def count(self) -> int:
        result = await self.session.execute(select(func.count()).select_from(Company))
        return int(result.scalar_one())


class LocationRepository(BaseRepository):
    """Resolve normalized geographies."""

    @staticmethod
    def location_slug(info: LocationInfo) -> str:
        parts = [info.country_code or "", info.region or "", info.city or ""]
        return slugify("-".join(p for p in parts if p)) or "unknown"

    async def resolve_many(self, locations: list[LocationInfo]) -> dict[str, Location]:
        """Fetch or create rows for a batch of locations, keyed by slug."""
        wanted: dict[str, LocationInfo] = {}
        for info in locations:
            if not info.is_resolved:
                continue
            wanted.setdefault(self.location_slug(info), info)
        if not wanted:
            return {}

        found: dict[str, Location] = {}
        for chunk in self._chunked(list(wanted), MAX_BIND_PARAMS):
            result = await self.session.execute(select(Location).where(Location.slug.in_(chunk)))
            for row in result.scalars():
                found[row.slug] = row

        new_rows = [
            Location(
                slug=slug,
                country_code=info.country_code,
                country=info.country,
                region=info.region,
                city=info.city,
            )
            for slug, info in wanted.items()
            if slug not in found
        ]
        if new_rows:
            self.session.add_all(new_rows)
            await self.session.flush()
            for row in new_rows:
                found[row.slug] = row
        return found


class SkillRepository(BaseRepository):
    """Persist the skill taxonomy and the job/skill association table."""

    async def upsert_skills(self, skills: list[ExtractedSkill]) -> dict[str, Skill]:
        """Ensure a row exists for every referenced skill."""
        by_slug: dict[str, ExtractedSkill] = {}
        for skill in skills:
            by_slug.setdefault(skill.slug, skill)
        if not by_slug:
            return {}

        found: dict[str, Skill] = {}
        for chunk in self._chunked(list(by_slug), MAX_BIND_PARAMS):
            result = await self.session.execute(select(Skill).where(Skill.slug.in_(chunk)))
            for row in result.scalars():
                found[row.slug] = row

        new_rows = [
            Skill(
                slug=slug,
                name=skill.name,
                category=str(skill.category),
                parent_slug=None,
                aliases=[],
            )
            for slug, skill in by_slug.items()
            if slug not in found
        ]
        if new_rows:
            self.session.add_all(new_rows)
            await self.session.flush()
            for row in new_rows:
                found[row.slug] = row
        return found

    async def sync_taxonomy(self, nodes: list[dict[str, str | None]]) -> int:
        """Write the configured taxonomy (slug/name/category/parent) to the database."""
        if not nodes:
            return 0
        slugs = [str(node["slug"]) for node in nodes]
        existing: dict[str, Skill] = {}
        for chunk in self._chunked(slugs, MAX_BIND_PARAMS):
            result = await self.session.execute(select(Skill).where(Skill.slug.in_(chunk)))
            for row in result.scalars():
                existing[row.slug] = row

        # Two passes: every row is written without a parent first, then the
        # self-referencing links are applied. A single pass would depend on the
        # order the unit of work happens to emit inserts and updates in, which
        # SQLite's foreign-key checker will not forgive.
        written = 0
        for node in nodes:
            slug = str(node["slug"])
            found = existing.get(slug)
            if found is None:
                found = Skill(slug=slug)
                self.session.add(found)
                existing[slug] = found
            row = found
            row.name = str(node.get("name") or slug)
            row.category = str(node.get("category") or "other")
            row.parent_slug = None
            aliases = node.get("aliases")
            row.aliases = list(aliases) if isinstance(aliases, list) else []
            written += 1
        await self.session.flush()

        for node in nodes:
            parent = node.get("parent")
            if not parent:
                continue
            candidate = existing.get(str(node["slug"]))
            if candidate is not None and str(parent) in existing:
                candidate.parent_slug = str(parent)
        await self.session.flush()
        return written

    async def replace_job_skills(self, job_id: str, skills: list[ExtractedSkill]) -> None:
        """Replace the skill links of a posting."""
        await self.session.execute(delete(JobSkill).where(JobSkill.job_id == job_id))
        if not skills:
            return
        await self.upsert_skills(skills)
        seen: set[str] = set()
        rows = []
        for skill in skills:
            if skill.slug in seen:
                continue
            seen.add(skill.slug)
            rows.append(
                {
                    "job_id": job_id,
                    "skill_slug": skill.slug,
                    "confidence": skill.confidence,
                    "occurrences": skill.occurrences,
                    "field": skill.field,
                    "is_required": skill.is_required,
                }
            )
        await self.session.execute(insert(JobSkill), rows)

    async def bulk_replace_job_skills(self, mapping: dict[str, list[ExtractedSkill]]) -> int:
        """Replace skill links for many postings in as few statements as possible."""
        if not mapping:
            return 0
        job_ids = list(mapping)
        for chunk in self._chunked(job_ids, MAX_BIND_PARAMS):
            await self.session.execute(delete(JobSkill).where(JobSkill.job_id.in_(chunk)))

        all_skills = [skill for skills in mapping.values() for skill in skills]
        await self.upsert_skills(all_skills)

        rows = []
        for job_id, skills in mapping.items():
            seen: set[str] = set()
            for skill in skills:
                if skill.slug in seen:
                    continue
                seen.add(skill.slug)
                rows.append(
                    {
                        "job_id": job_id,
                        "skill_slug": skill.slug,
                        "confidence": skill.confidence,
                        "occurrences": skill.occurrences,
                        "field": skill.field,
                        "is_required": skill.is_required,
                    }
                )
        if rows:
            for chunk in self._chunked(rows, 200):
                await self.session.execute(insert(JobSkill), chunk)
        return len(rows)

    async def list_skills(self, *, category: str | None = None, limit: int = 500) -> list[Skill]:
        stmt = select(Skill).order_by(Skill.slug).limit(limit)
        if category:
            stmt = stmt.where(Skill.category == category)
        result = await self.session.execute(stmt)
        return list(result.scalars())

    async def get(self, slug: str) -> Skill | None:
        return await self.session.get(Skill, canonical_key(slug).replace(" ", "-"))


__all__ = ["CompanyRepository", "LocationRepository", "SkillRepository"]
