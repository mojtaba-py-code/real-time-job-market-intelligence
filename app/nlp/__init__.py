"""Natural-language processing: skills, titles and classification."""

from __future__ import annotations

from app.nlp.pipeline import NlpPipeline, NlpResult
from app.nlp.skills.extractor import SkillExtractor, TaxonomySkillExtractor
from app.nlp.skills.taxonomy import SkillTaxonomy, get_taxonomy, load_taxonomy
from app.nlp.titles.normalizer import TitleNormalizer

__all__ = [
    "NlpPipeline",
    "NlpResult",
    "SkillExtractor",
    "SkillTaxonomy",
    "TaxonomySkillExtractor",
    "TitleNormalizer",
    "get_taxonomy",
    "load_taxonomy",
]
