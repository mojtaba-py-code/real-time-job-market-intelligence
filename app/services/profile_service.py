"""Candidate profiles and personal market intelligence."""

from __future__ import annotations

from app.analytics.recommendations.market_fit import MarketFitEngine
from app.core.config import Settings, get_settings
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.models.analytics import MarketFitResult
from app.models.profiles import CandidateProfile
from app.storage.repositories.analytics import AnalyticsRepository
from app.storage.repositories.jobs import JobRepository
from app.storage.repositories.profiles import ProfileRepository
from app.storage.session import Database

log = get_logger(__name__)


class ProfileService:
    """CRUD for profiles plus the market-fit calculation."""

    def __init__(self, database: Database, *, settings: Settings | None = None) -> None:
        self._db = database
        self._settings = settings or get_settings()

    async def save(self, profile: CandidateProfile) -> CandidateProfile:
        async with self._db.session() as session:
            return await ProfileRepository(session).save(profile)

    async def get(self, profile_id: str) -> CandidateProfile:
        async with self._db.read_session() as session:
            profile = await ProfileRepository(session).get(profile_id)
        if profile is None:
            raise NotFoundError(f"profile {profile_id} was not found")
        return profile

    async def list_profiles(self, *, owner: str | None = None) -> list[CandidateProfile]:
        async with self._db.read_session() as session:
            return await ProfileRepository(session).list_profiles(owner=owner)

    async def delete(self, profile_id: str) -> bool:
        async with self._db.session() as session:
            return await ProfileRepository(session).delete(profile_id)

    async def market_fit(
        self, profile: CandidateProfile, *, window_days: int = 30
    ) -> MarketFitResult:
        """Score a profile against current demand."""
        async with self._db.read_session() as session:
            engine = MarketFitEngine(
                AnalyticsRepository(session),
                JobRepository(session),
                settings=self._settings.analytics,
            )
            return await engine.evaluate(profile, window_days=window_days)

    async def market_fit_for(self, profile_id: str, *, window_days: int = 30) -> MarketFitResult:
        """Score a stored profile."""
        return await self.market_fit(await self.get(profile_id), window_days=window_days)


__all__ = ["ProfileService"]
