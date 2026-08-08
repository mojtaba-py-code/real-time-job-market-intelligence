"""Unit tests for the cross-cutting core utilities."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import DeduplicationSettings, Environment, Settings
from app.core.errors import AuthenticationError
from app.core.hashing import (
    content_fingerprint,
    normalize_url,
    sha256_hex,
    stable_bucket,
    url_fingerprint,
)
from app.core.logging import REDACTED, mask_email, redact, scrub_text
from app.core.security import (
    generate_api_key,
    hash_secret,
    parse_api_key,
    sign_token,
    verify_secret,
    verify_token,
)
from app.core.text import (
    canonical_key,
    clean_text,
    collapse_whitespace,
    contains_word,
    shingles,
    slugify,
    strip_html,
    tokenize,
    truncate,
)
from app.core.timeutils import ensure_utc, humanize_duration, parse_datetime, utcnow

pytestmark = pytest.mark.unit


class TestText:
    def test_strip_html_drops_scripts_and_keeps_text(self) -> None:
        html = "<div><p>Hello <b>World</b></p><script>alert(1)</script></div>"
        assert "alert" not in strip_html(html)
        assert "Hello" in strip_html(html)

    def test_strip_html_survives_malformed_markup(self) -> None:
        assert "Python" in strip_html("<p>Python <<>> developer")

    def test_clean_text_normalizes_entities_and_whitespace(self) -> None:
        assert clean_text("<p>Hello&nbsp;&amp;   welcome</p>") == "Hello & welcome"

    def test_clean_text_preserves_paragraphs_when_asked(self) -> None:
        result = clean_text("<p>One</p><p>Two</p>", keep_newlines=True)
        assert "One" in result and "Two" in result and "\n" in result

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Senior C++ Developer", ["senior", "c++", "developer"]),
            ("Node.js / TypeScript", ["node.js", "typescript"]),
            ("", []),
        ],
    )
    def test_tokenize(self, raw: str, expected: list[str]) -> None:
        assert tokenize(raw) == expected

    def test_canonical_key_strips_accents_and_punctuation(self) -> None:
        assert canonical_key("Zürich, Schweiz!") == "zurich schweiz"

    def test_slugify(self) -> None:
        assert slugify("Acme Analytics GmbH") == "acme-analytics-gmbh"

    def test_shingles_produce_overlapping_ngrams(self) -> None:
        result = shingles(["a", "b", "c", "d"], size=2)
        assert result == {"a b", "b c", "c d"}

    def test_shingles_on_short_input_return_tokens(self) -> None:
        assert shingles(["a", "b"], size=5) == {"a", "b"}

    def test_collapse_whitespace(self) -> None:
        assert collapse_whitespace("  a   b \n c ") == "a b c"

    def test_truncate_adds_ellipsis(self) -> None:
        assert truncate("abcdefghij", 6) == "abc..."

    def test_contains_word_respects_boundaries(self) -> None:
        assert contains_word("we use go daily", "go")
        assert not contains_word("golang is great", "go")


class TestHashing:
    def test_normalize_url_removes_tracking_and_sorts_query(self) -> None:
        assert (
            normalize_url("HTTPS://WWW.Example.com:443/jobs/1/?utm_source=x&b=2&a=1#frag")
            == "https://example.com/jobs/1?a=1&b=2"
        )

    def test_normalize_url_is_stable_for_equivalent_urls(self) -> None:
        left = normalize_url("https://example.com/a?x=1&y=2")
        right = normalize_url("http://www.example.com/a?y=2&x=1".replace("http", "https"))
        assert left == right

    def test_url_fingerprint_is_empty_without_a_url(self) -> None:
        assert url_fingerprint(None) == ""

    def test_content_fingerprint_ignores_cosmetic_differences(self) -> None:
        left = content_fingerprint(
            company="Acme", title="Python Dev", location="Berlin", description="Build things"
        )
        right = content_fingerprint(
            company="  ACME ", title="python dev", location="berlin", description="Build things"
        )
        assert left == right

    def test_content_fingerprint_changes_with_content(self) -> None:
        left = content_fingerprint(company="A", title="T", location="L", description="one")
        right = content_fingerprint(company="A", title="T", location="L", description="two")
        assert left != right

    def test_sha256_is_deterministic(self) -> None:
        assert sha256_hex("a", "b") == sha256_hex("a", "b")

    def test_stable_bucket_is_within_range(self) -> None:
        assert 0 <= stable_bucket("value", 8) < 8


class TestTimeUtils:
    @pytest.mark.parametrize(
        "value",
        [
            "2024-03-01T10:00:00Z",
            "2024-03-01 10:00:00",
            "Fri, 01 Mar 2024 10:00:00 GMT",
            1709287200,
        ],
    )
    def test_parse_datetime_handles_common_formats(self, value: object) -> None:
        parsed = parse_datetime(value)
        assert parsed is not None
        assert parsed.tzinfo is not None
        assert parsed.year == 2024

    def test_parse_datetime_returns_none_for_garbage(self) -> None:
        assert parse_datetime("not a date") is None
        assert parse_datetime(None) is None

    def test_ensure_utc_attaches_timezone(self) -> None:
        naive = datetime(2024, 1, 1, 12, 0, 0)  # noqa: DTZ001 - deliberately naive
        assert ensure_utc(naive).tzinfo == UTC

    def test_humanize_duration(self) -> None:
        assert humanize_duration(0.25).endswith("ms")
        assert humanize_duration(42).endswith("s")
        assert "m" in humanize_duration(125)


class TestSecurity:
    def test_api_key_round_trip(self) -> None:
        generated = generate_api_key()
        key_id, presented = parse_api_key(generated.full_key)
        assert key_id == generated.key_id
        assert verify_secret(generated.hashed, presented)

    def test_api_key_rejects_wrong_secret(self) -> None:
        generated = generate_api_key()
        assert not verify_secret(generated.hashed, generated.full_key + "x")

    @pytest.mark.parametrize("raw", ["", "nope", "jmi_short", "abc_123456789012_secret"])
    def test_parse_api_key_rejects_malformed_input(self, raw: str) -> None:
        with pytest.raises(AuthenticationError):
            parse_api_key(raw)

    def test_hash_secret_refuses_empty_input(self) -> None:
        with pytest.raises(ValueError, match="empty secret"):
            hash_secret("")

    def test_token_round_trip(self) -> None:
        secret = "s" * 48
        token = sign_token({"sub": "user"}, secret_key=secret, ttl_seconds=60)
        assert verify_token(token, secret_key=secret)["sub"] == "user"

    def test_token_signature_is_checked(self) -> None:
        token = sign_token({"sub": "user"}, secret_key="a" * 48, ttl_seconds=60)
        with pytest.raises(AuthenticationError, match="signature"):
            verify_token(token, secret_key="b" * 48)

    def test_expired_token_is_rejected(self) -> None:
        secret = "s" * 48
        token = sign_token({"sub": "user"}, secret_key=secret, ttl_seconds=-10)
        with pytest.raises(AuthenticationError, match="expired"):
            verify_token(token, secret_key=secret)

    def test_tampered_payload_is_rejected(self) -> None:
        secret = "s" * 48
        token = sign_token({"sub": "user"}, secret_key=secret, ttl_seconds=60)
        body, signature = token.split(".")
        with pytest.raises(AuthenticationError):
            verify_token(body[:-2] + "AA." + signature, secret_key=secret)


class TestLoggingRedaction:
    def test_sensitive_keys_are_redacted(self) -> None:
        payload = redact({"password": "hunter2", "api_key": "jmi_x", "count": 3})
        assert payload["password"] == REDACTED
        assert payload["api_key"] == REDACTED
        assert payload["count"] == 3

    def test_emails_are_masked(self) -> None:
        assert mask_email("someone@example.com").startswith("s*")
        assert "someone" not in mask_email("someone@example.com")

    def test_bearer_tokens_are_scrubbed(self) -> None:
        assert REDACTED in scrub_text("Authorization: Bearer abcdef0123456789")

    def test_nested_structures_are_redacted(self) -> None:
        payload = redact({"outer": {"secret": "x", "ok": 1}})
        assert payload["outer"]["secret"] == REDACTED


class TestConfiguration:
    def test_development_generates_an_ephemeral_secret(self) -> None:
        settings = Settings(environment=Environment.DEVELOPMENT)
        assert len(settings.security.secret_key.get_secret_value()) >= 32

    def test_production_requires_an_explicit_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import DatabaseSettings

        monkeypatch.delenv("JOBINTEL_SECURITY__SECRET_KEY", raising=False)
        with pytest.raises(ValueError, match="SECRET_KEY"):
            Settings(
                environment=Environment.PRODUCTION,
                database=DatabaseSettings(url="postgresql+asyncpg://u:p@h/db"),
            )

    def test_production_rejects_sqlite(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JOBINTEL_SECURITY__SECRET_KEY", "k" * 48)
        with pytest.raises(ValueError, match="SQLite"):
            Settings(environment=Environment.PRODUCTION)

    def test_production_rejects_wildcard_cors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import ApiSettings, DatabaseSettings

        monkeypatch.setenv("JOBINTEL_SECURITY__SECRET_KEY", "k" * 48)
        with pytest.raises(ValueError, match="wildcard CORS"):
            Settings(
                environment=Environment.PRODUCTION,
                database=DatabaseSettings(url="postgresql+asyncpg://u:p@h/db"),
                api=ApiSettings(cors_origins=["*"]),
            )

    def test_deduplication_bands_must_divide_permutations(self) -> None:
        with pytest.raises(ValueError, match="divisible"):
            DeduplicationSettings(minhash_permutations=128, lsh_bands=7)

    def test_window_bounds_are_enforced(self) -> None:
        with pytest.raises(ValueError, match="default_page_size"):
            from app.core.config import ApiSettings

            Settings(api=ApiSettings(default_page_size=100, max_page_size=50))


def test_utcnow_is_timezone_aware() -> None:
    now = utcnow()
    assert now.tzinfo is not None
    assert abs((now - datetime.now(UTC)).total_seconds()) < timedelta(seconds=5).total_seconds()
