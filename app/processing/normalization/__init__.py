"""Normalization of locations, salaries, companies and whole postings."""

from __future__ import annotations

from app.processing.normalization.company import CompanyNormalizer
from app.processing.normalization.location import LocationNormalizer
from app.processing.normalization.normalizer import JobNormalizer, NormalizationStats
from app.processing.normalization.salary import SalaryParser

__all__ = [
    "CompanyNormalizer",
    "JobNormalizer",
    "LocationNormalizer",
    "NormalizationStats",
    "SalaryParser",
]
