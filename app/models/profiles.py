"""Candidate profiles used by the personal job-intelligence module.

Only job-market preferences are stored. The platform deliberately does not
accept CVs, contact details or any other personal information it does not need
in order to compute market fit.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.text import slugify
from app.core.timeutils import utcnow
from app.models.enums import ExperienceLevel, RemoteType


class CandidateProfile(BaseModel):
    """A target role plus the skills and constraints of one candidate."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, max_length=32)
    owner: str | None = Field(default=None, max_length=128)
    label: str = Field(default="default", min_length=1, max_length=64)

    target_role: str = Field(min_length=2, max_length=128)
    skills: list[str] = Field(default_factory=list, max_length=200)
    experience_years: float = Field(default=0.0, ge=0, le=60)
    seniority: ExperienceLevel = ExperienceLevel.UNKNOWN
    countries: list[str] = Field(default_factory=list, max_length=50)
    cities: list[str] = Field(default_factory=list, max_length=50)
    salary_min: float | None = Field(default=None, ge=0)
    salary_max: float | None = Field(default=None, ge=0)
    salary_currency: str = Field(default="USD", min_length=3, max_length=3)
    remote_preference: RemoteType = RemoteType.UNKNOWN

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _normalize(self) -> Self:
        object.__setattr__(self, "skills", sorted({slugify(s) for s in self.skills if s.strip()}))
        object.__setattr__(
            self, "countries", sorted({c.strip().upper()[:2] for c in self.countries if c.strip()})
        )
        object.__setattr__(self, "cities", sorted({c.strip() for c in self.cities if c.strip()}))
        if (
            self.salary_min is not None
            and self.salary_max is not None
            and self.salary_min > self.salary_max
        ):
            raise ValueError("salary_min cannot exceed salary_max")
        return self

    @property
    def skill_set(self) -> frozenset[str]:
        return frozenset(self.skills)


__all__ = ["CandidateProfile"]
