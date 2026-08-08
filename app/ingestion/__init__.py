"""Ingestion layer: source adapters, the outbound HTTP guard and scheduling."""

from __future__ import annotations

from app.ingestion.base import BaseJobSource, JobSource, SourceContext
from app.ingestion.http import SafeHttpClient, UrlGuard
from app.ingestion.registry import SourceDefinition, SourceRegistry

__all__ = [
    "BaseJobSource",
    "JobSource",
    "SafeHttpClient",
    "SourceContext",
    "SourceDefinition",
    "SourceRegistry",
    "UrlGuard",
]
