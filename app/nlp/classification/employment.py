"""Employment-type and language classification.

Both are small, high-precision classifiers: the source usually states the
contract type explicitly, and the language of a posting is decided by a
stop-word profile rather than a heavyweight model, because the platform only
needs to distinguish a handful of European languages for reporting.
"""

from __future__ import annotations

from app.core.text import canonical_key, tokenize
from app.models.enums import EmploymentType

#: Canonical keys that map directly onto a contract type.
EMPLOYMENT_TOKENS: tuple[tuple[EmploymentType, tuple[str, ...]], ...] = (
    (
        EmploymentType.INTERNSHIP,
        ("internship", "intern", "praktikum", "working student", "werkstudent", "trainee"),
    ),
    (
        EmploymentType.FREELANCE,
        ("freelance", "freelancer", "self employed", "independent contractor", "b2b"),
    ),
    (
        EmploymentType.CONTRACT,
        ("contract", "contractor", "fixed term", "fixed-term", "interim", "temporary contract"),
    ),
    (EmploymentType.TEMPORARY, ("temporary", "seasonal", "casual", "zeitarbeit")),
    (
        EmploymentType.PART_TIME,
        ("part time", "part-time", "parttime", "teilzeit", "20h", "30h", "0.5 fte"),
    ),
    (
        EmploymentType.FULL_TIME,
        ("full time", "full-time", "fulltime", "permanent", "vollzeit", "unbefristet", "40h"),
    ),
    (EmploymentType.VOLUNTEER, ("volunteer", "voluntary", "unpaid")),
)

#: Stop-word profiles used for language identification.
LANGUAGE_PROFILES: dict[str, frozenset[str]] = {
    "en": frozenset(
        {"the", "and", "you", "with", "for", "our", "we", "are", "will", "your", "have", "team"}
    ),
    "de": frozenset(
        {"und", "der", "die", "das", "mit", "für", "wir", "sie", "ein", "eine", "nicht", "bei"}
    ),
    "fr": frozenset(
        {"et", "le", "la", "les", "des", "vous", "nous", "pour", "avec", "dans", "une", "est"}
    ),
    "es": frozenset(
        {"y", "el", "la", "los", "las", "con", "para", "que", "una", "del", "nuestro", "somos"}
    ),
    "nl": frozenset(
        {"en", "de", "het", "een", "voor", "met", "van", "wij", "je", "onze", "bij", "zijn"}
    ),
    "pt": frozenset(
        {"e", "o", "a", "os", "as", "com", "para", "que", "uma", "nosso", "somos", "voce"}
    ),
}

MIN_LANGUAGE_TOKENS = 25


class EmploymentClassifier:
    """Decides the contract type of a posting."""

    def classify(
        self, raw_value: str | None, *, title: str | None = None, description: str | None = None
    ) -> tuple[EmploymentType, float]:
        """Return the contract type and how confident the decision is."""
        if raw_value:
            match = self._match(canonical_key(raw_value))
            if match is not None:
                return match, 0.95

        if title:
            match = self._match(canonical_key(title))
            if match is not None:
                return match, 0.75

        if description:
            match = self._match(canonical_key(description[:1500]))
            if match is not None:
                return match, 0.6

        return EmploymentType.UNKNOWN, 0.0

    @staticmethod
    def _match(haystack: str) -> EmploymentType | None:
        if not haystack:
            return None
        padded = f" {haystack} "
        for employment_type, tokens in EMPLOYMENT_TOKENS:
            if any(f" {token} " in padded for token in tokens):
                return employment_type
        return None


class LanguageDetector:
    """Identifies the language of a posting from stop-word frequencies."""

    def detect(self, text: str | None) -> tuple[str | None, float]:
        """Return an ISO-639-1 code and a confidence score."""
        if not text:
            return None, 0.0
        tokens = tokenize(text[:4000])
        if len(tokens) < MIN_LANGUAGE_TOKENS:
            return None, 0.0

        counts = {
            code: sum(1 for token in tokens if token in profile)
            for code, profile in LANGUAGE_PROFILES.items()
        }
        best = max(counts, key=lambda code: counts[code])
        hits = counts[best]
        if hits == 0:
            return None, 0.0

        total = sum(counts.values()) or 1
        confidence = round(min(1.0, (hits / total) * min(1.0, hits / 8)), 3)
        return best, confidence


__all__ = [
    "EMPLOYMENT_TOKENS",
    "LANGUAGE_PROFILES",
    "EmploymentClassifier",
    "LanguageDetector",
]
