"""Company intelligence endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.api.deps import AnalyticsFilterDep, AnalyticsServiceDep, JobServiceDep, require_scope
from app.api.schemas import JobSummary, Page
from app.core.errors import NotFoundError
from app.models.analytics import CompanyHiring
from app.models.enums import Scope
from app.storage.repositories.jobs import JobQuery

router = APIRouter(
    prefix="/companies", tags=["companies"], dependencies=[require_scope(Scope.ANALYTICS_READ)]
)

CompanyName = Annotated[str, Path(min_length=1, max_length=256)]


@router.get("", response_model=list[CompanyHiring], summary="Companies that are hiring")
async def list_companies(
    analytics: AnalyticsServiceDep,
    flt: AnalyticsFilterDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    with_skills: bool = False,
) -> list[CompanyHiring]:
    """Ranked by number of open postings in the window."""
    return await analytics.companies(flt, limit=limit, with_skills=with_skills)


@router.get("/{name}", response_model=CompanyHiring, summary="Company hiring profile")
async def company_profile(
    analytics: AnalyticsServiceDep, flt: AnalyticsFilterDep, name: CompanyName
) -> CompanyHiring:
    """Hiring volume, growth, remote share and top skills for one company."""
    profile = await analytics.company_profile(name, flt)
    if profile is None:
        raise NotFoundError(f"no hiring data for {name!r}", details={"company": name})
    return profile


@router.get("/{name}/jobs", response_model=Page[JobSummary], summary="Postings by a company")
async def company_jobs(
    jobs: JobServiceDep,
    name: CompanyName,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> Page[JobSummary]:
    """Active postings published by one company."""
    items, total = await jobs.list_jobs(
        JobQuery(company_name=name, limit=limit, offset=offset, sort="published_at")
    )
    return Page.build(
        [JobSummary.from_job(job) for job in items], total=total, limit=limit, offset=offset
    )


__all__ = ["router"]
