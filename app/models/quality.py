"""Data-quality value objects.

Quality is measured per batch, per source and per field so that a regression in
one adapter does not disappear inside a global average.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.core.timeutils import utcnow
from app.models.enums import QUALITY_DIMENSION_WEIGHTS, QualityDimension

Score = Annotated[float, Field(ge=0.0, le=1.0)]


class FieldQuality(BaseModel):
    """Completeness/validity of a single field within a batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    present: int = 0
    missing: int = 0
    invalid: int = 0

    @property
    def total(self) -> int:
        return self.present + self.missing

    @property
    def completeness(self) -> float:
        return self.present / self.total if self.total else 0.0

    @property
    def validity(self) -> float:
        if not self.present:
            return 0.0
        return max(0.0, (self.present - self.invalid) / self.present)


class QualityReport(BaseModel):
    """Aggregated quality assessment for one ingestion batch."""

    model_config = ConfigDict(extra="forbid")

    source: str
    ingestion_run_id: str | None = None
    evaluated_at: datetime = Field(default_factory=utcnow)
    records_evaluated: int = 0
    dimensions: dict[QualityDimension, float] = Field(default_factory=dict)
    fields: list[FieldQuality] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)

    @property
    def score(self) -> float:
        """Weighted quality score in ``[0, 1]``."""
        if not self.dimensions:
            return 0.0
        total_weight = 0.0
        accumulated = 0.0
        for dimension, value in self.dimensions.items():
            weight = QUALITY_DIMENSION_WEIGHTS.get(dimension, 0.0)
            accumulated += weight * max(0.0, min(1.0, value))
            total_weight += weight
        return accumulated / total_weight if total_weight else 0.0

    @property
    def grade(self) -> str:
        """Letter grade, handy for dashboards and CLI output."""
        score = self.score
        if score >= 0.9:
            return "A"
        if score >= 0.8:
            return "B"
        if score >= 0.7:
            return "C"
        if score >= 0.6:
            return "D"
        return "F"

    def worst_dimensions(self, limit: int = 3) -> list[tuple[QualityDimension, float]]:
        """Lowest scoring dimensions first."""
        return sorted(self.dimensions.items(), key=lambda item: item[1])[:limit]


class QualityEvent(BaseModel):
    """A single quality violation worth persisting."""

    model_config = ConfigDict(extra="forbid")

    source: str
    dimension: QualityDimension
    field: str | None = None
    severity: str = "warning"
    message: str
    value: float | None = None
    threshold: float | None = None
    job_id: str | None = None
    ingestion_run_id: str | None = None
    occurred_at: datetime = Field(default_factory=utcnow)


__all__ = ["FieldQuality", "QualityEvent", "QualityReport", "Score"]
