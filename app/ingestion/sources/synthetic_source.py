"""A realistic synthetic job-market simulator.

Trend detection cannot be validated against a live feed - the answer changes
every day and nobody knows the ground truth. This generator produces a corpus
with *known* dynamics (Python and FastAPI rising, a legacy stack declining),
so the analytics layer can be tested for correctness rather than plausibility.

The output is ordinary :class:`RawJob` records, so synthetic data travels
through exactly the same cleaning, NLP and analytics code as real data.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from app.core.timeutils import utcnow
from app.ingestion.base import BaseJobSource, SourceContext
from app.models.enums import SourceKind
from app.models.raw import RawBatch, RawJob

# --------------------------------------------------------------------------- #
# Market shape
# --------------------------------------------------------------------------- #

COMPANIES: tuple[tuple[str, str], ...] = (
    ("Northwind Analytics", "Software"),
    ("Helios Data Systems", "Software"),
    ("Meridian Payments", "Fintech"),
    ("Arcadia Health", "Healthcare"),
    ("Bluepeak Logistics", "Logistics"),
    ("Quantum Retail Group", "Retail"),
    ("Vireo Energy", "Energy"),
    ("Cobalt Security", "Cybersecurity"),
    ("Lumen Media", "Media"),
    ("Orchard Labs", "Software"),
    ("Sable Insurance", "Insurance"),
    ("Ferrite Robotics", "Manufacturing"),
    ("Pinegrove Education", "Education"),
    ("Aster Cloud", "Cloud"),
    ("Tessera Games", "Gaming"),
    ("Kestrel Telecom", "Telecommunications"),
    ("Solstice Travel", "Travel"),
    ("Ironbark Consulting", "Consulting"),
    ("Nimbus Marketplace", "E-commerce"),
    ("Varda Biotech", "Biotech"),
)

LOCATIONS: tuple[tuple[str, str, str, str], ...] = (
    ("Berlin", "Berlin", "DE", "Germany"),
    ("Munich", "Bavaria", "DE", "Germany"),
    ("Amsterdam", "North Holland", "NL", "Netherlands"),
    ("London", "England", "GB", "United Kingdom"),
    ("Manchester", "England", "GB", "United Kingdom"),
    ("Paris", "Ile-de-France", "FR", "France"),
    ("Madrid", "Madrid", "ES", "Spain"),
    ("Lisbon", "Lisbon", "PT", "Portugal"),
    ("Warsaw", "Masovia", "PL", "Poland"),
    ("Dubai", "Dubai", "AE", "United Arab Emirates"),
    ("Toronto", "Ontario", "CA", "Canada"),
    ("New York", "New York", "US", "United States"),
    ("Austin", "Texas", "US", "United States"),
    ("San Francisco", "California", "US", "United States"),
    ("Seattle", "Washington", "US", "United States"),
    ("Bengaluru", "Karnataka", "IN", "India"),
    ("Singapore", "Singapore", "SG", "Singapore"),
    ("Sydney", "New South Wales", "AU", "Australia"),
    ("Stockholm", "Stockholm", "SE", "Sweden"),
    ("Tallinn", "Harju", "EE", "Estonia"),
)

#: Purchasing-power-ish multiplier applied to the base salary band.
COUNTRY_SALARY_FACTOR: dict[str, float] = {
    "US": 1.45,
    "CA": 1.05,
    "GB": 1.05,
    "DE": 1.0,
    "NL": 1.0,
    "FR": 0.92,
    "SE": 1.0,
    "ES": 0.72,
    "PT": 0.65,
    "PL": 0.6,
    "EE": 0.62,
    "AE": 0.95,
    "SG": 1.0,
    "AU": 1.1,
    "IN": 0.32,
}

SENIORITY_BANDS: tuple[tuple[str, float, float, float], ...] = (
    # label, weight, base_min, base_max (EUR, annual)
    ("Intern", 0.03, 12_000, 20_000),
    ("Junior", 0.15, 34_000, 48_000),
    ("", 0.34, 48_000, 68_000),  # mid-level jobs usually carry no prefix
    ("Senior", 0.32, 68_000, 95_000),
    ("Lead", 0.10, 85_000, 115_000),
    ("Principal", 0.04, 100_000, 140_000),
    ("Engineering Manager", 0.02, 95_000, 130_000),
)


@dataclass(frozen=True, slots=True)
class RoleTemplate:
    """A family of postings sharing a title pattern and a skill pool."""

    family: str
    titles: tuple[str, ...]
    core_skills: tuple[str, ...]
    optional_skills: tuple[str, ...]
    salary_factor: float = 1.0


ROLES: tuple[RoleTemplate, ...] = (
    RoleTemplate(
        family="Backend Engineering",
        titles=(
            "Python Developer",
            "Backend Engineer",
            "Backend Python Engineer",
            "Software Engineer - Python",
        ),
        core_skills=("Python", "PostgreSQL", "Docker", "REST"),
        optional_skills=(
            "FastAPI",
            "Django",
            "Flask",
            "Redis",
            "Kubernetes",
            "AWS",
            "Celery",
            "gRPC",
            "Go",
            "Rust",
        ),
        salary_factor=1.0,
    ),
    RoleTemplate(
        family="Frontend Engineering",
        titles=(
            "Frontend Developer",
            "React Engineer",
            "UI Engineer",
            "Frontend Software Engineer",
        ),
        core_skills=("JavaScript", "React", "CSS"),
        optional_skills=("TypeScript", "Next.js", "GraphQL", "Webpack", "Jest", "Tailwind"),
        salary_factor=0.95,
    ),
    RoleTemplate(
        family="Data Engineering",
        titles=("Data Engineer", "Analytics Engineer", "Big Data Engineer", "ETL Developer"),
        core_skills=("Python", "SQL", "Airflow"),
        optional_skills=(
            "Spark",
            "dbt",
            "Kafka",
            "Snowflake",
            "Pandas",
            "Polars",
            "AWS",
            "Databricks",
        ),
        salary_factor=1.08,
    ),
    RoleTemplate(
        family="Data Science",
        titles=("Data Scientist", "Machine Learning Engineer", "Applied Scientist"),
        core_skills=("Python", "Pandas", "Statistics"),
        optional_skills=("PyTorch", "TensorFlow", "scikit-learn", "MLflow", "SQL", "Spark"),
        salary_factor=1.12,
    ),
    RoleTemplate(
        family="DevOps",
        titles=("DevOps Engineer", "Site Reliability Engineer", "Platform Engineer"),
        core_skills=("Linux", "Docker", "CI/CD"),
        optional_skills=(
            "Kubernetes",
            "Terraform",
            "AWS",
            "Azure",
            "GCP",
            "Prometheus",
            "Ansible",
            "Jenkins",
        ),
        salary_factor=1.1,
    ),
    RoleTemplate(
        family="Cybersecurity",
        titles=("Security Engineer", "Application Security Engineer", "SOC Analyst"),
        core_skills=("Security", "Linux", "Networking"),
        optional_skills=("Python", "SIEM", "Kubernetes", "Threat Modeling", "Penetration Testing"),
        salary_factor=1.15,
    ),
    RoleTemplate(
        family="Mobile Development",
        titles=("iOS Engineer", "Android Engineer", "Mobile Developer"),
        core_skills=("Swift", "Kotlin"),
        optional_skills=("React Native", "Flutter", "GraphQL", "Firebase"),
        salary_factor=1.0,
    ),
    RoleTemplate(
        family="Quality Assurance",
        titles=("QA Engineer", "Test Automation Engineer", "SDET"),
        core_skills=("Testing", "Selenium"),
        optional_skills=("Python", "Cypress", "Playwright", "CI/CD", "Java"),
        salary_factor=0.85,
    ),
    RoleTemplate(
        family="Legacy Systems",
        titles=("COBOL Developer", "Mainframe Engineer", "VB.NET Developer"),
        core_skills=("COBOL", "Mainframe"),
        optional_skills=("VB.NET", "Oracle", "JCL"),
        salary_factor=0.9,
    ),
)

#: Skills whose demand is deliberately animated over the simulated period.
#: The value is ``(weight_at_start, weight_at_end)``; 1.0 means "unchanged".
TREND_PROFILE: dict[str, tuple[float, float]] = {
    "FastAPI": (0.35, 1.60),
    "Python": (0.85, 1.25),
    "Kubernetes": (0.60, 1.45),
    "Rust": (0.30, 1.20),
    "Polars": (0.15, 1.10),
    "dbt": (0.40, 1.30),
    "COBOL": (1.40, 0.35),
    "Mainframe": (1.35, 0.40),
    "VB.NET": (1.30, 0.30),
    "Jenkins": (1.20, 0.70),
}

REMOTE_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("remote", 0.34),
    ("hybrid", 0.29),
    ("onsite", 0.31),
    ("", 0.06),  # unspecified
)

EMPLOYMENT_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("Full-time", 0.78),
    ("Contract", 0.12),
    ("Part-time", 0.05),
    ("Internship", 0.03),
    ("Freelance", 0.02),
)

DESCRIPTION_TEMPLATE = """About {company}
{company} is a {industry_lower} company building products used by teams across {country}.

