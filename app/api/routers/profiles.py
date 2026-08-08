"""Personal job intelligence: candidate profiles and market fit."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, status

from app.api.deps import PrincipalDep, ProfileServiceDep, require_scope
from app.api.schemas import ProfileInput
from app.core.errors import NotFoundError
from app.models.analytics import MarketFitResult
from app.models.enums import Scope
from app.models.profiles import CandidateProfile

router = APIRouter(
    prefix="/profiles", tags=["profiles"], dependencies=[require_scope(Scope.PROFILES_WRITE)]
)

ProfileId = Annotated[str, Path(min_length=8, max_length=32, pattern=r"^[0-9a-f]+$")]


@router.get("", response_model=list[CandidateProfile], summary="List profiles")
async def list_profiles(
    profiles: ProfileServiceDep, principal: PrincipalDep
) -> list[CandidateProfile]:
    """Profiles owned by the calling key."""
    return await profiles.list_profiles(owner=principal.key_id if principal else None)


@router.post(
    "",
    response_model=CandidateProfile,
    status_code=status.HTTP_201_CREATED,
    summary="Create or update a profile",
)
async def save_profile(
    profiles: ProfileServiceDep, principal: PrincipalDep, payload: ProfileInput
) -> CandidateProfile:
    """Profiles are keyed by ``(owner, label)``, so posting twice updates."""
    profile = CandidateProfile(
        **payload.model_dump(), owner=principal.key_id if principal else None
    )
    return await profiles.save(profile)


@router.get("/{profile_id}", response_model=CandidateProfile, summary="Get a profile")
async def get_profile(profiles: ProfileServiceDep, profile_id: ProfileId) -> CandidateProfile:
    return await profiles.get(profile_id)


@router.delete(
    "/{profile_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Delete a profile",
)
async def delete_profile(profiles: ProfileServiceDep, profile_id: ProfileId) -> None:
    if not await profiles.delete(profile_id):
        raise NotFoundError(f"profile {profile_id} was not found")


@router.get(
    "/{profile_id}/market-fit",
    response_model=MarketFitResult,
    summary="Market fit for a stored profile",
)
async def market_fit(
    profiles: ProfileServiceDep,
    profile_id: ProfileId,
    window_days: Annotated[int, Query(ge=7, le=365)] = 30,
) -> MarketFitResult:
    """Skill coverage, missing skills and matching postings."""
    return await profiles.market_fit_for(profile_id, window_days=window_days)


@router.post(
    "/market-fit",
    response_model=MarketFitResult,
    summary="Market fit for an ad-hoc profile",
)
async def market_fit_adhoc(
    profiles: ProfileServiceDep,
    payload: ProfileInput,
    window_days: Annotated[int, Query(ge=7, le=365)] = 30,
) -> MarketFitResult:
    """Score a profile without storing it."""
    profile = CandidateProfile(**payload.model_dump())
    return await profiles.market_fit(profile, window_days=window_days)


__all__ = ["router"]
