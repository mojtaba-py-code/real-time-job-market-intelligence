"""Security regression tests.

Each test pins down a specific attack the platform is designed to refuse. They
are the ones most worth keeping green: a refactor that quietly reopens one of
these holes is far more expensive than a broken feature.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.core.config import IngestionSettings, SecuritySettings, Settings
from app.core.errors import SourceConfigurationError, SourceUnavailableError, UnsafeUrlError
from app.core.logging import REDACTED, redact, scrub_text
from app.core.security import generate_api_key, sign_token, verify_secret, verify_token
from app.ingestion.http import UrlGuard, is_public_address
from app.ingestion.sources.dataset_source import resolve_within
from app.ingestion.sources.rss_source import parse_feed_safely
from app.services.admin_service import AdminService
from app.storage.repositories.jobs import JobQuery, JobRepository
from app.storage.session import Database

pytestmark = [pytest.mark.security, pytest.mark.integration]


@pytest.fixture
def client(database: Database, settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings, database=database)) as test_client:
        yield test_client


class TestSsrfProtection:
    """Outbound requests must never reach the private network."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8000/admin",
            "https://localhost/internal",
            "https://169.254.169.254/latest/meta-data/",
            "https://10.0.0.5/secrets",
            "https://192.168.1.1/",
            "https://[::1]/",
            "https://metadata.google.internal/",
        ],
    )
    async def test_private_and_metadata_targets_are_rejected(self, url: str) -> None:
        guard = UrlGuard(IngestionSettings(allowed_schemes=("http", "https")))
        with pytest.raises(UnsafeUrlError):
            await guard.validate(url)

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "gopher://example.com/",
            "ftp://example.com/",
            "javascript:alert(1)",
        ],
    )
    async def test_non_http_schemes_are_rejected(self, url: str) -> None:
        with pytest.raises(UnsafeUrlError):
            await UrlGuard(IngestionSettings()).validate(url)

    async def test_plain_http_is_rejected_by_default(self) -> None:
        with pytest.raises(UnsafeUrlError, match="scheme"):
            await UrlGuard(IngestionSettings()).validate("http://example.com/jobs")

    async def test_embedded_credentials_are_rejected(self) -> None:
        with pytest.raises(UnsafeUrlError, match="credentials"):
            await UrlGuard(IngestionSettings()).validate("https://user:pass@example.com/")

    async def test_oversized_urls_are_rejected(self) -> None:
        with pytest.raises(UnsafeUrlError):
            await UrlGuard(IngestionSettings()).validate("https://example.com/" + "a" * 4000)

    @pytest.mark.parametrize(
        ("address", "public"),
        [
            ("8.8.8.8", True),
            ("127.0.0.1", False),
            ("10.1.2.3", False),
            ("172.16.0.1", False),
            ("192.168.0.1", False),
            ("169.254.169.254", False),
            ("::1", False),
            ("not-an-ip", False),
        ],
    )
    def test_address_classification(self, address: str, public: bool) -> None:
        assert is_public_address(address) is public


class TestXmlSafety:
    """XML feeds must not be able to expand entities or read local files."""

    def test_doctype_declarations_are_refused(self) -> None:
        payload = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE lolz [<!ENTITY lol "lol">]>'
            "<rss><channel><item><title>&lol;</title></item></channel></rss>"
        )
        with pytest.raises(SourceUnavailableError, match="DTD or entity"):
            parse_feed_safely(payload)

    def test_external_entity_declarations_are_refused(self) -> None:
        payload = (
            '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            "<rss><channel><item><title>&xxe;</title></item></channel></rss>"
        )
        with pytest.raises(SourceUnavailableError):
            parse_feed_safely(payload)

    def test_well_formed_feeds_still_parse(self) -> None:
        root = parse_feed_safely(
            "<rss><channel><item><title>Python Developer</title></item></channel></rss>"
        )
        assert root.find(".//item") is not None


class TestPathTraversal:
    """Dataset paths come from configuration and must stay inside the data root."""

    @pytest.mark.parametrize(
        "candidate",
        ["../../etc/passwd", "../../../windows/system32/config/sam", "/etc/shadow"],
    )
    def test_traversal_outside_the_root_is_rejected(self, tmp_path: Path, candidate: str) -> None:
        with pytest.raises(SourceConfigurationError, match="outside"):
            resolve_within(candidate, [tmp_path])

    def test_paths_inside_the_root_are_accepted(self, tmp_path: Path) -> None:
        resolved = resolve_within("raw/jobs.jsonl", [tmp_path])
        assert str(resolved).startswith(str(tmp_path.resolve()))


