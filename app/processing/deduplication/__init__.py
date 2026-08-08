"""Duplicate detection."""

from __future__ import annotations

from app.processing.deduplication.engine import (
    DeduplicationEngine,
    DeduplicationStats,
    DuplicateVerdict,
    KnownPosting,
)
from app.processing.deduplication.minhash import LshIndex, MinHasher, jaccard_similarity

__all__ = [
    "DeduplicationEngine",
    "DeduplicationStats",
    "DuplicateVerdict",
    "KnownPosting",
    "LshIndex",
    "MinHasher",
    "jaccard_similarity",
]
