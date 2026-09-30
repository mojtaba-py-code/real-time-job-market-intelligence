"""Persistence for candidate profiles."""

from __future__ import annotations

from sqlalchemy import delete, select

from app.core.timeutils import utcnow
from app.models.profiles import CandidateProfile
from app.storage.mappers import profile_to_domain, profile_to_values
from app.storage.models import CandidateProfileRow
from app.storage.repositories.base import BaseRepository, affected_rows


class ProfileRepository(BaseRepository):
    """CRUD for the personal job-intelligence module."""

    async def save(self, profile: CandidateProfile) -> CandidateProfile:
        """Insert or update a profile, keyed by ``(owner, label)``."""
        stmt = select(CandidateProfileRow).where(
            CandidateProfileRow.owner == profile.owner,
            CandidateProfileRow.label == profile.label,
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        profile.updated_at = utcnow()
        values = profile_to_values(profile)
        if row is None:
            self.session.add(CandidateProfileRow(**values))
        else:
            values.pop("id")
            values.pop("created_at")
            for key, value in values.items():
                setattr(row, key, value)
            profile.id = row.id
        await self.session.flush()
        return profile

    async def get(self, profile_id: str) -> CandidateProfile | None:
        row = await self.session.get(CandidateProfileRow, profile_id)
        return profile_to_domain(row) if row else None

    async def get_by_label(self, owner: str | None, label: str) -> CandidateProfile | None:
        stmt = select(CandidateProfileRow).where(
            CandidateProfileRow.owner == owner, CandidateProfileRow.label == label
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        return profile_to_domain(row) if row else None

    async def list_profiles(
        self, *, owner: str | None = None, limit: int = 100
    ) -> list[CandidateProfile]:
        stmt = (
            select(CandidateProfileRow).order_by(CandidateProfileRow.updated_at.desc()).limit(limit)
        )
        if owner is not None:
            stmt = stmt.where(CandidateProfileRow.owner == owner)
        rows = (await self.session.execute(stmt)).scalars()
        return [profile_to_domain(row) for row in rows]

    async def delete(self, profile_id: str) -> bool:
        result = await self.session.execute(
            delete(CandidateProfileRow).where(CandidateProfileRow.id == profile_id)
        )
        return affected_rows(result) > 0


__all__ = ["ProfileRepository"]
