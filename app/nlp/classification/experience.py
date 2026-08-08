"""Experience-requirement detection.

Two independent signals are combined: what the title says (``Senior``) and what
the body asks for (``5+ years of experience``). When they disagree the explicit
year range wins, because it is the harder requirement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.models.enums import EXPERIENCE_YEAR_BANDS, SENIORITY_ORDER, ExperienceLevel

#: "5+ years", "3 - 5 years", "at least 4 years", "mindestens 3 Jahre"
_RANGE_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:[-\u2013\u2014]|to|bis)\s*(\d{1,2})\s*\+?\s*"
    r"(?:years?|yrs?|jahre|ans|anni|años)",
    re.IGNORECASE,
)
_MINIMUM_RE = re.compile(
    r"(?:at least|minimum(?: of)?|min\.?|mindestens|more than|over|ab)\s*(\d{1,2})\s*\+?\s*"
    r"(?:years?|yrs?|jahre|ans)",
    re.IGNORECASE,
)
_PLUS_RE = re.compile(r"(\d{1,2})\s*\+\s*(?:years?|yrs?|jahre|ans)", re.IGNORECASE)
_SIMPLE_RE = re.compile(
    r"(\d{1,2})\s*(?:years?|yrs?|jahre|ans)\s*(?:of\s+)?(?:relevant\s+|professional\s+|"
    r"commercial\s+|hands[- ]on\s+)?(?:experience|expertise|background|erfahrung)",
    re.IGNORECASE,
)

#: Reject absurd values ("20 years of Kubernetes" is a red flag, not a fact).
MAX_YEARS = 30


@dataclass(frozen=True, slots=True)
class ExperienceRequirement:
    """What a posting asks for in terms of experience."""

    level: ExperienceLevel = ExperienceLevel.UNKNOWN
    years_min: float | None = None
    years_max: float | None = None
    confidence: float = 0.0
    evidence: str | None = None

    @property
    def has_years(self) -> bool:
        return self.years_min is not None or self.years_max is not None


def level_for_years(years: float) -> ExperienceLevel:
    """Map a years-of-experience figure onto the seniority ladder."""
    for level, (low, high) in EXPERIENCE_YEAR_BANDS.items():
        if low <= years < high:
            return level
    return ExperienceLevel.PRINCIPAL if years >= 9 else ExperienceLevel.UNKNOWN


class ExperienceClassifier:
    """Extracts the experience requirement from a posting."""

    def extract_years(self, text: str) -> tuple[float | None, float | None, str | None]:
        """Find an explicit years-of-experience requirement."""
        if not text:
            return None, None, None
        window = text[:8000]

        match = _RANGE_RE.search(window)
        if match:
            low, high = float(match.group(1)), float(match.group(2))
            if 0 <= low <= high <= MAX_YEARS:
                return low, high, match.group(0)[:120]

        for pattern in (_MINIMUM_RE, _PLUS_RE, _SIMPLE_RE):
            match = pattern.search(window)
            if match:
                value = float(match.group(1))
                if 0 <= value <= MAX_YEARS:
                    return value, None, match.group(0)[:120]
        return None, None, None

    def classify(
        self,
        *,
        title_seniority: ExperienceLevel = ExperienceLevel.UNKNOWN,
        description: str | None = None,
    ) -> ExperienceRequirement:
        """Combine the title signal with the explicit requirement in the body."""
        years_min, years_max, evidence = self.extract_years(description or "")

        if years_min is not None:
            derived = level_for_years(years_min)
            if title_seniority is ExperienceLevel.UNKNOWN:
                return ExperienceRequirement(
                    level=derived,
                    years_min=years_min,
                    years_max=years_max,
                    confidence=0.75,
                    evidence=evidence,
                )
            # Both signals present: agreement is strong evidence, disagreement
            # is resolved in favour of the explicit requirement but noted with
            # a lower confidence.
            agrees = (
                abs(SENIORITY_ORDER.get(derived, 99) - SENIORITY_ORDER.get(title_seniority, 99))
                <= 1
            )
            return ExperienceRequirement(
                level=title_seniority if agrees else derived,
                years_min=years_min,
                years_max=years_max,
                confidence=0.9 if agrees else 0.6,
                evidence=evidence,
            )

        if title_seniority is not ExperienceLevel.UNKNOWN:
            band = EXPERIENCE_YEAR_BANDS.get(title_seniority)
            return ExperienceRequirement(
                level=title_seniority,
                years_min=band[0] if band else None,
                years_max=band[1] if band else None,
                confidence=0.55,
                evidence="title",
            )

        return ExperienceRequirement()


__all__ = [
    "MAX_YEARS",
    "ExperienceClassifier",
    "ExperienceRequirement",
    "level_for_years",
]
