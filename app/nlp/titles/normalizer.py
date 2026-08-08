"""Job-title normalization.

A title is decomposed rather than flattened: the canonical role, the family it
belongs to, its seniority and its technical specialization are separate facts,
each with its own evidence. ``Senior Backend Python Engineer (m/f/d)`` becomes

    canonical_title = "Backend Engineer"
    title_family    = "Backend Engineering"
    seniority       = senior
    specialization  = "Python"

Every classification carries a confidence score, because a title such as
"Ninja Rockstar Developer" genuinely is ambiguous and the platform should say
so instead of pretending otherwise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from app.core.errors import ConfigurationError
from app.core.text import canonical_key, clean_text
from app.models.enums import ExperienceLevel, MarketSegment
from app.models.job import TitleAnalysis

DEFAULT_TITLE_RULES_PATH = Path("configs") / "title_rules.yaml"

FALLBACK_FAMILY = "Other"
FALLBACK_TITLE = "Other"


@dataclass(frozen=True, slots=True)
class SeniorityRule:
    level: ExperienceLevel
    priority: int
    pattern: re.Pattern[str]


@dataclass(frozen=True, slots=True)
class FamilyRule:
    family: str
    segment: MarketSegment
    canonical: str
    priority: int
    pattern: re.Pattern[str]


@dataclass(frozen=True, slots=True)
class SpecializationRule:
    name: str
    pattern: re.Pattern[str]
    family: str | None = None
    segment: MarketSegment | None = None


@dataclass(slots=True)
class TitleRules:
    """The compiled rule set."""

    seniority: list[SeniorityRule] = field(default_factory=list)
    families: list[FamilyRule] = field(default_factory=list)
    specializations: list[SpecializationRule] = field(default_factory=list)
    noise: list[re.Pattern[str]] = field(default_factory=list)
    version: int = 1


def _compile_alternation(patterns: list[str]) -> re.Pattern[str]:
    """One case-insensitive alternation with technology-safe boundaries."""
    escaped = "|".join(re.escape(str(p).strip()) for p in patterns if str(p).strip())
    return re.compile(rf"(?<![a-z0-9_+#])(?:{escaped})(?![a-z0-9_+#])", re.IGNORECASE)


def load_title_rules(path: Path | str | None = None) -> TitleRules:
    """Read and compile the title rule set."""
    config_path = Path(path) if path else DEFAULT_TITLE_RULES_PATH
    if not config_path.exists():
        raise ConfigurationError(
            f"title rules not found at {config_path}", details={"path": str(config_path)}
        )
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ConfigurationError(f"{config_path} must contain a mapping")

    seniority: list[SeniorityRule] = []
    for entry in payload.get("seniority") or []:
        try:
            level = ExperienceLevel(str(entry["level"]).lower())
        except (KeyError, ValueError) as exc:
            raise ConfigurationError(f"invalid seniority entry in {config_path}: {entry}") from exc
        seniority.append(
            SeniorityRule(
                level=level,
                priority=int(entry.get("priority", 0)),
                pattern=_compile_alternation(entry.get("patterns") or []),
            )
        )

    families: list[FamilyRule] = []
    for entry in payload.get("families") or []:
        try:
            segment = MarketSegment(str(entry.get("segment", "other")).lower())
        except ValueError as exc:
            raise ConfigurationError(
                f"family {entry.get('family')!r} has unknown segment {entry.get('segment')!r}"
            ) from exc
        families.append(
            FamilyRule(
                family=str(entry.get("family") or FALLBACK_FAMILY),
                segment=segment,
                canonical=str(entry.get("canonical") or entry.get("family") or FALLBACK_TITLE),
                priority=int(entry.get("priority", 0)),
                pattern=_compile_alternation(entry.get("patterns") or []),
            )
        )

    specializations: list[SpecializationRule] = []
    for entry in payload.get("specializations") or []:
        segment_value = entry.get("segment")
        specializations.append(
            SpecializationRule(
                name=str(entry.get("name") or ""),
                pattern=_compile_alternation(entry.get("patterns") or []),
                family=str(entry["family"]) if entry.get("family") else None,
                segment=MarketSegment(str(segment_value).lower()) if segment_value else None,
            )
        )

    noise = [re.compile(str(p), re.IGNORECASE) for p in payload.get("noise_patterns") or []]

    if not families:
        raise ConfigurationError(f"{config_path} does not define any title families")

    families.sort(key=lambda rule: rule.priority, reverse=True)
    seniority.sort(key=lambda rule: rule.priority, reverse=True)
    return TitleRules(
        seniority=seniority,
        families=families,
        specializations=specializations,
        noise=noise,
        version=int(payload.get("version", 1)),
    )


@lru_cache(maxsize=4)
def get_title_rules(path: str | None = None) -> TitleRules:
    """Process-wide cached rule set."""
    return load_title_rules(path)


class TitleNormalizer:
    """Decomposes a raw job title into comparable parts."""

    def __init__(self, rules: TitleRules | None = None) -> None:
        self._rules = rules or get_title_rules()

    def strip_noise(self, title: str) -> str:
        """Remove gender markers, contract notes and trailing decorations."""
        cleaned = clean_text(title, max_length=512)
        for pattern in self._rules.noise:
            cleaned = pattern.sub(" ", cleaned)
        cleaned = re.sub(r"[\s\-\u2013\u2014/|,;:]+$", "", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        return cleaned.strip()

    def normalize(self, title: str, *, description: str | None = None) -> TitleAnalysis:
        """Classify a title, optionally using the description as a tie-breaker."""
        raw = title or ""
        cleaned = self.strip_noise(raw)
        if not cleaned:
            return TitleAnalysis(
                raw_title=raw,
                canonical_title=FALLBACK_TITLE,
                title_family=FALLBACK_FAMILY,
                confidence=0.0,
            )

        seniority = self.detect_seniority(cleaned)
        family_rule = self._match_family(cleaned)
        specialization_rule = self._match_specialization(cleaned)

        if family_rule is None and description:
            family_rule = self._match_family(description[:600])

        if family_rule is not None:
            family = family_rule.family
            canonical = family_rule.canonical
            segment = family_rule.segment
            base_confidence = 0.55 + min(0.25, family_rule.priority / 400)
        elif specialization_rule is not None and specialization_rule.family:
            family = specialization_rule.family
            canonical = "Software Engineer"
            segment = specialization_rule.segment or MarketSegment.OTHER
            base_confidence = 0.5
        else:
            family = FALLBACK_FAMILY
            canonical = FALLBACK_TITLE
            segment = MarketSegment.OTHER
            base_confidence = 0.2

        specialization = specialization_rule.name if specialization_rule else None
        confidence = base_confidence
        if seniority is not ExperienceLevel.UNKNOWN:
            confidence += 0.08
        if specialization:
            confidence += 0.07
        if len(cleaned) <= 60:
            confidence += 0.05

        return TitleAnalysis(
            raw_title=raw,
            canonical_title=self._compose(canonical, seniority, family),
            title_family=family,
            seniority=seniority,
            specialization=specialization,
            segment=segment,
            confidence=round(min(confidence, 1.0), 3),
        )

    def detect_seniority(self, title: str) -> ExperienceLevel:
        """Highest-priority seniority marker present in the title."""
        for rule in self._rules.seniority:
            if rule.pattern.search(title):
                return rule.level
        return ExperienceLevel.UNKNOWN

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _match_family(self, text: str) -> FamilyRule | None:
        for rule in self._rules.families:
            if rule.pattern.search(text):
                return rule
        return None

    def _match_specialization(self, text: str) -> SpecializationRule | None:
        for rule in self._rules.specializations:
            if rule.pattern.search(text):
                return rule
        return None

    @staticmethod
    def _compose(canonical: str, seniority: ExperienceLevel, family: str) -> str:
        """Build the comparable title, keeping seniority where it is meaningful."""
        if canonical == FALLBACK_TITLE:
            return canonical

        # Leadership roles are named after the discipline they lead, not after
        # the individual-contributor title.
        leadership = {
            ExperienceLevel.MANAGER: "Manager",
            ExperienceLevel.DIRECTOR: "Director",
            ExperienceLevel.EXECUTIVE: "Executive",
        }.get(seniority)
        if leadership and family != FALLBACK_FAMILY:
            return f"{family} {leadership}"

        prefix = {
            ExperienceLevel.INTERN: "Intern",
            ExperienceLevel.JUNIOR: "Junior",
            ExperienceLevel.SENIOR: "Senior",
            ExperienceLevel.LEAD: "Lead",
            ExperienceLevel.PRINCIPAL: "Principal",
        }.get(seniority)
        return f"{prefix} {canonical}" if prefix else canonical

    @staticmethod
    def group_key(analysis: TitleAnalysis) -> str:
        """Stable key for grouping equivalent roles in analytics."""
        return canonical_key(f"{analysis.title_family}|{analysis.canonical_title}")


__all__ = [
    "DEFAULT_TITLE_RULES_PATH",
    "FALLBACK_FAMILY",
    "FALLBACK_TITLE",
    "FamilyRule",
    "SeniorityRule",
    "SpecializationRule",
    "TitleNormalizer",
    "TitleRules",
    "get_title_rules",
    "load_title_rules",
]
