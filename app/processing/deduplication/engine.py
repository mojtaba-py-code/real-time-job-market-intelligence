"""The deduplication engine.

Job boards repost, aggregators republish and companies re-list. Treating every
fetched record as a new opening would inflate every metric the platform
produces, so identity is resolved in four escalating stages:

1. **Exact source identity** - ``(source, source_job_id)``. Cheap, exact.
2. **Canonical URL** - the same posting reached through different tracking
   parameters or hosts.
3. **Content fingerprint** - identical company, title, location and body.
4. **Near duplicate** - MinHash/LSH similarity above a configurable threshold,
   which catches reposts with a changed title or a reworded intro.

Every stage is optional and every threshold is configuration, because the right
aggressiveness depends on which sources are enabled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.core.config import DeduplicationSettings
from app.core.hashing import content_fingerprint, url_fingerprint
from app.core.logging import get_logger
from app.core.text import shingles, tokenize
from app.core.timeutils import days_ago
from app.models.enums import DuplicateKind
from app.models.job import NormalizedJob
from app.processing.deduplication.minhash import LshIndex, MinHasher, jaccard_similarity

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DuplicateVerdict:
    """The outcome of checking one posting."""

    kind: DuplicateKind = DuplicateKind.NONE
    duplicate_of: str | None = None
    similarity: float = 0.0

    @property
    def is_duplicate(self) -> bool:
        return self.kind is not DuplicateKind.NONE


@dataclass(slots=True)
class DeduplicationStats:
    """Counters for one batch."""

    checked: int = 0
    duplicates: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)

    def record(self, verdict: DuplicateVerdict) -> None:
        self.checked += 1
        if verdict.is_duplicate:
            self.duplicates += 1
            key = str(verdict.kind)
            self.by_kind[key] = self.by_kind.get(key, 0) + 1

    @property
    def duplicate_rate(self) -> float:
        return self.duplicates / self.checked if self.checked else 0.0


@dataclass(slots=True)
class KnownPosting:
    """A previously stored posting, as far as deduplication cares."""

    job_id: str
    content_hash: str = ""
    url_fingerprint: str = ""
    signature: list[int] = field(default_factory=list)
    natural_key: tuple[str, str] | None = None


class DeduplicationEngine:
    """Assigns each incoming posting an identity relative to what we know."""

    def __init__(self, settings: DeduplicationSettings | None = None) -> None:
        self._settings = settings or DeduplicationSettings()
        self._hasher = MinHasher(self._settings.minhash_permutations)
        self._index = LshIndex(
            bands=self._settings.lsh_bands, permutations=self._settings.minhash_permutations
        )
        self._by_content: dict[str, str] = {}
        self._by_url: dict[str, str] = {}
        self._by_natural_key: dict[tuple[str, str], str] = {}

    # ------------------------------------------------------------------ #
    # Fingerprinting
    # ------------------------------------------------------------------ #
    def fingerprint(self, job: NormalizedJob) -> NormalizedJob:
        """Attach the content hash, URL fingerprint and MinHash signature."""
        content = content_fingerprint(
            company=job.company_name,
            title=job.normalized_title or job.title,
            location=job.location.display(),
            description=job.description,
        )
        job.content_hash = content
        job.url_fingerprint = url_fingerprint(job.canonical_url) or None
        job.similarity_signature = self.signature_for(job)
        return job

    def signature_for(self, job: NormalizedJob) -> list[int]:
        """MinHash signature of the posting's comparable text."""
        if not self._settings.enabled:
            return []
        material = " ".join(part for part in (job.title, job.company_name, job.description) if part)
        tokens = shingles(tokenize(material), self._settings.shingle_size)
        return self._hasher.signature(tokens)

    # ------------------------------------------------------------------ #
    # Known-posting index
    # ------------------------------------------------------------------ #
    def prime(self, known: list[KnownPosting]) -> None:
        """Load the comparison window (typically the last 30 days)."""
        self.reset()
        for posting in known:
            self.remember(posting)

    def remember(self, posting: KnownPosting) -> None:
        """Add one posting to the in-memory index."""
        if posting.content_hash:
            self._by_content.setdefault(posting.content_hash, posting.job_id)
        if posting.url_fingerprint:
            self._by_url.setdefault(posting.url_fingerprint, posting.job_id)
        if posting.natural_key:
            self._by_natural_key.setdefault(posting.natural_key, posting.job_id)
        if posting.signature:
            self._index.add(posting.job_id, posting.signature)

    def remember_job(self, job: NormalizedJob) -> None:
        """Index a posting that was just accepted, so the batch self-deduplicates."""
        self.remember(
            KnownPosting(
                job_id=job.id,
                content_hash=job.content_hash,
                url_fingerprint=job.url_fingerprint or "",
                signature=list(job.similarity_signature),
                natural_key=(job.source, job.source_job_id),
            )
        )

    def reset(self) -> None:
        """Forget everything (used between runs and by tests)."""
        self._by_content.clear()
        self._by_url.clear()
        self._by_natural_key.clear()
        self._index.clear()

    @property
    def window_start(self) -> datetime:
        """Oldest posting the comparison window should include."""
        return days_ago(self._settings.candidate_window_days)

    @property
    def indexed_count(self) -> int:
        return len(self._index)

    # ------------------------------------------------------------------ #
    # Checking
    # ------------------------------------------------------------------ #
    def check(self, job: NormalizedJob) -> DuplicateVerdict:
        """Classify a posting against everything currently indexed."""
        if not self._settings.enabled:
            return DuplicateVerdict()

        natural = self._by_natural_key.get((job.source, job.source_job_id))
        if natural and natural != job.id:
            return DuplicateVerdict(DuplicateKind.EXACT_SOURCE, natural, 1.0)

        if job.url_fingerprint:
            match = self._by_url.get(job.url_fingerprint)
            if match and match != job.id:
                return DuplicateVerdict(DuplicateKind.URL, match, 1.0)

        if job.content_hash:
            match = self._by_content.get(job.content_hash)
            if match and match != job.id:
                return DuplicateVerdict(DuplicateKind.CONTENT, match, 1.0)

        if job.similarity_signature:
            for candidate_id, score in self._index.query(
                job.similarity_signature, threshold=self._settings.near_duplicate_threshold
            ):
                if candidate_id != job.id:
                    return DuplicateVerdict(DuplicateKind.NEAR, candidate_id, round(score, 4))

        return DuplicateVerdict()

    def process_batch(
        self, jobs: list[NormalizedJob]
    ) -> tuple[
        list[NormalizedJob], list[tuple[NormalizedJob, DuplicateVerdict]], DeduplicationStats
    ]:
        """Split a batch into unique postings and duplicates.

        Accepted postings are indexed as we go, so two identical records inside
        the *same* batch are also caught.
        """
        unique: list[NormalizedJob] = []
        duplicates: list[tuple[NormalizedJob, DuplicateVerdict]] = []
        stats = DeduplicationStats()

        for job in jobs:
            if not job.content_hash:
                self.fingerprint(job)
            verdict = self.check(job)
            stats.record(verdict)
            if verdict.is_duplicate:
                job.duplicate_of = verdict.duplicate_of
                job.duplicate_kind = verdict.kind
                duplicates.append((job, verdict))
            else:
                unique.append(job)
                self.remember_job(job)
        return unique, duplicates, stats

    def similarity(self, left: NormalizedJob, right: NormalizedJob) -> float:
        """Estimated similarity between two postings (diagnostics and tests)."""
        return jaccard_similarity(
            left.similarity_signature or self.signature_for(left),
            right.similarity_signature or self.signature_for(right),
        )


__all__ = [
    "DeduplicationEngine",
    "DeduplicationStats",
    "DuplicateVerdict",
    "KnownPosting",
]
