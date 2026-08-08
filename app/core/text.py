"""Text utilities shared by the cleaning, NLP and deduplication layers.

The functions here are deliberately dependency-free and deterministic: the
same input must always produce the same output, because content hashes and
fingerprints are derived from them.
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Iterable
from html.parser import HTMLParser
from typing import Literal

_WHITESPACE_RE = re.compile(r"[^\S\n]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ZERO_WIDTH_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060\ufeff\u00ad]")
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#.\-_/]*", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

_BLOCK_TAGS = frozenset(
    {"p", "div", "li", "br", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}
)
_SKIP_TAGS = frozenset({"script", "style", "noscript", "head", "template", "svg"})


class _TextExtractor(HTMLParser):
    """Collect visible text from an HTML fragment, dropping scripts and styles."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


def strip_html(value: str) -> str:
    """Remove HTML markup, keeping block structure as newlines."""
    if not value:
        return ""
    if "<" not in value and "&" not in value:
        return value
    parser = _TextExtractor()
    try:
        parser.feed(value)
        parser.close()
        text = parser.get_text()
    except Exception:
        text = re.sub(r"<[^>]+>", " ", value)
    return html.unescape(text)


def normalize_unicode(value: str, *, form: Literal["NFC", "NFD", "NFKC", "NFKD"] = "NFKC") -> str:
    """Apply Unicode normalization and strip invisible characters."""
    if not value:
        return ""
    normalized = unicodedata.normalize(form, value)
    normalized = _ZERO_WIDTH_RE.sub("", normalized)
    return _CONTROL_CHARS_RE.sub(" ", normalized)


def collapse_whitespace(value: str, *, keep_newlines: bool = False) -> str:
    """Collapse runs of whitespace, optionally preserving paragraph breaks."""
    if not value:
        return ""
    if keep_newlines:
        collapsed = _WHITESPACE_RE.sub(" ", value)
        collapsed = _MULTI_NEWLINE_RE.sub("\n\n", collapsed)
        return "\n".join(line.strip() for line in collapsed.split("\n")).strip()
    return " ".join(value.split())


def clean_text(
    value: str | None,
    *,
    keep_newlines: bool = False,
    max_length: int | None = None,
) -> str:
    """Full cleaning chain: HTML removal, Unicode normalization, whitespace fix."""
    if not value:
        return ""
    text = strip_html(value)
    text = normalize_unicode(text)
    text = collapse_whitespace(text, keep_newlines=keep_newlines)
    if max_length is not None and len(text) > max_length:
        text = text[:max_length].rstrip()
    return text


def strip_accents(value: str) -> str:
    """Remove diacritics (``Zürich`` -> ``Zurich``)."""
    decomposed = unicodedata.normalize("NFD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def canonical_key(value: str | None) -> str:
    """Lowercase, accent-free, punctuation-free key used for grouping."""
    if not value:
        return ""
    lowered = strip_accents(normalize_unicode(value)).lower()
    return _NON_ALNUM_RE.sub(" ", lowered).strip()


def slugify(value: str, *, max_length: int = 80) -> str:
    """Produce a URL/identifier-safe slug."""
    key = canonical_key(value).replace(" ", "-")
    key = re.sub(r"-{2,}", "-", key).strip("-")
    return key[:max_length]


def tokenize(value: str) -> list[str]:
    """Split text into lowercase technical tokens (keeps ``c++``, ``node.js``)."""
    if not value:
        return []
    text = normalize_unicode(value).lower()
    text = _URL_RE.sub(" ", text)
    return [match.group(0).strip("._-/") for match in _TOKEN_RE.finditer(text) if match.group(0)]


def shingles(tokens: Iterable[str], size: int = 5) -> set[str]:
    """Build overlapping token n-grams used for near-duplicate detection."""
    items = list(tokens)
    if size <= 1 or len(items) <= size:
        return set(items) if items else set()
    return {" ".join(items[i : i + size]) for i in range(len(items) - size + 1)}


def truncate(value: str, limit: int, *, suffix: str = "...") -> str:
    """Shorten a string to ``limit`` characters, appending an ellipsis."""
    if limit <= 0 or len(value) <= limit:
        return value
    if limit <= len(suffix):
        return value[:limit]
    return value[: limit - len(suffix)].rstrip() + suffix


def remove_urls(value: str) -> str:
    """Strip URLs (they add noise to skill extraction and fingerprints)."""
    return _URL_RE.sub(" ", value)


def contains_word(haystack: str, needle: str) -> bool:
    """Whole-word containment check that tolerates technical punctuation."""
    if not haystack or not needle:
        return False
    pattern = re.escape(needle.lower())
    return re.search(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", haystack.lower()) is not None


__all__ = [
    "canonical_key",
    "clean_text",
    "collapse_whitespace",
    "contains_word",
    "normalize_unicode",
    "remove_urls",
    "shingles",
    "slugify",
    "strip_accents",
    "strip_html",
    "tokenize",
    "truncate",
]
