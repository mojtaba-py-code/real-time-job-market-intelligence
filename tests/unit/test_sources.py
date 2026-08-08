"""Unit tests for the source adapters and the field mapper."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from app.core.config import IngestionSettings, Settings
from app.core.errors import (
    ResponseTooLargeError,
    SourceConfigurationError,
    SourceUnavailableError,
)
from app.ingestion.base import SourceContext
from app.ingestion.http import SafeHttpClient
from app.ingestion.mapping import RecordMapper, extract_path, stringify
from app.ingestion.registry import SourceDefinition, SourceRegistry
from app.ingestion.sources.api_source import ApiJobSource
from app.ingestion.sources.company_career import CompanyCareerSource
from app.ingestion.sources.dataset_source import DatasetSource
from app.ingestion.sources.rss_source import RssJobSource
from app.ingestion.sources.synthetic_source import (
    SyntheticConfig,
    SyntheticJobGenerator,
    SyntheticJobSource,
)
from app.models.enums import SourceKind

pytestmark = pytest.mark.unit


@pytest.fixture
def permissive_client() -> SafeHttpClient:
    """An HTTP client that allows the loopback host respx intercepts."""
    return SafeHttpClient(
        IngestionSettings(
            allow_private_networks=True,
            allowed_schemes=("http", "https"),
            respect_robots_txt=False,
            per_host_delay_seconds=0.0,
            max_retries=0,
        )
    )


class TestMapping:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("a.b", 1),
            ("list.0.name", "first"),
            ("list.-1.name", "second"),
            ("missing", None),
            ("a.b.c", None),
        ],
    )
    def test_extract_path(self, path: str, expected: object) -> None:
        payload = {"a": {"b": 1}, "list": [{"name": "first"}, {"name": "second"}]}
        assert extract_path(payload, path) == expected

    def test_mapper_uses_declared_paths(self) -> None:
        mapper = RecordMapper({"title": "position", "company": "employer.name"})
        fields = mapper.to_fields({"position": "Engineer", "employer": {"name": "Acme"}})
        assert fields["title"] == "Engineer"
        assert fields["company"] == "Acme"

    def test_mapper_falls_back_to_conventional_names(self) -> None:
        fields = RecordMapper().to_fields({"job_title": "Engineer", "company_name": "Acme"})
        assert fields["title"] == "Engineer"
        assert fields["company"] == "Acme"

    def test_literals_are_supported(self) -> None:
        assert RecordMapper({"remote_status": "@remote"}).resolve("remote_status", {}) == "remote"

    def test_unknown_targets_are_rejected(self) -> None:
        with pytest.raises(SourceConfigurationError, match="unknown mapping"):
            RecordMapper({"not_a_field": "x"})

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("  text  ", "text"),
            (42, "42"),
            (True, "true"),
            ({"name": "Acme"}, "Acme"),
            (["a", "b"], "a, b"),
            (None, None),
            ("", None),
        ],
    )
    def test_stringify(self, value: object, expected: str | None) -> None:
        assert stringify(value) == expected


class TestApiSource:
    @respx.mock
    async def test_maps_records_into_raw_jobs(self, permissive_client: SafeHttpClient) -> None:
        respx.get("https://127.0.0.1/api/jobs").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "42",
                            "title": "Python Developer",
                            "description": "We use Python and FastAPI daily.",
                            "company": {"name": "Acme"},
                            "location": {"display_name": "Berlin, Germany"},
                            "absolute_url": "https://127.0.0.1/api/jobs/42",
                            "created_at": "2024-03-01T10:00:00Z",
                        }
                    ]
                },
            )
        )
        source = ApiJobSource(
            name="test_api",
            options={
                "url": "https://127.0.0.1/api/jobs",
                "records_path": "results",
                "mapping": {
                    "source_job_id": "id",
                    "company": "company.name",
                    "location": "location.display_name",
                    "url": "absolute_url",
                    "published_at": "created_at",
                },
            },
            client=permissive_client,
        )
        batch = await source.fetch_jobs(SourceContext(limit=10))
        await permissive_client.aclose()

        assert batch.size == 1
        job = batch.jobs[0]
        assert job.source_job_id == "42"
        assert job.company == "Acme"
        assert job.location == "Berlin, Germany"
        assert job.published_at is not None
        assert job.raw_payload["id"] == "42"

    @respx.mock
    async def test_pagination_stops_at_the_limit(self, permissive_client: SafeHttpClient) -> None:
        def page(request: httpx.Request) -> httpx.Response:
            number = int(request.url.params.get("page", 1))
            return httpx.Response(
                200,
                json=[
                    {"id": f"{number}-{i}", "title": "Engineer", "description": "x" * 60}
                    for i in range(5)
                ],
            )

        respx.get("https://127.0.0.1/api/jobs").mock(side_effect=page)
        source = ApiJobSource(
            name="paged",
            options={
                "url": "https://127.0.0.1/api/jobs",
                "pagination": {"mode": "page", "size": 5},
            },
            client=permissive_client,
        )
        batch = await source.fetch_jobs(SourceContext(limit=12))
        await permissive_client.aclose()
        assert batch.size == 12

    @respx.mock
    async def test_http_errors_surface(self, permissive_client: SafeHttpClient) -> None:
        respx.get("https://127.0.0.1/api/jobs").mock(return_value=httpx.Response(404))
        source = ApiJobSource(
            name="broken", options={"url": "https://127.0.0.1/api/jobs"}, client=permissive_client
        )
        with pytest.raises(SourceUnavailableError):
            await source.fetch_jobs(SourceContext(limit=5))
        await permissive_client.aclose()

    @respx.mock
    async def test_oversized_responses_are_refused(self) -> None:
        client = SafeHttpClient(
            IngestionSettings(
                allow_private_networks=True,
                allowed_schemes=("http", "https"),
                respect_robots_txt=False,
                per_host_delay_seconds=0.0,
                max_response_bytes=1024,
                max_retries=0,
            )
        )
        respx.get("https://127.0.0.1/api/big").mock(
            return_value=httpx.Response(200, json=[{"id": str(i)} for i in range(5000)])
        )
        source = ApiJobSource(
            name="big", options={"url": "https://127.0.0.1/api/big"}, client=client
        )
        with pytest.raises(ResponseTooLargeError):
            await source.fetch_jobs(SourceContext(limit=5))
        await client.aclose()

    def test_missing_url_is_a_configuration_error(self, permissive_client: SafeHttpClient) -> None:
        with pytest.raises(SourceConfigurationError, match="url"):
            ApiJobSource(name="no-url", options={}, client=permissive_client)

    def test_unsupported_pagination_mode_is_rejected(
        self, permissive_client: SafeHttpClient
    ) -> None:
        with pytest.raises(SourceConfigurationError, match="pagination"):
            ApiJobSource(
                name="bad",
                options={"url": "https://127.0.0.1/api/x", "pagination": {"mode": "telepathy"}},
                client=permissive_client,
            )

    def test_credentials_come_from_the_environment(
        self, permissive_client: SafeHttpClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = ApiJobSource(
            name="auth",
            options={"url": "https://127.0.0.1/api/x", "api_key_env": "TEST_TOKEN"},
            client=permissive_client,
        )
        monkeypatch.delenv("TEST_TOKEN", raising=False)
        with pytest.raises(SourceConfigurationError, match="TEST_TOKEN"):
            source._auth_headers()

        monkeypatch.setenv("TEST_TOKEN", "secret-value")
        assert source._auth_headers() == {"Authorization": "Bearer secret-value"}


class TestRssSource:
    @respx.mock
    async def test_parses_an_rss_feed(self, permissive_client: SafeHttpClient) -> None:
        feed = """<?xml version="1.0"?>
        <rss version="2.0"><channel>
          <item>
            <title>Senior Python Developer</title>
            <link>https://jobs.test/1</link>
            <guid>job-1</guid>
            <description>&lt;p&gt;We use Python and Django.&lt;/p&gt;</description>
            <pubDate>Fri, 01 Mar 2024 10:00:00 GMT</pubDate>
          </item>
        </channel></rss>"""
        respx.get("https://127.0.0.1/feed.xml").mock(return_value=httpx.Response(200, text=feed))
        source = RssJobSource(
            name="feed",
            options={"url": "https://127.0.0.1/feed.xml", "company": "Test Ltd"},
            client=permissive_client,
        )
        batch = await source.fetch_jobs(SourceContext(limit=10))
        await permissive_client.aclose()

        job = batch.jobs[0]
        assert job.source_job_id == "job-1"
        assert job.title == "Senior Python Developer"
        assert "<p>" not in (job.description or "")
        assert job.company == "Test Ltd"
        assert job.published_at is not None

    @respx.mock
    async def test_malformed_xml_is_reported(self, permissive_client: SafeHttpClient) -> None:
        respx.get("https://127.0.0.1/bad.xml").mock(
            return_value=httpx.Response(200, text="<rss><channel><item>")
        )
        source = RssJobSource(
            name="bad", options={"url": "https://127.0.0.1/bad.xml"}, client=permissive_client
        )
        with pytest.raises(SourceUnavailableError, match="malformed"):
            await source.fetch_jobs(SourceContext(limit=5))
        await permissive_client.aclose()


class TestCompanyCareerSource:
    @respx.mock
    async def test_greenhouse_preset(self, permissive_client: SafeHttpClient) -> None:
        respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
            return_value=httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "id": 7,
                            "title": "Backend Engineer",
                            "content": "Python, PostgreSQL and Docker.",
                            "absolute_url": "https://boards.greenhouse.io/acme/jobs/7",
                            "location": {"name": "Remote"},
                            "updated_at": "2024-02-01T09:00:00Z",
                        }
                    ]
                },
            )
        )
        source = CompanyCareerSource(
            name="acme_careers",
            options={"provider": "greenhouse", "board": "acme", "company": "Acme Inc"},
            client=permissive_client,
        )
        batch = await source.fetch_jobs(SourceContext(limit=5))
        await permissive_client.aclose()

        job = batch.jobs[0]
        assert job.company == "Acme Inc"
        assert job.source_kind is SourceKind.CAREER_PAGE
        assert job.location == "Remote"


class TestDatasetSource:
    def _write(self, path: Path, rows: list[dict[str, object]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")

    async def test_reads_jsonl(self, tmp_path: Path, settings: Settings) -> None:
        dataset = tmp_path / "raw" / "jobs.jsonl"
        self._write(
            dataset,
            [
                {
                    "id": str(index),
                    "title": f"Engineer {index}",
                    "description": "Python and FastAPI. " * 5,
                    "company": "Acme",
                }
                for index in range(7)
            ],
        )
        source = DatasetSource(
            name="local", options={"path": "raw/jobs.jsonl"}, allowed_roots=[tmp_path]
        )
        batch = await source.fetch_jobs(SourceContext(limit=5))
        assert batch.size == 5
        assert batch.cursor == "5"

        second = await source.fetch_jobs(SourceContext(limit=5, cursor=batch.cursor))
        assert second.size == 2

    async def test_reads_csv(self, tmp_path: Path) -> None:
        dataset = tmp_path / "jobs.csv"
        dataset.write_text(
            "id,title,description,company\n1,Engineer,Python work here,Acme\n", encoding="utf-8"
        )
        source = DatasetSource(name="csv", options={"path": "jobs.csv"}, allowed_roots=[tmp_path])
        batch = await source.fetch_jobs(SourceContext(limit=5))
        assert batch.size == 1
        assert batch.jobs[0].title == "Engineer"

    def test_unsupported_format_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "jobs.txt").write_text("nope", encoding="utf-8")
        with pytest.raises(SourceConfigurationError, match="unsupported dataset format"):
            DatasetSource(name="txt", options={"path": "jobs.txt"}, allowed_roots=[tmp_path])


class TestSyntheticSource:
    async def test_generates_a_deterministic_batch(self) -> None:
        source = SyntheticJobSource(options={"count": 50, "days": 60, "seed": 99})
        first = await source.fetch_jobs(SourceContext(limit=20))
        second = await source.fetch_jobs(SourceContext(limit=20))
        assert first.size == 20
        assert [job.source_job_id for job in first.jobs] == [
            job.source_job_id for job in second.jobs
        ]

    async def test_cursor_advances_the_stream(self) -> None:
        source = SyntheticJobSource(options={"count": 50, "seed": 99})
        first = await source.fetch_jobs(SourceContext(limit=10))
        second = await source.fetch_jobs(SourceContext(limit=10, cursor=first.cursor))
        assert first.jobs[0].source_job_id != second.jobs[0].source_job_id

    def test_known_trends_are_reproduced(self) -> None:
        from collections import Counter

        jobs = SyntheticJobGenerator(SyntheticConfig(count=3_000, days=120, seed=7)).generate()
        early: Counter[str] = Counter()
        late: Counter[str] = Counter()
        half = len(jobs) // 2
        for index, job in enumerate(jobs):
            (early if index < half else late).update(job.raw_payload.get("skills", []))

        # raw_payload records the display names the generator emitted.
        assert late["FastAPI"] > early["FastAPI"] * 1.3, "FastAPI should be rising"
        assert late["Kubernetes"] > early["Kubernetes"], "Kubernetes should be rising"
        assert late["COBOL"] < early["COBOL"] * 0.8, "COBOL should be declining"

    def test_broken_records_are_emitted_on_purpose(self) -> None:
        jobs = SyntheticJobGenerator(
            SyntheticConfig(count=500, malformed_rate=0.05, seed=3)
        ).generate()
        assert any(len(job.description or "") < 40 for job in jobs)


class TestRegistry:
    def test_loads_the_shipped_configuration(self, project_root: Path) -> None:
        registry = SourceRegistry.from_file(project_root / "configs" / "sources.yaml")
        names = {definition.name for definition in registry.definitions()}
        assert "synthetic" in names
        assert registry.definitions(enabled_only=True)

    def test_missing_file_falls_back_to_synthetic(self, tmp_path: Path) -> None:
        registry = SourceRegistry.from_file(tmp_path / "absent.yaml")
        assert [d.name for d in registry.definitions()] == ["synthetic"]

    def test_unknown_source_is_an_error(self) -> None:
        registry = SourceRegistry([])
        with pytest.raises(SourceConfigurationError, match="unknown source"):
            registry.get_definition("ghost")

    def test_duplicate_names_are_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "sources.yaml"
        path.write_text(
            "sources:\n  - name: dup\n    kind: synthetic\n  - name: dup\n    kind: synthetic\n",
            encoding="utf-8",
        )
        with pytest.raises(SourceConfigurationError, match="duplicate"):
            SourceRegistry.from_file(path)

    def test_unknown_kind_is_rejected(self) -> None:
        with pytest.raises(SourceConfigurationError, match="unknown kind"):
            SourceDefinition.from_mapping({"name": "x", "kind": "telepathy"})

    def test_builds_every_adapter_kind(self, settings: Settings, tmp_path: Path) -> None:
        (tmp_path / "jobs.jsonl").write_text("", encoding="utf-8")
        registry = SourceRegistry(
            [
                SourceDefinition(name="syn", kind=SourceKind.SYNTHETIC),
                SourceDefinition(
                    name="api", kind=SourceKind.API, options={"url": "https://127.0.0.1/j"}
                ),
                SourceDefinition(
                    name="rss", kind=SourceKind.RSS, options={"url": "https://127.0.0.1/f"}
                ),
                SourceDefinition(
                    name="careers",
                    kind=SourceKind.CAREER_PAGE,
                    options={"provider": "lever", "board": "acme"},
                ),
            ],
            settings=settings,
        )
        for name in ("syn", "api", "rss", "careers"):
            assert registry.create(name).name == name