The role
We are hiring a {title} to join our {family} team{location_clause}. You will design, build and
operate services that process production traffic every day, and you will work closely with
product, data and platform engineers.

Responsibilities
- Design, implement and maintain {primary} services in production.
- Improve reliability, observability and performance of existing systems.
- Review code, mentor colleagues and contribute to technical decisions.
- Collaborate with product management on scope and delivery.

Requirements
- {experience_clause}
- Strong experience with {core_list}.
- Solid understanding of software engineering fundamentals, testing and code review.
- Good written and spoken English.

Nice to have
- Experience with {optional_list}.
- Exposure to {extra_skill}.

What we offer
- {remote_sentence}
- A budget for conferences, books and certifications.
- {salary_sentence}
"""


@dataclass(slots=True)
class SyntheticConfig:
    """Knobs of the market simulator."""

    count: int = 1000
    days: int = 120
    seed: int = 20240101
    remote_boost: float = 1.0
    salary_disclosure_rate: float = 0.55
    duplicate_rate: float = 0.06
    malformed_rate: float = 0.02
    reference_time: datetime | None = None
    trends: dict[str, tuple[float, float]] = field(default_factory=lambda: dict(TREND_PROFILE))


class SyntheticJobGenerator:
    """Deterministic generator of realistic postings."""

    def __init__(self, config: SyntheticConfig | None = None) -> None:
        self.config = config or SyntheticConfig()
        self._random = random.Random(self.config.seed)
        self._reference = self.config.reference_time or utcnow()

    def generate(self, count: int | None = None) -> list[RawJob]:
        """Produce ``count`` postings spread across the configured period."""
        total = count if count is not None else self.config.count
        jobs: list[RawJob] = []
        for index in range(total):
            progress = index / max(total - 1, 1)
            job = self._build_job(index, progress)
            jobs.append(job)
            # A share of postings is re-published verbatim by another source,
            # which is exactly what the deduplication engine has to catch.
            if self._random.random() < self.config.duplicate_rate and len(jobs) < total:
                jobs.append(self._republish(job, index))
        return jobs[:total] if count is not None else jobs

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _weighted_choice(self, options: tuple[tuple[Any, float], ...]) -> Any:
        population = [option for option, _ in options]
        weights = [weight for _, weight in options]
        return self._random.choices(population, weights=weights, k=1)[0]

    def _trend_weight(self, skill: str, progress: float) -> float:
        start, end = self.config.trends.get(skill, (1.0, 1.0))
        return start + (end - start) * progress

    def _pick_role(self, progress: float) -> RoleTemplate:
        # A role's popularity follows the trend of the skills that define it.
        # Only core skills count: an optional technology should not drag a
        # whole discipline up or down.
        weights = []
        for role in ROLES:
            weight = sum(self._trend_weight(skill, progress) for skill in role.core_skills) / len(
                role.core_skills
            )
            weights.append(max(weight, 0.05))
        return self._random.choices(ROLES, weights=weights, k=1)[0]

    def _pick_skills(self, role: RoleTemplate, progress: float) -> list[str]:
        chosen = list(role.core_skills)
        for skill in role.optional_skills:
            probability = min(0.85, 0.35 * self._trend_weight(skill, progress))
            if self._random.random() < probability:
                chosen.append(skill)
        if not chosen:
            chosen = list(role.core_skills)
        return chosen

    def _build_job(self, index: int, progress: float) -> RawJob:
        role = self._pick_role(progress)
        seniority, _, base_min, base_max = self._weighted_seniority()
        company, industry = self._random.choice(COMPANIES)
        city, region, country_code, country = self._random.choice(LOCATIONS)
        remote = self._pick_remote()
        skills = self._pick_skills(role, progress)
        title_core = self._random.choice(role.titles)
        title = f"{seniority} {title_core}".strip()

        published_at = self._published_at(progress)
        factor = COUNTRY_SALARY_FACTOR.get(country_code, 0.9) * role.salary_factor
        salary_min = round(base_min * factor / 500) * 500
        salary_max = round(base_max * factor / 500) * 500
        discloses_salary = self._random.random() < self.config.salary_disclosure_rate
        salary = f"{salary_min:,} - {salary_max:,} EUR per year" if discloses_salary else None

        location = "Remote" if remote == "remote" else f"{city}, {country}"
        description = self._description(
            role=role,
            title=title,
            company=company,
            industry=industry,
            country=country,
            city=city,
            remote=remote,
            skills=skills,
            seniority=seniority,
            salary=salary,
        )

        # A small share of records is deliberately broken so the validation and
        # data-quality layers have something real to catch.
        if self._random.random() < self.config.malformed_rate:
            description = description[: self._random.randint(0, 40)]

        return RawJob(
            source="synthetic",
            source_kind=SourceKind.SYNTHETIC,
            source_job_id=f"syn-{self.config.seed}-{index:07d}",
            url=f"https://jobs.example.com/{_slug(company)}/{_slug(title)}-{index}",
            title=title,
            description=description,
            company=company,
            location=location,
            salary=salary,
            employment_type=self._weighted_choice(EMPLOYMENT_WEIGHTS),
            remote_status=remote or None,
            industry=industry,
            published_at=published_at,
            raw_payload={
                "generated": True,
                "role_family": role.family,
                "skills": skills,
                "country_code": country_code,
                "region": region,
            },
            metadata={"adapter": "synthetic", "progress": round(progress, 4)},
        )

    def _republish(self, job: RawJob, index: int) -> RawJob:
        """Create a near-identical repost, as aggregators do in the real world."""
        suffix = self._random.choice(["", " (m/f/d)", " - Remote", " | Urgent"])
        return job.model_copy(
            update={
                "source": "synthetic_aggregator",
                "source_job_id": f"agg-{self.config.seed}-{index:07d}",
                "title": f"{job.title}{suffix}",
                "url": f"https://aggregator.example.org/listing/{index}",
                "published_at": (job.published_at or self._reference)
                + timedelta(hours=self._random.randint(1, 72)),
                "metadata": {**job.metadata, "republished_from": job.source_job_id},
            }
        )

    def _weighted_seniority(self) -> tuple[str, float, float, float]:
        weights = [band[1] for band in SENIORITY_BANDS]
        return self._random.choices(SENIORITY_BANDS, weights=weights, k=1)[0]

    def _pick_remote(self) -> str:
        options = tuple(
            (value, weight * (self.config.remote_boost if value == "remote" else 1.0))
            for value, weight in REMOTE_WEIGHTS
        )
        return str(self._weighted_choice(options))

    def _published_at(self, progress: float) -> datetime:
        """Spread postings over the window, weighted towards recent days."""
        span_days = self.config.days
        day_offset = span_days - int(progress * span_days)
        jitter_hours = self._random.randint(0, 23)
        return self._reference - timedelta(days=day_offset, hours=jitter_hours)

    def _description(
        self,
        *,
        role: RoleTemplate,
        title: str,
        company: str,
        industry: str,
        country: str,
        city: str,
        remote: str,
        skills: list[str],
        seniority: str,
        salary: str | None,
    ) -> str:
        core = skills[: max(3, len(role.core_skills))]
        optional = skills[len(core) :] or list(role.optional_skills[:2])
        extra = self._random.choice(role.optional_skills)
        years = {
            "Intern": "You are studying towards a degree or have just graduated",
            "Junior": "1-2 years of professional experience",
            "": "3-5 years of professional experience",
            "Senior": "5+ years of professional experience",
            "Lead": "7+ years of experience including technical leadership",
            "Principal": "10+ years of experience shaping technical strategy",
            "Engineering Manager": "5+ years of engineering experience and 2+ years leading teams",
        }[seniority]
        remote_sentence = {
            "remote": "Fully remote work with a flexible schedule.",
            "hybrid": f"Hybrid working model with 2 days per week in our {city} office.",
            "onsite": f"On-site collaboration in our {city} office.",
            "": "Flexible working arrangements.",
        }[remote]
        location_clause = "" if remote == "remote" else f" in {city}"
        salary_sentence = (
            f"Transparent salary range: {salary}."
            if salary
            else "Compensation is discussed during the first interview."
        )
        return DESCRIPTION_TEMPLATE.format(
            company=company,
            industry_lower=industry.lower(),
            country=country,
            title=title,
            family=role.family,
            location_clause=location_clause,
            primary=core[0] if core else "backend",
            experience_clause=years,
            core_list=", ".join(core),
            optional_list=", ".join(optional[:4]) or "modern tooling",
            extra_skill=extra,
            remote_sentence=remote_sentence,
            salary_sentence=salary_sentence,
        )


def _slug(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")


class SyntheticJobSource(BaseJobSource):
    """Exposes the generator through the standard source interface."""

    kind = SourceKind.SYNTHETIC

    def __init__(self, *, name: str = "synthetic", options: dict[str, Any] | None = None) -> None:
        super().__init__(name=name, options=options)
        self._config = SyntheticConfig(
            count=int(self.options.get("count", 500)),
            days=int(self.options.get("days", 120)),
            seed=int(self.options.get("seed", 20240101)),
            remote_boost=float(self.options.get("remote_boost", 1.0)),
            salary_disclosure_rate=float(self.options.get("salary_disclosure_rate", 0.55)),
            duplicate_rate=float(self.options.get("duplicate_rate", 0.06)),
            malformed_rate=float(self.options.get("malformed_rate", 0.02)),
        )

    async def fetch_jobs(self, context: SourceContext) -> RawBatch:
        """Generate a deterministic batch, continuing from the stored cursor."""
        offset = int(context.cursor) if context.cursor and context.cursor.isdigit() else 0
        wanted = min(context.limit, self._config.count)
        # Offsetting the seed keeps successive pages distinct while staying
        # fully reproducible.
        generator = SyntheticJobGenerator(replace(self._config, seed=self._config.seed + offset))
        jobs = generator.generate(wanted)
        cursor = str(offset + len(jobs))
        return self.build_batch(jobs, cursor=cursor, has_more=False)


__all__ = [
    "COMPANIES",
    "LOCATIONS",
    "ROLES",
    "TREND_PROFILE",
    "SyntheticConfig",
    "SyntheticJobGenerator",
    "SyntheticJobSource",
]