class TestSqlInjection:
    """User input reaches the database only through bound parameters."""

    @pytest.mark.parametrize(
        "payload",
        [
            "'; DROP TABLE jobs; --",
            "' OR '1'='1",
            "1; DELETE FROM jobs WHERE 1=1; --",
            "python') UNION SELECT * FROM api_keys --",
        ],
    )
    async def test_search_input_cannot_alter_the_database(
        self, database: Database, payload: str
    ) -> None:
        async with database.session() as session:
            repository = JobRepository(session)
            jobs, total = await repository.search(JobQuery(text=payload, limit=5))
            assert jobs == []
            assert total == 0
            # The table is still there and still queryable.
            assert await repository.count(JobQuery(limit=1)) == 0

    async def test_injection_through_the_api_is_harmless(self, client: TestClient) -> None:
        response = client.get("/jobs/search", params={"q": "'; DROP TABLE jobs; --"})
        assert response.status_code == 200
        assert client.get("/health").json()["status"] == "healthy"


class TestAuthentication:
    async def test_admin_endpoints_require_a_key(self, client: TestClient) -> None:
        for path in ("/admin/stats", "/admin/keys"):
            assert client.get(path).status_code == 401

    async def test_malformed_keys_are_rejected(self, client: TestClient) -> None:
        for header in ("garbage", "jmi_short", "jmi_zzzzzzzzzzzz_secret"):
            response = client.get("/admin/stats", headers={"X-API-Key": header})
            assert response.status_code == 401

    async def test_keys_are_stored_hashed(self, database: Database, settings: Settings) -> None:
        from app.storage.repositories.auth import ApiKeyRepository

        generated = await AdminService(database, settings=settings).issue_api_key(name="x")
        async with database.read_session() as session:
            rows = await ApiKeyRepository(session).list_keys()
            hashed = rows[0].hashed_key
        assert generated.secret not in hashed
        assert hashed.startswith("$argon2")
        assert verify_secret(hashed, generated.full_key)

    def test_generated_keys_are_unpredictable(self) -> None:
        keys = {generate_api_key().full_key for _ in range(25)}
        assert len(keys) == 25

    def test_forged_tokens_are_rejected(self) -> None:
        from app.core.errors import AuthenticationError

        token = sign_token({"sub": "attacker"}, secret_key="a" * 48, ttl_seconds=60)
        with pytest.raises(AuthenticationError):
            verify_token(token, secret_key="b" * 48)


class TestTransportHardening:
    def test_security_headers_on_every_response(self, client: TestClient) -> None:
        headers = client.get("/health").headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert headers["Referrer-Policy"] == "no-referrer"

    def test_oversized_bodies_are_refused(self, database: Database, settings: Settings) -> None:
        tiny = settings.model_copy(
            update={"security": SecuritySettings(max_request_body_bytes=1024)}
        )
        with TestClient(create_app(tiny, database=database)) as client:
            response = client.post(
                "/profiles/market-fit",
                content=b"x" * 5000,
                headers={"Content-Type": "application/json"},
            )
        assert response.status_code == 413

    def test_errors_do_not_leak_internals(self, client: TestClient) -> None:
        body = client.get("/jobs/" + "0" * 32).json()
        assert "Traceback" not in str(body)
        assert "sqlalchemy" not in str(body).lower()
        assert body["error"]["request_id"]


class TestSecretHandling:
    def test_secrets_never_reach_the_logs(self) -> None:
        payload = redact(
            {
                "api_key": "jmi_abcdef012345_supersecret",
                "password": "hunter2",
                "authorization": "Bearer abc.def.ghi",
                "note": "contact me at person@example.com",
            }
        )
        assert payload["api_key"] == REDACTED
        assert payload["password"] == REDACTED
        assert payload["authorization"] == REDACTED
        assert "person@example.com" not in payload["note"]

    def test_bearer_tokens_in_free_text_are_scrubbed(self) -> None:
        assert "abcdef0123456789" not in scrub_text("Bearer abcdef0123456789")

    def test_settings_hide_the_secret_key(self, settings: Settings) -> None:
        assert "SecretStr" in repr(settings.security.secret_key)
        assert settings.security.secret_key.get_secret_value() not in str(settings)


class TestPrivacy:
    async def test_no_personal_contact_data_is_stored_on_postings(
        self, database: Database, normalized_jobs: list
    ) -> None:
        """The canonical schema has no field for candidate contact details."""
        from app.storage.models import Job

        columns = {column.name for column in Job.__table__.columns}
        forbidden = {"email", "phone", "candidate_name", "cv", "resume", "address"}
        assert columns.isdisjoint(forbidden)
