"""Deterministic hashing and URL canonicalisation.

These primitives back the deduplication engine, so they must be stable across
processes and releases. ``hash()`` from the standard library is deliberately
avoided because Python randomises string hashing per process.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.core.text import canonical_key, clean_text

#: Query parameters that never change which job a URL points at.
TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "gclid",
        "fbclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "ref",
        "referrer",
        "source",
        "trk",
        "trkinfo",
        "position",
        "pageNum",
        "sessionid",
        "sid",
    }
)

_DEFAULT_PORTS = {"http": "80", "https": "443"}


def sha256_hex(*parts: str) -> str:
    """SHA-256 over a null-separated concatenation of ``parts``."""
    digest = hashlib.sha256()
    digest.update("\x00".join(parts).encode("utf-8", errors="replace"))
    return digest.hexdigest()


def short_hash(*parts: str, length: int = 16) -> str:
    """A shortened SHA-256 digest, useful for keys and identifiers."""
    return sha256_hex(*parts)[:length]


def normalize_url(url: str | None) -> str:
    """Canonicalise a URL so that cosmetic variants collapse into one key.

    Lowercases the host, drops default ports, removes tracking parameters,
    sorts the remaining query string and strips fragments.
    """
    if not url:
        return ""
    raw = url.strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw.lstrip('/')}"

    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.lower()

    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    netloc = host
    if parts.port and str(parts.port) != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    query_items = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=False)
        if key.lower() not in TRACKING_PARAMS
    ]
    query = urlencode(sorted(query_items))

    return urlunsplit((scheme, netloc, path, query, ""))


def url_fingerprint(url: str | None) -> str:
    """Stable hash of a canonicalised URL (empty string when there is no URL)."""
    normalized = normalize_url(url)
    return sha256_hex("url", normalized) if normalized else ""


def content_fingerprint(
    *,
    company: str | None,
    title: str | None,
    location: str | None,
    description: str | None,
    description_chars: int = 4000,
) -> str:
    """Fingerprint the semantic identity of a posting.

    Two postings with the same company, title, location and (truncated)
    description body are considered the same opening even if they arrived from
    different sources with different identifiers.
    """
    body = clean_text(description or "")[:description_chars]
    return sha256_hex(
        "content",
        canonical_key(company),
        canonical_key(title),
        canonical_key(location),
        canonical_key(body),
    )


def source_fingerprint(source: str, source_job_id: str) -> str:
    """Fingerprint the ``(source, source_job_id)`` natural key."""
    return sha256_hex("source", source.lower().strip(), source_job_id.strip())


def stable_bucket(value: str, buckets: int) -> int:
    """Map a string to a stable bucket index (used for sharding and sampling)."""
    if buckets <= 0:
        raise ValueError("buckets must be positive")
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % buckets


def hash_tokens(tokens: Iterable[str]) -> str:
    """Order-independent hash of a token collection."""
    return sha256_hex("tokens", *sorted(set(tokens)))


__all__ = [
    "TRACKING_PARAMS",
    "content_fingerprint",
    "hash_tokens",
    "normalize_url",
    "sha256_hex",
    "short_hash",
    "source_fingerprint",
    "stable_bucket",
    "url_fingerprint",
]
