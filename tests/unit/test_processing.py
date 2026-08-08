"""Unit tests for cleaning, validation and normalization."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.timeutils import utcnow
from app.models.enums import (
    EmploymentType,
    ExperienceLevel,
    JobStatus,
    RejectionReason,
    RemoteType,
    SalaryPeriod,
    SalaryProvenance,
)
from app.models.raw import RawJob
from app.processing.cleaning.cleaner import RecordCleaner, is_null_token, repair_mojibake
from app.processing.normalization.company import CompanyNormalizer
from app.processing.normalization.location import LocationNormalizer
from app.processing.normalization.normalizer import JobNormalizer
from app.processing.normalization.salary import (
    SalaryParser,
    detect_currency,
    detect_period,
    load_currency_table,
    parse_amount,
)
from app.processing.validation.validator import RecordValidator

pytestmark = pytest.mark.unit


def make_raw(**overrides: object) -> RawJob:
    payload: dict[str, object] = {
        "source": "test",
        "source_job_id": "1",
        "title": "Python Developer",
        "description": "We are looking for a Python developer with FastAPI experience. " * 3,
    }
    payload.update(overrides)
    return RawJob(**payload)  # type: ignore[arg-type]


class TestCleaner:
    def test_html_is_stripped_from_every_text_field(self) -> None:
        cleaned, report = RecordCleaner().clean(
            make_raw(title="<b>Python</b> Developer", company="<i>Acme</i>")
        )
        assert cleaned.title == "Python Developer"
        assert cleaned.company == "Acme"
        assert report.html_removed

    def test_placeholder_values_become_none(self) -> None:
        cleaned, report = RecordCleaner().clean(make_raw(company="N/A", location="unknown"))
        assert cleaned.company is None
        assert cleaned.location is None
        assert "company" in report.fields_emptied

    def test_mojibake_is_repaired(self) -> None:
        repaired, changed = repair_mojibake("Weâ€™re hiring")
        assert repaired == "We're hiring"
        assert changed

    def test_clean_text_is_idempotent(self) -> None:
        cleaner = RecordCleaner()
        once, _ = cleaner.clean(make_raw(title="  Python   Developer  "))
        twice, report = cleaner.clean(once)
        assert twice.title == once.title
        assert not report.changed

    @pytest.mark.parametrize("value", ["", "n/a", "None", "  unknown  ", None])
    def test_null_tokens(self, value: str | None) -> None:
        assert is_null_token(value)


class TestValidator:
    def test_accepts_a_good_record(self) -> None:
        outcome = RecordValidator().validate(make_raw())
        assert outcome.valid
        assert outcome.rejection is None

    def test_rejects_a_missing_title(self) -> None:
        outcome = RecordValidator().validate(make_raw(title=None))
        assert outcome.rejected
        assert outcome.rejection is not None
        assert outcome.rejection.reason == RejectionReason.MISSING_REQUIRED_FIELD

    def test_rejects_a_short_description(self) -> None:
        outcome = RecordValidator().validate(make_raw(description="too short"))
        assert outcome.rejection is not None
        assert outcome.rejection.reason == RejectionReason.DESCRIPTION_TOO_SHORT

    def test_rejects_active_content(self) -> None:
        outcome = RecordValidator().validate(
            make_raw(description="<script>alert(1)</script> " + "x" * 100)
        )
        assert outcome.rejection is not None
        assert outcome.rejection.reason == RejectionReason.UNSAFE_CONTENT

    def test_rejects_future_timestamps(self) -> None:
        outcome = RecordValidator().validate(make_raw(published_at=utcnow() + timedelta(days=30)))
        assert outcome.rejection is not None
        assert outcome.rejection.reason == RejectionReason.FUTURE_TIMESTAMP

    def test_rejects_ancient_postings(self) -> None:
        outcome = RecordValidator().validate(make_raw(published_at=utcnow() - timedelta(days=3000)))
        assert outcome.rejection is not None
        assert outcome.rejection.reason == RejectionReason.STALE_POSTING

    def test_rejected_records_keep_a_payload_for_replay(self) -> None:
        outcome = RecordValidator().validate(make_raw(title=None))
        assert outcome.rejection is not None
        assert outcome.rejection.payload["description_preview"]

    def test_batch_splits_valid_and_invalid(self) -> None:
        accepted, rejected, summary = RecordValidator().validate_batch(
            [make_raw(), make_raw(source_job_id="2", title=None)]
        )
        assert len(accepted) == 1
        assert len(rejected) == 1
        assert summary.total == 2
        assert summary.rejected == 1

    def test_missing_optional_fields_are_tracked(self) -> None:
        outcome = RecordValidator().validate(make_raw())
        assert "salary" in outcome.missing_fields


class TestLocationNormalizer:
    @pytest.mark.parametrize(
        ("raw", "country", "city"),
        [
            ("Berlin, Germany", "DE", "Berlin"),
            ("Austin, TX", "US", "Austin"),
            ("US-TX-Austin", "US", "Austin"),
            ("Bengaluru, India", "IN", "Bengaluru"),
            ("London / Hybrid", "GB", "London"),
        ],
    )
    def test_resolves_geography(self, raw: str, country: str, city: str) -> None:
        info = LocationNormalizer().normalize(raw)
        assert info.country_code == country
        assert info.city == city
        assert info.confidence > 0.5

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Remote", RemoteType.REMOTE),
            ("Berlin (Hybrid)", RemoteType.HYBRID),
            ("On-site, Munich", RemoteType.ONSITE),
            ("Berlin", RemoteType.UNKNOWN),
        ],
    )
    def test_detects_work_arrangement(self, raw: str, expected: RemoteType) -> None:
        assert LocationNormalizer().normalize(raw).remote_hint is expected

    def test_remote_hint_field_wins(self) -> None:
        info = LocationNormalizer().normalize("Berlin", remote_hint="true")
        assert info.remote_hint is RemoteType.REMOTE

    def test_unknown_location_is_reported_honestly(self) -> None:
        info = LocationNormalizer().normalize(None)
        assert not info.is_resolved
        assert info.confidence == 0.0


class TestSalaryParser:
    @pytest.mark.parametrize(
        ("text", "low", "high", "currency", "period"),
        [
            ("45,000 - 60,000 EUR per year", 45_000, 60_000, "EUR", SalaryPeriod.YEARLY),
            ("$120k - $150k", 120_000, 150_000, "USD", SalaryPeriod.YEARLY),
            ("60.000 - 80.000 EUR brutto/Jahr", 60_000, 80_000, "EUR", SalaryPeriod.YEARLY),
            ("50 USD/hour", 50, None, "USD", SalaryPeriod.HOURLY),
            ("PLN 15 000 - 20 000 monthly", 15_000, 20_000, "PLN", SalaryPeriod.MONTHLY),
        ],
    )
    def test_parses_ranges(
        self,
        text: str,
        low: float,
        high: float | None,
        currency: str,
        period: SalaryPeriod,
    ) -> None:
        salary = SalaryParser().parse(text)
        assert salary.provenance is SalaryProvenance.OBSERVED
        assert salary.min_amount == pytest.approx(low)
        assert salary.currency == currency
        assert salary.period is period
        if high is not None:
            assert salary.max_amount == pytest.approx(high)

    @pytest.mark.parametrize(
        "text", ["Competitive salary", "Attractive package", "", None, "Founded in 2018"]
    )
    def test_never_invents_a_salary(self, text: str | None) -> None:
        salary = SalaryParser().parse(text)
        assert salary.provenance is SalaryProvenance.UNKNOWN
        assert salary.min_amount is None
        assert salary.annual_min is None

    def test_annualises_and_converts_to_the_base_currency(self) -> None:
        salary = SalaryParser().parse("5,000 EUR per month")
        assert salary.normalized_currency == "USD"
        assert salary.annual_min == pytest.approx(5_000 * 12 * 1.09, rel=1e-6)

    def test_implausible_values_are_discarded(self) -> None:
        assert SalaryParser().parse("3 USD per year").provenance is SalaryProvenance.UNKNOWN

    @pytest.mark.parametrize(
        ("token", "suffix", "expected"),
        [
            ("60,000", None, 60_000),
            ("60.000", None, 60_000),
            ("60", "k", 60_000),
            ("1.5", "M", 1_500_000),
        ],
    )
    def test_parse_amount(self, token: str, suffix: str | None, expected: float) -> None:
        assert parse_amount(token, suffix) == pytest.approx(expected)

    def test_currency_and_period_detection(self) -> None:
        assert detect_currency("100 GBP") == "GBP"
        assert detect_period("per annum") is SalaryPeriod.YEARLY

    def test_currency_table_is_dated(self) -> None:
        table = load_currency_table()
        assert table.base == "USD"
        assert table.as_of is not None
        assert table.convert(100, "EUR") is not None
        assert table.convert(100, "XYZ") is None


class TestCompanyNormalizer:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Acme GmbH", "acme"),
            ("ACME GMBH", "acme"),
            ("Acme Inc.", "acme"),
            ("Acme (via TalentPartners)", "acme"),
            ("Acme Holding GmbH & Co. KG", "acme-holding"),
        ],
    )
    def test_collapses_legal_forms(self, raw: str, expected: str) -> None:
        assert CompanyNormalizer().normalize(raw).slug == expected

    def test_unknown_company_is_explicit(self) -> None:
        result = CompanyNormalizer().normalize(None)
        assert result.slug == "unknown"
        assert not result.is_known

    def test_short_acronyms_keep_their_casing(self) -> None:
        assert CompanyNormalizer().normalize("IBM").name == "IBM"


class TestJobNormalizer:
    def test_produces_a_complete_canonical_record(self, sample_raw_job: RawJob) -> None:
        job = JobNormalizer().normalize(sample_raw_job)

        assert job.normalized_title == "Senior Backend Engineer"
        assert job.title_family == "Backend Engineering"
        assert job.experience_level is ExperienceLevel.SENIOR
        assert job.experience_years_min == 6
        assert job.company_name == "Northwind Analytics"
        assert job.location.country_code == "DE"
        assert job.remote_type is RemoteType.HYBRID
        assert job.employment_type is EmploymentType.FULL_TIME
        assert job.salary.is_observed
        assert job.salary.normalized_currency == "USD"
        assert {"python", "fastapi", "postgresql", "docker", "kubernetes"} <= set(job.skill_slugs)
        assert job.status is JobStatus.NORMALIZED
        assert job.canonical_url is not None
        assert "utm_source" not in job.canonical_url

    def test_batch_statistics_are_collected(self, raw_jobs: list[RawJob]) -> None:
        jobs, stats = JobNormalizer().normalize_batch(raw_jobs[:20])
        assert len(jobs) == 20
        assert stats.normalized == 20
        assert stats.with_skills > 0
        assert stats.total_ms > 0
