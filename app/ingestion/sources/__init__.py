"""Concrete source adapters."""

from __future__ import annotations

from app.ingestion.sources.api_source import ApiJobSource
from app.ingestion.sources.company_career import CompanyCareerSource
from app.ingestion.sources.dataset_source import DatasetSource
from app.ingestion.sources.rss_source import RssJobSource
from app.ingestion.sources.synthetic_source import (
    SyntheticConfig,
    SyntheticJobGenerator,
    SyntheticJobSource,
)

__all__ = [
    "ApiJobSource",
    "CompanyCareerSource",
    "DatasetSource",
    "RssJobSource",
    "SyntheticConfig",
    "SyntheticJobGenerator",
    "SyntheticJobSource",
]
