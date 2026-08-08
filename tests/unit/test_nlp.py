"""Unit tests for the NLP layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import TaxonomyError
from app.models.enums import EmploymentType, ExperienceLevel, MarketSegment, SkillCategory
from app.nlp.classification.employment import EmploymentClassifier, LanguageDetector
from app.nlp.classification.experience import ExperienceClassifier, level_for_years
from app.nlp.pipeline import NlpPipeline
from app.nlp.skills.extractor import TaxonomySkillExtractor
from app.nlp.skills.taxonomy import load_taxonomy
from app.nlp.titles.normalizer import TitleNormalizer

pytestmark = pytest.mark.unit

DESCRIPTION = """
About Acme
We build data platforms.

Requirements
- Strong experience with Python, FastAPI and PostgreSQL
- 5+ years of professional experience
- Comfortable with Docker and Kubernetes in production

Nice to have
- Terraform, AWS
- Exposure to Go or Rust

We go to conferences twice a year.
"""


@pytest.fixture(scope="module")
def taxonomy() -> object:
    return load_taxonomy()


@pytest.fixture(scope="module")
def extractor(taxonomy: object) -> TaxonomySkillExtractor:
    return TaxonomySkillExtractor(taxonomy)  # type: ignore[arg-type]


class TestTaxonomy:
    def test_loads_and_validates(self, taxonomy: object) -> None:
        assert len(taxonomy.skills) > 80  # type: ignore[attr-defined]
        assert taxonomy.get("python") is not None  # type: ignore[attr-defined]

    def test_resolves_aliases(self, taxonomy: object) -> None:
        assert taxonomy.resolve("k8s").slug == "kubernetes"  # type: ignore[attr-defined]
        assert taxonomy.resolve("postgres").slug == "postgresql"  # type: ignore[attr-defined]

    def test_hierarchy_is_navigable(self, taxonomy: object) -> None:
        ancestors = taxonomy.ancestors("python")  # type: ignore[attr-defined]
        assert "languages" in ancestors
        assert "software-engineering" in ancestors

    def test_tree_and_rows_are_exportable(self, taxonomy: object) -> None:
        tree = taxonomy.tree()  # type: ignore[attr-defined]
        assert tree["groups"]
        rows = taxonomy.to_rows()  # type: ignore[attr-defined]
        assert any(row["slug"] == "python" for row in rows)

    def test_missing_file_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(TaxonomyError):
            load_taxonomy(tmp_path / "nope.yaml")

    def test_unknown_parent_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "taxonomy.yaml"
        path.write_text(
            "version: 1\ngroups: []\nskills:\n  - slug: x\n    name: X\n    parent: ghost\n",
            encoding="utf-8",
        )
        with pytest.raises(TaxonomyError, match="unknown group"):
            load_taxonomy(path)


class TestSkillExtraction:
    def test_extracts_required_and_optional_skills(self, extractor: TaxonomySkillExtractor) -> None:
        skills = {s.slug: s for s in extractor.extract(DESCRIPTION)}
        assert {"python", "fastapi", "postgresql", "docker", "kubernetes"} <= set(skills)
        assert skills["python"].is_required is True
        assert skills["terraform"].is_required is False

    def test_ambiguous_names_need_context(self, extractor: TaxonomySkillExtractor) -> None:
        assert not any(s.slug == "go" for s in extractor.extract("We go to the office daily."))
        assert any(s.slug == "go" for s in extractor.extract("Stack: Python, Go, Rust"))

    def test_title_matches_raise_confidence(self, extractor: TaxonomySkillExtractor) -> None:
        from_body = {s.slug: s for s in extractor.extract(DESCRIPTION)}
        combined = {
            s.slug: s
            for s in extractor.extract_from_job(
                title="Senior Python Engineer", description=DESCRIPTION
            )
        }
        assert combined["python"].confidence > from_body["python"].confidence

    def test_categories_come_from_the_taxonomy(self, extractor: TaxonomySkillExtractor) -> None:
        skills = {s.slug: s for s in extractor.extract(DESCRIPTION)}
        assert skills["python"].category is SkillCategory.LANGUAGE
        assert skills["postgresql"].category is SkillCategory.DATABASE

    def test_empty_input_is_safe(self, extractor: TaxonomySkillExtractor) -> None:
        assert extractor.extract("") == []

    def test_confidence_is_bounded(self, extractor: TaxonomySkillExtractor) -> None:
        for skill in extractor.extract(DESCRIPTION):
            assert 0.0 <= skill.confidence <= 1.0


class TestTitleNormalization:
    @pytest.mark.parametrize(
        ("title", "family", "seniority"),
        [
            ("Senior Python Developer", "Software Engineering", ExperienceLevel.SENIOR),
            ("Backend Python Developer", "Backend Engineering", ExperienceLevel.UNKNOWN),
            (
                "Senior Backend Python Engineer (m/f/d)",
                "Backend Engineering",
                ExperienceLevel.SENIOR,
            ),
            ("Data Engineer", "Data Engineering", ExperienceLevel.UNKNOWN),
            ("Working Student Data Science", "Data Science", ExperienceLevel.INTERN),
            ("Staff SRE", "DevOps", ExperienceLevel.PRINCIPAL),
            ("QA Automation Tester", "Quality Assurance", ExperienceLevel.UNKNOWN),
        ],
    )
    def test_decomposes_titles(self, title: str, family: str, seniority: ExperienceLevel) -> None:
        analysis = TitleNormalizer().normalize(title)
        assert analysis.title_family == family
        assert analysis.seniority is seniority
        assert 0.0 <= analysis.confidence <= 1.0

    def test_specialization_is_detected(self) -> None:
        assert TitleNormalizer().normalize("Senior Python Developer").specialization == "Python"

    def test_gender_markers_are_removed(self) -> None:
        assert "m/f/d" not in TitleNormalizer().strip_noise("Engineer (m/f/d)")

    def test_leadership_titles_name_the_discipline(self) -> None:
        analysis = TitleNormalizer().normalize("Head of Engineering")
        assert analysis.seniority is ExperienceLevel.DIRECTOR
        assert "Director" in analysis.canonical_title

    def test_empty_title_reports_zero_confidence(self) -> None:
        assert TitleNormalizer().normalize("").confidence == 0.0


class TestExperienceClassification:
    @pytest.mark.parametrize(
        ("text", "low", "high"),
        [
            ("3-5 years of experience", 3, 5),
            ("at least 4 years of experience", 4, None),
            ("7+ years", 7, None),
            ("5 years of professional experience", 5, None),
        ],
    )
    def test_extracts_year_requirements(self, text: str, low: float, high: float | None) -> None:
        years_min, years_max, _evidence = ExperienceClassifier().extract_years(text)
        assert years_min == low
        assert years_max == high

    def test_explicit_years_win_over_the_title(self) -> None:
        from app.models.enums import SENIORITY_ORDER

        result = ExperienceClassifier().classify(
            title_seniority=ExperienceLevel.JUNIOR, description="10+ years of experience"
        )
        assert result.years_min == 10
        # The title said junior; ten years of experience does not.
        assert SENIORITY_ORDER[result.level] >= SENIORITY_ORDER[ExperienceLevel.LEAD]
        # Disagreement is reported with a lower confidence than agreement.
        assert result.confidence < 0.9

    def test_agreement_raises_confidence(self) -> None:
        result = ExperienceClassifier().classify(
            title_seniority=ExperienceLevel.SENIOR, description="6 years of experience"
        )
        assert result.level is ExperienceLevel.SENIOR
        assert result.confidence >= 0.9

    def test_title_only_still_produces_a_band(self) -> None:
        result = ExperienceClassifier().classify(title_seniority=ExperienceLevel.JUNIOR)
        assert result.years_min is not None

    def test_level_for_years(self) -> None:
        assert level_for_years(0.5) is ExperienceLevel.ENTRY
        assert level_for_years(6) is ExperienceLevel.SENIOR


class TestClassifiers:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Full-time", EmploymentType.FULL_TIME),
            ("Internship", EmploymentType.INTERNSHIP),
            ("Contract", EmploymentType.CONTRACT),
            ("Teilzeit", EmploymentType.PART_TIME),
            (None, EmploymentType.UNKNOWN),
        ],
    )
    def test_employment_type(self, raw: str | None, expected: EmploymentType) -> None:
        result, _confidence = EmploymentClassifier().classify(raw)
        assert result is expected

    def test_language_detection(self) -> None:
        english = "We are looking for a developer to join our team and build the product with us"
        detected, confidence = LanguageDetector().detect(english * 2)
        assert detected == "en"
        assert confidence > 0

    def test_language_detection_needs_enough_text(self) -> None:
        assert LanguageDetector().detect("hi") == (None, 0.0)


class TestPipeline:
    def test_analyses_a_whole_posting(self) -> None:
        result = NlpPipeline().analyze(
            title="Senior Backend Python Engineer (m/f/d)",
            description=DESCRIPTION,
            raw_employment_type="Full-time",
        )
        assert result.title.title_family == "Backend Engineering"
        assert result.experience.level is ExperienceLevel.SENIOR
        assert result.employment_type is EmploymentType.FULL_TIME
        assert result.segment is MarketSegment.BACKEND
        assert "python" in result.skill_slugs
        assert result.duration_ms >= 0

    def test_required_skills_are_separated(self) -> None:
        result = NlpPipeline().analyze(title="Python Developer", description=DESCRIPTION)
        assert any(skill.slug == "python" for skill in result.required_skills)

    def test_average_confidence_is_reported(self) -> None:
        result = NlpPipeline().analyze(title="Data Engineer", description=DESCRIPTION)
        assert 0.0 < result.average_confidence <= 1.0
