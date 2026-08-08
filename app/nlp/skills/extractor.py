"""Skill extraction.

The default implementation is a high-precision dictionary matcher built from
the taxonomy. It is fast (one compiled automaton, linear in the document
length), deterministic, and explainable - every detection carries the alias
that produced it.

It sits behind the :class:`SkillExtractor` protocol so it can be swapped for a
spaCy pipeline, an embedding model or a fine-tuned classifier without touching
any caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.core.text import normalize_unicode
from app.models.job import ExtractedSkill
from app.nlp.skills.taxonomy import SkillNode, SkillTaxonomy, get_taxonomy

#: Words that make an ambiguous match ("Go", "R", "C") credible.
CONTEXT_QUALIFIERS = (
    "developer",
    "engineer",
    "programming",
    "language",
    "development",
    "backend",
    "experience",
    "knowledge",
    "skills",
    "stack",
    "code",
    "coding",
    "proficiency",
)

#: Section headings that mark requirements versus optional extras.
_REQUIRED_HEADING_RE = re.compile(
    r"^\s*(?:requirements?|qualifications?|must[ -]?have|what you.{0,20}bring|"
    r"we expect|your profile|essential)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)
_OPTIONAL_HEADING_RE = re.compile(
    r"^\s*(?:nice[ -]to[ -]have|bonus|plus(?:es)?|preferred|desirable|"
    r"good to have|optional|advantageous)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)

#: Characters that may abut a technology name without changing it.
_DELIMITERS = set(" \t\n\r,;:/|()[]{}<>\"'`\u2022*-\u2013\u2014+&")


@runtime_checkable
class SkillExtractor(Protocol):
    """Anything that can find skills in text."""

    def extract(self, text: str, *, field: str = "description") -> list[ExtractedSkill]:
        """Return the skills mentioned in ``text``."""
        ...


@dataclass(frozen=True, slots=True)
class _Match:
    slug: str
    alias: str
    start: int
    end: int


class TaxonomySkillExtractor:
    """Dictionary-based extractor driven entirely by the taxonomy."""

    def __init__(
        self,
        taxonomy: SkillTaxonomy | None = None,
        *,
        min_confidence: float = 0.0,
        max_skills: int = 60,
    ) -> None:
        self._taxonomy = taxonomy if taxonomy is not None else get_taxonomy()
        self._min_confidence = min_confidence
        self._max_skills = max_skills
        self._alias_to_slug = self._taxonomy.alias_map()
        self._pattern = self._compile(self._alias_to_slug)

    @property
    def taxonomy(self) -> SkillTaxonomy:
        return self._taxonomy

    @staticmethod
    def _compile(alias_map: dict[str, str]) -> re.Pattern[str]:
        """Build one alternation regex over every surface form.

        Longer aliases come first so ``react native`` wins over ``react``.
        Lookarounds rather than ``\\b`` are used because technology names end in
        characters (``c++``, ``c#``, ``node.js``) that ``\\b`` treats as
        boundaries in the wrong places.
        """
        aliases = sorted(alias_map, key=len, reverse=True)
        alternation = "|".join(re.escape(alias) for alias in aliases if alias)
        return re.compile(
            rf"(?<![a-z0-9_+#])(?:{alternation})(?![a-z0-9_+#])",
            re.IGNORECASE,
        )

    # ------------------------------------------------------------------ #
    # Extraction
    # ------------------------------------------------------------------ #
    def extract(self, text: str, *, field: str = "description") -> list[ExtractedSkill]:
        """Find every taxonomy skill mentioned in ``text``."""
        if not text:
            return []
        body = normalize_unicode(text)
        lowered = body.lower()

        required_span, optional_span = self._section_spans(body)
        matches = self._find_matches(lowered)

        aggregated: dict[str, list[_Match]] = {}
        for match in matches:
            aggregated.setdefault(match.slug, []).append(match)

        skills: list[ExtractedSkill] = []
        for slug, hits in aggregated.items():
            node = self._taxonomy.get(slug)
            if node is None:
                continue
            is_required = self._requirement_flag(hits, required_span, optional_span)
            confidence = self._confidence(node, hits, is_required)
            if confidence < self._min_confidence:
                continue
            skills.append(
                ExtractedSkill(
                    slug=slug,
                    name=node.name,
                    category=node.category,
                    parent=node.parent,
                    confidence=confidence,
                    occurrences=len(hits),
                    matched_alias=hits[0].alias[:96],
                    field=field,
                    is_required=is_required,
                )
            )

        skills.sort(key=lambda s: (-s.confidence, -s.occurrences, s.slug))
        return skills[: self._max_skills]

    def extract_from_job(self, *, title: str, description: str) -> list[ExtractedSkill]:
        """Extract from a whole posting, boosting skills named in the title."""
        from_description = {s.slug: s for s in self.extract(description, field="description")}
        for skill in self.extract(title, field="title"):
            existing = from_description.get(skill.slug)
            if existing is None:
                from_description[skill.slug] = skill.model_copy(
                    update={"confidence": min(1.0, skill.confidence + 0.15), "field": "title"}
                )
            else:
                # A technology in the title is the point of the job.
                from_description[skill.slug] = existing.model_copy(
                    update={
                        "confidence": min(1.0, existing.confidence + 0.2),
                        "field": "title+description",
                        "occurrences": existing.occurrences + skill.occurrences,
                        "is_required": True,
                    }
                )
        ranked = sorted(
            from_description.values(), key=lambda s: (-s.confidence, -s.occurrences, s.slug)
        )
        return ranked[: self._max_skills]

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _find_matches(self, lowered: str) -> list[_Match]:
        matches: list[_Match] = []
        for found in self._pattern.finditer(lowered):
            alias = found.group(0)
            slug = self._alias_to_slug.get(alias.lower())
            if slug is None:
                continue
            node = self._taxonomy.get(slug)
            if node is not None and node.ambiguous and not self._is_credible(lowered, found):
                continue
            matches.append(_Match(slug=slug, alias=alias, start=found.start(), end=found.end()))
        return matches

    @staticmethod
    def _is_credible(text: str, match: re.Match[str]) -> bool:
        """Decide whether an ambiguous short name is really a technology.

        ``Go`` in "go to the office" is noise; ``Go`` in "Python, Go, Rust" or
        "Go developer" is a language.
        """
        before = text[max(0, match.start() - 1) : match.start()]
        after = text[match.end() : match.end() + 1]
        delimited = (not before or before in _DELIMITERS) and (not after or after in _DELIMITERS)
        if not delimited:
            return False
        window = text[max(0, match.start() - 60) : match.end() + 60]
        if any(qualifier in window for qualifier in CONTEXT_QUALIFIERS):
            return True
        # A list such as "Python, Go, Rust" is credible on its own.
        neighbourhood = text[max(0, match.start() - 3) : match.end() + 3]
        return neighbourhood.count(",") >= 1 or neighbourhood.count("/") >= 1

    @staticmethod
    def _section_spans(body: str) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
        """Locate the requirements and the nice-to-have sections."""
        required = _REQUIRED_HEADING_RE.search(body)
        optional = _OPTIONAL_HEADING_RE.search(body)

        required_span: tuple[int, int] | None = None
        optional_span: tuple[int, int] | None = None
        if required is not None:
            end = (
                optional.start()
                if optional is not None and optional.start() > required.start()
                else len(body)
            )
            required_span = (required.end(), end)
        if optional is not None:
            optional_span = (optional.end(), len(body))
        return required_span, optional_span

    @staticmethod
    def _requirement_flag(
        hits: list[_Match],
        required_span: tuple[int, int] | None,
        optional_span: tuple[int, int] | None,
    ) -> bool | None:
        """``True`` if required, ``False`` if nice-to-have, ``None`` if unclear."""
        in_required = required_span is not None and any(
            required_span[0] <= hit.start < required_span[1] for hit in hits
        )
        in_optional = optional_span is not None and any(
            optional_span[0] <= hit.start < optional_span[1] for hit in hits
        )
        if in_required:
            return True
        if in_optional:
            return False
        return None

    @staticmethod
    def _confidence(node: SkillNode, hits: list[_Match], is_required: bool | None) -> float:
        """Score a detection from repetition, weight and section."""
        score = 0.55
        score += min(0.2, 0.07 * (len(hits) - 1))
        score += 0.1 * min(1.0, node.weight - 1.0) if node.weight > 1.0 else 0.0
        if is_required is True:
            score += 0.15
        elif is_required is False:
            score -= 0.05
        if node.ambiguous:
            score -= 0.1
        # An exact canonical-name match is stronger than an alias match.
        if any(hit.alias.lower() == node.name.lower() for hit in hits):
            score += 0.08
        return round(max(0.0, min(score, 1.0)), 3)


__all__ = ["CONTEXT_QUALIFIERS", "SkillExtractor", "TaxonomySkillExtractor"]
