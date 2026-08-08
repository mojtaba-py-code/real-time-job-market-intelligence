"""Company-name normalization.

``Acme GmbH``, ``ACME Gmbh.``, ``Acme (via TalentPartners)`` and ``Acme Inc.``
are one employer. Collapsing them is what makes company analytics meaningful,
so the normalizer removes legal suffixes, recruiter annotations and casing
noise while keeping a readable display name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.text import canonical_key, clean_text, slugify

#: Legal-form suffixes stripped from the end of a company name.
LEGAL_SUFFIXES: tuple[str, ...] = (
    "incorporated",
    "inc",
    "corporation",
    "corp",
    "company",
    "co",
    "limited",
    "ltd",
    "llc",
    "llp",
    "lp",
    "plc",
    "pllc",
    "gmbh",
    "mbh",
    "ug",
    "kg",
    "gbr",
    "ag",
    "se",
    "bv",
    "nv",
    "cv",
    "vof",
    "sa",
    "sas",
    "sarl",
    "sl",
    "srl",
    "spa",
    "sprl",
    "snc",
    "ab",
    "asa",
    "as",
    "aps",
    "oy",
    "oyj",
    "kft",
    "zrt",
    "sp z oo",
    "spzoo",
    "doo",
    "pty",
    "pty ltd",
    "pte",
    "pte ltd",
    "sdn bhd",
    "bhd",
    "kk",
    "gk",
    "kabushiki kaisha",
)

#: Recruiter/aggregator annotations that are not part of the employer name.
_VIA_RE = re.compile(
    r"\s*[\(\[]?\s*(?:via|through|on behalf of|c/o|recruiting for|hiring for)"
    r"\s+[^\)\]]*[\)\]]?\s*$",
    re.IGNORECASE,
)
_TRAILING_PUNCT_RE = re.compile(r"[\s.,;:\-\u2013\u2014/|]+$")
_MULTISPACE_RE = re.compile(r"\s{2,}")
_ALL_CAPS_RE = re.compile(r"^[A-Z0-9&.\s-]+$")

DEFAULT_COMPANY_NAME = "Unknown"
DEFAULT_COMPANY_SLUG = "unknown"


@dataclass(frozen=True, slots=True)
class NormalizedCompany:
    """A cleaned employer identity."""

    name: str
    slug: str
    confidence: float

    @property
    def is_known(self) -> bool:
        return self.slug != DEFAULT_COMPANY_SLUG


class CompanyNormalizer:
    """Cleans and canonicalises employer names."""

    def __init__(self, *, extra_suffixes: tuple[str, ...] = ()) -> None:
        self._suffixes = tuple(sorted({*LEGAL_SUFFIXES, *extra_suffixes}, key=len, reverse=True))

    def normalize(self, raw: str | None) -> NormalizedCompany:
        """Return the display name and slug for an employer."""
        text = clean_text(raw, max_length=256)
        if not text:
            return NormalizedCompany(DEFAULT_COMPANY_NAME, DEFAULT_COMPANY_SLUG, 0.0)

        text = _VIA_RE.sub("", text)
        text = _MULTISPACE_RE.sub(" ", text).strip()
        text = _TRAILING_PUNCT_RE.sub("", text)
        if not text:
            return NormalizedCompany(DEFAULT_COMPANY_NAME, DEFAULT_COMPANY_SLUG, 0.0)

        stripped = self._strip_suffixes(text)
        display = self._prettify(stripped or text)
        slug = slugify(stripped or text) or DEFAULT_COMPANY_SLUG

        confidence = 0.9 if stripped else 0.7
        if len(display) < 2:
            return NormalizedCompany(DEFAULT_COMPANY_NAME, DEFAULT_COMPANY_SLUG, 0.0)
        return NormalizedCompany(display[:256], slug[:128], confidence)

    def _strip_suffixes(self, text: str) -> str:
        """Remove one or more trailing legal-form tokens."""
        current = text
        for _ in range(3):  # e.g. "Acme Holding GmbH & Co. KG"
            words = current.split()
            if len(words) < 2:
                break
            removed = False
            for suffix in self._suffixes:
                span = len(suffix.split())
                if len(words) > span and canonical_key(" ".join(words[-span:])) == suffix:
                    trimmed = " ".join(words[:-span])
                    current = _TRAILING_PUNCT_RE.sub("", trimmed).rstrip(" &")
                    removed = True
                    break
            if not removed:
                break
        return current.strip()

    @staticmethod
    def _prettify(text: str) -> str:
        """Title-case names that arrive fully upper-cased, leave others alone."""
        if len(text) > 3 and _ALL_CAPS_RE.match(text) and text.upper() == text:
            return " ".join(
                word if len(word) <= 3 and word.isupper() else word.capitalize()
                for word in text.split()
            )
        return text


__all__ = [
    "DEFAULT_COMPANY_NAME",
    "DEFAULT_COMPANY_SLUG",
    "LEGAL_SUFFIXES",
    "CompanyNormalizer",
    "NormalizedCompany",
]
