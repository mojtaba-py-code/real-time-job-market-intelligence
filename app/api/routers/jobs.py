"""Job listing, retrieval and search."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query

from app.api.deps import JobServiceDep, require_scope
from app.api.schemas import JobDetail, JobSummary, Page, PageMeta, SearchMeta, SearchResult
from app.core.timeutils import days_ago
from app.models.enums import EmploymentType, ExperienceLevel, MarketSegment, RemoteType, Scope
from app.search.engine import SearchRequest
from app.storage.repositories.jobs import JobQuery, SortField

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[require_scope(Scope.JOBS_READ)])

JobId = Annotated[str, Path(min_length=8, max_length=32, pattern=r"^[0-9a-f]+$")]


@router.get("", response_model=Page[JobSummary], summary="List postings")
async def list_jobs(
    jobs: JobServiceDep,
    company: Annotated[str | None, Query(max_length=256)] = None,
    country_code: Annotated[str | None, Query(min_length=2, max_length=2)] = None,
    city: Annotated[str | None, Query(max_length=96)] = None,
    remote_type: RemoteType | None = None,
    employment_type: EmploymentType | None = None,
    experience_level: ExperienceLevel | None = None,
    segment: MarketSegment | None = None,
    skill: Annotated[list[str] | None, Query(max_length=10)] = None,
    require_all_skills: bool = True,
    source: Annotated[str | None, Query(max_length=64)] = None,
    salary_min: Annotated[float | None, Query(ge=0)] = None,
    salary_max: Annotated[float | None, Query(ge=0)] = None,
    has_salary: bool | None = None,
    posted_within_days: Annotated[int | None, Query(ge=1, le=365)] = None,
    published_after: datetime | None = None,
    published_before: datetime | None = None,
    sort: SortField = "published_at",
    descending: bool = True,
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> Page[JobSummary]:
    """Filter, sort and page over active postings."""
    query = JobQuery(
        company_name=company,
        country_code=country_code,
        city=city,
        remote_types=(str(remote_type),) if remote_type else (),
        employment_types=(str(employment_type),) if employment_type else (),
        experience_levels=(str(experience_level),) if experience_level else (),
        segments=(str(segment),) if segment else (),
        sources=(source,) if source else (),
        skills=tuple(skill or ()),
        require_all_skills=require_all_skills,
        salary_min=salary_min,
        salary_max=salary_max,
        has_salary=has_salary,
        published_after=published_after,
        published_before=published_before,
        first_seen_after=days_ago(posted_within_days) if posted_within_days else None,
        sort=sort,
        descending=descending,
        limit=limit,
        offset=offset,
    )
    items, total = await jobs.list_jobs(query)
    return Page.build(
        [JobSummary.from_job(job) for job in items], total=total, limit=limit, offset=offset
    )


@router.get("/search", response_model=SearchResult, summary="Search postings")
async def search_jobs(
    jobs: JobServiceDep,
    q: Annotated[str | None, Query(max_length=200, description="Free-text query")] = None,
    skill: Annotated[list[str] | None, Query(max_length=10)] = None,
    require_all_skills: bool = True,
    company: Annotated[str | None, Query(max_length=256)] = None,
    country_code: Annotated[str | None, Query(min_length=2, max_length=2)] = None,
    city: Annotated[str | None, Query(max_length=96)] = None,
    remote_type: RemoteType | None = None,
    experience_level: ExperienceLevel | None = None,
    segment: MarketSegment | None = None,
    salary_min: Annotated[float | None, Query(ge=0)] = None,
    has_salary: bool | None = None,
    posted_within_days: Annotated[int | None, Query(ge=1, le=365)] = None,
    sort: SortField = "relevance",
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> SearchResult:
    """Full-text search with the same filters as the listing endpoint."""
    request = SearchRequest(
        query=q,
        skills=tuple(skill or ()),
        require_all_skills=require_all_skills,
        company=company,
        country_code=country_code,
        city=city,
        remote_types=(str(remote_type),) if remote_type else (),
        experience_levels=(str(experience_level),) if experience_level else (),
        segments=(str(segment),) if segment else (),
        salary_min=salary_min,
        has_salary=has_salary,
        posted_within_days=posted_within_days,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    result = await jobs.search(request)
    return SearchResult(
        items=[JobSummary.from_job(job, score=result.scores.get(job.id)) for job in result.jobs],
        meta=PageMeta(total=result.total, limit=limit, offset=offset, returned=len(result.jobs)),
        search=SearchMeta(backend=result.backend, took_ms=result.took_ms, query=q),
    )


@router.get("/suggest", summary="Title suggestions")
async def suggest(
    jobs: JobServiceDep,
    prefix: Annotated[str, Query(min_length=2, max_length=64)],
    limit: Annotated[int, Query(ge=1, le=25)] = 10,
) -> dict[str, list[str]]:
    """Auto-completion for normalized job titles."""
    return {"suggestions": await jobs.suggest(prefix, limit=limit)}


@router.get("/facets", summary="Filter facets")
async def facets(jobs: JobServiceDep) -> dict[str, Any]:
    """Counts per filter value, for building the search UI."""
    return await jobs.facets()


@router.get("/{job_id}", response_model=JobDetail, summary="Get one posting")
async def get_job(jobs: JobServiceDep, job_id: JobId) -> JobDetail:
    """Return a single posting with its full description."""
    return JobDetail.from_job(await jobs.get(job_id))


@router.get("/{job_id}/similar", response_model=list[JobSummary], summary="Similar postings")
async def similar_jobs(
    jobs: JobServiceDep,
    job_id: JobId,
    limit: Annotated[int, Query(ge=1, le=25)] = 5,
) -> list[JobSummary]:
    """Postings that share the most skills with this one."""
    return [JobSummary.from_job(job) for job in await jobs.similar(job_id, limit=limit)]


__all__ = ["router"]
