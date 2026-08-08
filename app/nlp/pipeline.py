"""The NLP pipeline.

    Job description
           |
      text cleaning        (app.processing.cleaning)
           |
      tokenization         (app.core.text)
           |
      normalization        (unicode, casing, technical punctuation)
           |
      entity/skill extraction
           |
      classification       (title, seniority, contract, language)
           |
      skill mapping        (taxonomy slugs + hierarchy)
           |
      confidence scores

Each stage is an injectable component. Replacing the dictionary extractor with
a spaCy or transformer model means passing a different object to the
constructor - the pipeline, the processing layer and the analytics never
notice.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.models.enums import EmploymentType, MarketSegment, SkillCategory
from app.models.job import ExtractedSkill, TitleAnalysis
from app.nlp.classification.employment import EmploymentClassifier, LanguageDetector
from app.nlp.classification.experience import ExperienceClassifier, ExperienceRequirement
from app.nlp.skills.extractor import SkillExtractor, TaxonomySkillExtractor
from app.nlp.skills.taxonomy import SkillTaxonomy, get_taxonomy
from app.nlp.titles.normalizer import TitleNormalizer

log = get_logger(__name__)

#: Which skill categories point at which market segment when the title is
#: uninformative. Only used as a tie-breaker.
SEGMENT_HINTS: dict[str, MarketSegment] = {
    "ai-ml": MarketSegment.AI_ML,
    "data": MarketSegment.DATA_ENGINEERING,
    "devops": MarketSegment.DEVOPS,
    "cloud": MarketSegment.CLOUD,
    "frontend": MarketSegment.FRONTEND,
    "backend": MarketSegment.BACKEND,
    "mobile": MarketSegment.MOBILE,
    "security": MarketSegment.CYBERSECURITY,
    "testing": MarketSegment.QA,
}


@dataclass(slots=True)
class NlpResult:
    """Everything the NLP layer knows about one posting."""

    title: TitleAnalysis
    skills: list[ExtractedSkill] = field(default_factory=list)
    experience: ExperienceRequirement = field(default_factory=ExperienceRequirement)
    employment_type: EmploymentType = EmploymentType.UNKNOWN
    employment_confidence: float = 0.0
    language: str | None = None
    language_confidence: float = 0.0
    segment: MarketSegment = MarketSegment.OTHER
    duration_ms: float = 0.0

    @property
    def skill_slugs(self) -> list[str]:
        return [skill.slug for skill in self.skills]

    @property
    def required_skills(self) -> list[ExtractedSkill]:
        return [skill for skill in self.skills if skill.is_required]

    @property
    def average_confidence(self) -> float:
        if not self.skills:
            return 0.0
        return sum(skill.confidence for skill in self.skills) / len(self.skills)


class NlpPipeline:
    """Runs every language-processing stage over a posting."""

    def __init__(
        self,
        *,
        extractor: SkillExtractor | None = None,
        title_normalizer: TitleNormalizer | None = None,
        experience_classifier: ExperienceClassifier | None = None,
        employment_classifier: EmploymentClassifier | None = None,
        language_detector: LanguageDetector | None = None,
        taxonomy: SkillTaxonomy | None = None,
        min_skill_confidence: float = 0.0,
        max_description_chars: int = 60_000,
    ) -> None:
        self._taxonomy = taxonomy if taxonomy is not None else get_taxonomy()
        self._extractor = extractor or TaxonomySkillExtractor(
            self._taxonomy, min_confidence=min_skill_confidence
        )
        self._titles = title_normalizer or TitleNormalizer()
        self._experience = experience_classifier or ExperienceClassifier()
        self._employment = employment_classifier or EmploymentClassifier()
        self._language = language_detector or LanguageDetector()
        self._max_chars = max_description_chars

    @property
    def taxonomy(self) -> SkillTaxonomy:
        return self._taxonomy

    def analyze(
        self,
        *,
        title: str,
        description: str | None = None,
        raw_employment_type: str | None = None,
    ) -> NlpResult:
        """Run the full pipeline over one posting."""
        import time

        started = time.perf_counter()
        body = (description or "")[: self._max_chars]

        title_analysis = self._titles.normalize(title, description=body)
        skills = self._extract(title=title, description=body)
        experience = self._experience.classify(
            title_seniority=title_analysis.seniority, description=body
        )
        employment, employment_confidence = self._employment.classify(
            raw_employment_type, title=title, description=body
        )
        language, language_confidence = self._language.detect(body or title)
        segment = self._resolve_segment(title_analysis, skills)

        return NlpResult(
            title=title_analysis,
            skills=skills,
            experience=experience,
            employment_type=employment,
            employment_confidence=employment_confidence,
            language=language,
            language_confidence=language_confidence,
            segment=segment,
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    def _extract(self, *, title: str, description: str) -> list[ExtractedSkill]:
        extractor = self._extractor
        if isinstance(extractor, TaxonomySkillExtractor):
            return extractor.extract_from_job(title=title, description=description)
        # Any other implementation only has to satisfy the protocol.
        merged = {skill.slug: skill for skill in extractor.extract(description)}
        for skill in extractor.extract(title, field="title"):
            merged.setdefault(skill.slug, skill)
        return list(merged.values())

    def _resolve_segment(self, title: TitleAnalysis, skills: list[ExtractedSkill]) -> MarketSegment:
        """Prefer the title's segment; fall back to the skill mix."""
        if title.segment is not MarketSegment.OTHER and title.confidence >= 0.5:
            return title.segment

        weights: dict[MarketSegment, float] = {}
        for skill in skills:
            parent = skill.parent or ""
            segment = SEGMENT_HINTS.get(parent)
            if segment is None and skill.category is SkillCategory.DATA:
                segment = MarketSegment.DATA_ENGINEERING
            if segment is None:
                continue
            weights[segment] = weights.get(segment, 0.0) + skill.confidence

        if not weights:
            return title.segment
        best = max(weights, key=lambda key: weights[key])
        return best if weights[best] >= 1.0 else title.segment


__all__ = ["SEGMENT_HINTS", "NlpPipeline", "NlpResult"]
