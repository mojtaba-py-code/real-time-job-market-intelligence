"""Unit tests for deduplication and the statistical primitives."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.analytics.statistics import (
    classify_trend,
    coefficient_of_variation,
    confidence_from_sample,
    fill_missing_days,
    linear_slope,
    mean,
    median,
    moving_average,
    percent_change,
    percentile,
    share,
    standard_deviation,
    z_score,
)
from app.core.config import DeduplicationSettings
from app.core.text import shingles, tokenize
from app.models.enums import DuplicateKind, TrendDirection
from app.models.job import NormalizedJob
from app.processing.deduplication.engine import DeduplicationEngine, KnownPosting
from app.processing.deduplication.minhash import (
    LshIndex,
    MinHasher,
    exact_jaccard,
    jaccard_similarity,
)
from app.quality.scorer import BatchSignals, QualityScorer, score_job

pytestmark = pytest.mark.unit


def make_job(**overrides: object) -> NormalizedJob:
    payload: dict[str, object] = {
        "source": "test",
        "source_job_id": "1",
        "title": "Senior Python Engineer",
        "normalized_title": "Senior Backend Engineer",
        "company_name": "Acme",
        "company_slug": "acme",
        "description": "We need a Python engineer with FastAPI and PostgreSQL. " * 12,
    }
    payload.update(overrides)
    return NormalizedJob(**payload)  # type: ignore[arg-type]


class TestMinHash:
    def test_signature_is_deterministic(self) -> None:
        hasher = MinHasher(64)
        tokens = shingles(tokenize("the quick brown fox jumps over the lazy dog"), 3)
        assert hasher.signature(tokens) == hasher.signature(tokens)

    def test_signature_estimates_jaccard(self) -> None:
        hasher = MinHasher(256)
        left = shingles(tokenize("python fastapi postgresql docker kubernetes aws terraform"), 2)
        right = shingles(tokenize("python fastapi postgresql docker kubernetes gcp ansible"), 2)
        estimate = jaccard_similarity(hasher.signature(left), hasher.signature(right))
        exact = exact_jaccard(left, right)
        assert abs(estimate - exact) < 0.15

    def test_empty_input_gives_an_empty_signature(self) -> None:
        assert MinHasher(32).signature([]) == []
        assert jaccard_similarity([], [1, 2]) == 0.0

    def test_identical_documents_score_one(self) -> None:
        hasher = MinHasher(64)
        tokens = shingles(tokenize("identical text for both documents here"), 2)
        assert jaccard_similarity(hasher.signature(tokens), hasher.signature(tokens)) == 1.0

    def test_lsh_finds_similar_documents(self) -> None:
        hasher = MinHasher(128)
        index = LshIndex(bands=32, permutations=128)
        base = "we are hiring a senior python engineer to build data pipelines " * 6
        variant = base.replace("senior", "lead")

        index.add("base", hasher.signature(shingles(tokenize(base), 3)))
        hits = index.query(hasher.signature(shingles(tokenize(variant), 3)), threshold=0.5)
        assert hits and hits[0][0] == "base"

    def test_lsh_ignores_unrelated_documents(self) -> None:
        hasher = MinHasher(128)
        index = LshIndex(bands=16, permutations=128)
        index.add("base", hasher.signature(shingles(tokenize("python backend engineer" * 20), 3)))
        other = hasher.signature(shingles(tokenize("frontend react designer" * 20), 3))
        assert index.query(other, threshold=0.6) == []

    def test_bands_must_divide_permutations(self) -> None:
        with pytest.raises(ValueError, match="divisible"):
            LshIndex(bands=7, permutations=128)


class TestDeduplicationEngine:
    def test_exact_source_duplicates_are_caught(self) -> None:
        engine = DeduplicationEngine()
        first = engine.fingerprint(make_job())
        engine.remember_job(first)

        second = engine.fingerprint(make_job(id="b" * 32))
        verdict = engine.check(second)
        assert verdict.kind is DuplicateKind.EXACT_SOURCE

    def test_url_duplicates_are_caught(self) -> None:
        engine = DeduplicationEngine()
        first = engine.fingerprint(
            make_job(canonical_url="https://example.com/a", description="alpha " * 60)
        )
        engine.remember_job(first)
        second = engine.fingerprint(
            make_job(
                source_job_id="2",
                canonical_url="https://example.com/a",
                description="totally different body " * 40,
            )
        )
        assert engine.check(second).kind is DuplicateKind.URL

    def test_content_duplicates_are_caught_across_sources(self) -> None:
        engine = DeduplicationEngine()
        engine.remember_job(engine.fingerprint(make_job()))
        other = engine.fingerprint(make_job(source="aggregator", source_job_id="99"))
        assert engine.check(other).kind is DuplicateKind.CONTENT

    def test_near_duplicates_are_caught(self) -> None:
        engine = DeduplicationEngine(
            DeduplicationSettings(near_duplicate_threshold=0.7, lsh_bands=32)
        )
        # A realistic body: many distinct sentences, so the shingle set is large
        # enough for a small edit to leave similarity high.
        body = "\n".join(
            f"Responsibility {index}: design, build and operate service {index} "
            f"with Python, FastAPI and PostgreSQL in production."
            for index in range(40)
        )
        engine.remember_job(engine.fingerprint(make_job(description=body)))

        reposted = engine.fingerprint(
            make_job(
                source="aggregator",
                source_job_id="2",
                title="Senior Python Engineer - Remote",
                # The edit is at the start, so the content fingerprint differs
                # and only similarity can catch this repost.
                description="Reposted by our partner network.\n" + body,
            )
        )
        verdict = engine.check(reposted)
        assert verdict.kind is DuplicateKind.NEAR
        assert verdict.similarity >= 0.7

    def test_distinct_postings_are_not_duplicates(self) -> None:
        engine = DeduplicationEngine()
        engine.remember_job(engine.fingerprint(make_job()))
        different = engine.fingerprint(
            make_job(
                source_job_id="2",
                title="Frontend React Developer",
                company_name="Globex",
                company_slug="globex",
                description="We need a React and TypeScript developer for our design system. " * 12,
            )
        )
        assert not engine.check(different).is_duplicate

    def test_batch_deduplicates_within_itself(self) -> None:
        engine = DeduplicationEngine()
        jobs = [make_job(), make_job(source="aggregator", source_job_id="2")]
        unique, duplicates, stats = engine.process_batch(jobs)
        assert len(unique) == 1
        assert len(duplicates) == 1
        assert stats.duplicate_rate == 0.5

    def test_priming_loads_the_comparison_window(self) -> None:
        engine = DeduplicationEngine()
        seed = engine.fingerprint(make_job())
        engine.prime(
            [
                KnownPosting(
                    job_id=seed.id,
                    content_hash=seed.content_hash,
                    signature=seed.similarity_signature,
                )
            ]
        )
        assert engine.indexed_count == 1
        assert engine.check(engine.fingerprint(make_job(id="c" * 32))).is_duplicate

    def test_disabled_engine_reports_no_duplicates(self) -> None:
        engine = DeduplicationEngine(DeduplicationSettings(enabled=False))
        engine.remember_job(engine.fingerprint(make_job()))
        assert not engine.check(make_job(id="d" * 32)).is_duplicate


class TestStatistics:
    def test_percentiles(self) -> None:
        values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        assert percentile(values, 50) == pytest.approx(5.5)
        assert percentile(values, 25) == pytest.approx(3.25)
        assert percentile([], 50) is None

    def test_percentile_range_is_validated(self) -> None:
        with pytest.raises(ValueError, match="between 0 and 100"):
            percentile([1, 2], 101)

    def test_mean_median_and_deviation(self) -> None:
        assert mean([2, 4, 6]) == 4
        assert median([3, 1, 2]) == 2
        assert standard_deviation([2, 2, 2]) == 0
        assert standard_deviation([1]) == 0

    def test_percent_change_handles_a_zero_baseline(self) -> None:
        assert percent_change(10, 5) == pytest.approx(100)
        assert percent_change(5, 0) is None

    def test_moving_average_keeps_the_series_length(self) -> None:
        result = moving_average([1, 2, 3, 4, 5], 3)
        assert len(result) == 5
        assert result[-1] == pytest.approx(4)

    def test_z_score(self) -> None:
        assert z_score(10, [1, 2, 3]) is not None
        assert z_score(1, [2, 2, 2]) is None

    def test_linear_slope_detects_direction(self) -> None:
        assert linear_slope([1, 2, 3, 4]) > 0
        assert linear_slope([4, 3, 2, 1]) < 0

    def test_fill_missing_days_produces_a_dense_series(self) -> None:
        start = date(2024, 1, 1)
        points = [(start, 5), (start + timedelta(days=2), 3)]
        dense = fill_missing_days(points, start=start, end=start + timedelta(days=3))
        assert [value for _day, value in dense] == [5, 0, 3, 0]

    def test_share_is_bounded(self) -> None:
        assert share(5, 10) == 0.5
        assert share(5, 0) == 0.0

    @pytest.mark.parametrize(
        ("change", "series", "expected"),
        [
            (25.0, [5, 6, 7, 8, 9, 10], TrendDirection.RISING),
            (-25.0, [10, 9, 8, 7, 6, 5], TrendDirection.DECLINING),
            (2.0, [10, 10, 10, 10, 10, 10], TrendDirection.STABLE),
            (None, [1, 2], TrendDirection.INSUFFICIENT_DATA),
            (5.0, [1, 40, 2, 38, 3, 42], TrendDirection.VOLATILE),
        ],
    )
    def test_trend_classification(
        self, change: float | None, series: list[float], expected: TrendDirection
    ) -> None:
        assert classify_trend(change_pct=change, series=series) is expected

    def test_coefficient_of_variation(self) -> None:
        assert coefficient_of_variation([5, 5, 5]) == 0.0
        assert coefficient_of_variation([]) == 0.0

    def test_confidence_grows_with_the_sample(self) -> None:
        assert confidence_from_sample(0) == 0.0
        assert confidence_from_sample(5) < confidence_from_sample(50)
        assert confidence_from_sample(1000) == 1.0


class TestQualityScoring:
    def test_a_complete_posting_scores_well(self, normalized_jobs: list[NormalizedJob]) -> None:
        assert max(score_job(job) for job in normalized_jobs) > 0.7

    def test_an_empty_posting_scores_badly(self) -> None:
        assert score_job(make_job(description="", skills=[])) < 0.6

    def test_report_covers_every_dimension(self, normalized_jobs: list[NormalizedJob]) -> None:
        report, events = QualityScorer().evaluate(
            source="test",
            jobs=normalized_jobs,
            signals=BatchSignals(records_received=len(normalized_jobs) + 5, records_rejected=5),
        )
        assert len(report.dimensions) == 6
        assert 0.0 <= report.score <= 1.0
        assert report.grade in {"A", "B", "C", "D", "F"}
        assert all(event.dimension in report.dimensions for event in events)

    def test_rejections_lower_validity(self, normalized_jobs: list[NormalizedJob]) -> None:
        clean, _ = QualityScorer().evaluate(
            source="test", jobs=normalized_jobs, signals=BatchSignals(records_received=50)
        )
        dirty, _ = QualityScorer().evaluate(
            source="test",
            jobs=normalized_jobs,
            signals=BatchSignals(records_received=50, records_rejected=25),
        )
        assert dirty.score < clean.score

    def test_field_level_quality_is_reported(self, normalized_jobs: list[NormalizedJob]) -> None:
        report, _ = QualityScorer().evaluate(
            source="test", jobs=normalized_jobs, signals=BatchSignals(records_received=60)
        )
        fields = {field.field: field for field in report.fields}
        assert fields["title"].completeness == 1.0
        assert 0.0 <= fields["salary"].completeness <= 1.0
