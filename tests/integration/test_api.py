"""Integration tests for the HTTP API."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.core.config import ApiSettings, Settings
from app.ingestion.registry import SourceRegistry
from app.services.admin_service import AdminService
from app.services.ingestion_service import IngestionService
from app.storage.session import Database

pytestmark = pytest.mark.integration


@pytest.fixture
async def populated(
    database: Database, synthetic_registry: SourceRegistry, settings: Settings
) -> Database:
    """A database holding one synthetic ingestion run."""
    service = IngestionService(database=database, registry=synthetic_registry, settings=settings)
    await service.run_source("synthetic")
    await AdminService(database, settings=settings).sync_taxonomy()
    return database


@pytest.fixture
def client(populated: Database, settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings, database=populated)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
async def admin_key(populated: Database, settings: Settings) -> str:
    from app.models.enums import UserRole

    generated = await AdminService(populated, settings=settings).issue_api_key(
        name="tests", role=UserRole.ADMIN
    )
    return generated.full_key


class TestHealth:
    def test_health_reports_healthy(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "healthy"
        assert body["checks"]["database"] is True

    def test_liveness_and_readiness(self, client: TestClient) -> None:
        assert client.get("/health/live").json()["status"] == "alive"
        assert client.get("/health/ready").json()["status"] == "ready"

    def test_version(self, client: TestClient) -> None:
        assert client.get("/version").json()["version"]

    def test_security_headers_are_present(self, client: TestClient) -> None:
        headers = client.get("/health").headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert "Content-Security-Policy" in headers
        assert headers["X-Request-ID"]


class TestJobsApi:
    def test_list_is_paginated(self, client: TestClient) -> None:
        body = client.get("/jobs", params={"limit": 5}).json()
        assert len(body["items"]) <= 5
        assert body["meta"]["total"] >= len(body["items"])
        assert body["meta"]["limit"] == 5

    def test_filters_are_applied(self, client: TestClient) -> None:
        body = client.get("/jobs", params={"remote_type": "remote", "limit": 10}).json()
        assert all(item["remote_type"] == "remote" for item in body["items"])

    def test_search_reports_its_backend(self, client: TestClient) -> None:
        body = client.get("/jobs/search", params={"q": "python", "limit": 5}).json()
        assert body["search"]["backend"] in {"sql", "postgres_fts"}
        assert body["search"]["took_ms"] >= 0

    def test_detail_and_similar(self, client: TestClient) -> None:
        listing = client.get("/jobs", params={"limit": 1}).json()
        job_id = listing["items"][0]["id"]

        detail = client.get(f"/jobs/{job_id}").json()
        assert detail["id"] == job_id
        assert detail["description"]

        similar = client.get(f"/jobs/{job_id}/similar").json()
        assert all(item["id"] != job_id for item in similar)

    def test_missing_job_returns_a_structured_error(self, client: TestClient) -> None:
        response = client.get("/jobs/" + "0" * 32)
        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == "not_found"
        assert error["request_id"]

    def test_invalid_identifier_is_rejected(self, client: TestClient) -> None:
        assert client.get("/jobs/NOT-A-VALID-ID").status_code == 422

    def test_facets_and_suggestions(self, client: TestClient) -> None:
        facets = client.get("/jobs/facets").json()
        assert "remote_type" in facets and "skills" in facets
        assert "suggestions" in client.get("/jobs/suggest", params={"prefix": "so"}).json()


class TestAnalyticsApi:
    @pytest.mark.parametrize(
        "path",
        [
            "/analytics/market",
            "/analytics/volume",
            "/analytics/skills",
            "/analytics/skills/trends",
            "/analytics/emerging-skills",
            "/analytics/co-occurrence",
            "/analytics/skill-graph",
            "/analytics/salaries",
            "/analytics/locations",
            "/analytics/remote",
            "/analytics/seniority",
            "/analytics/segments",
            "/analytics/companies",
        ],
    )
    def test_endpoints_answer(self, client: TestClient, path: str) -> None:
        response = client.get(path, params={"window_days": 1825})
        assert response.status_code == 200, response.text

    def test_market_overview_shape(self, client: TestClient) -> None:
        body = client.get("/analytics/market", params={"window_days": 1825}).json()
        assert body["active_jobs"] > 0
        assert 0 <= body["remote_share"] <= 1
        assert body["volume"]["points"]

    def test_salary_dimension_is_validated(self, client: TestClient) -> None:
        assert client.get("/analytics/salaries/by/segment").status_code == 200
        response = client.get("/analytics/salaries/by/nonsense")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"

    def test_window_bounds_are_enforced(self, client: TestClient) -> None:
        assert client.get("/analytics/market", params={"window_days": 99999}).status_code == 422


class TestSkillsApi:
    def test_taxonomy_is_served(self, client: TestClient) -> None:
        body = client.get("/skills/taxonomy").json()
        assert body["groups"]

    def test_trend_endpoint(self, client: TestClient) -> None:
        body = client.get("/skills/python/trend").json()
        assert body["slug"] == "python"
        assert body["windows"]

    def test_explorer_reports_missing_skills(self, client: TestClient) -> None:
        assert client.get("/skills/not-a-real-skill/explorer").status_code == 404

    def test_slug_pattern_is_enforced(self, client: TestClient) -> None:
        assert client.get("/skills/PYTHON!!/trend").status_code == 422


class TestAlertsAndProfiles:
    def test_alert_rule_crud_requires_write_scope(self, client: TestClient, admin_key: str) -> None:
        payload = {
            "name": "python demand",
            "metric": "skill_demand_change_pct",
            "subject": "python",
            "operator": "gt",
            "threshold": 10,
            "window_days": 30,
            "channels": ["in_app"],
        }
        anonymous = client.post("/alerts/rules", json=payload)
        assert anonymous.status_code == 401

        created = client.post("/alerts/rules", json=payload, headers={"X-API-Key": admin_key})
        assert created.status_code == 201
        rule_id = created.json()["id"]

        listed = client.get("/alerts/rules", headers={"X-API-Key": admin_key}).json()
        assert any(rule["id"] == rule_id for rule in listed)

        deleted = client.delete(f"/alerts/rules/{rule_id}", headers={"X-API-Key": admin_key})
        assert deleted.status_code == 204

    def test_invalid_alert_rule_is_rejected(self, client: TestClient, admin_key: str) -> None:
        response = client.post(
            "/alerts/rules",
            json={"name": "bad", "metric": "skill_demand_change_pct", "channels": ["email"]},
            headers={"X-API-Key": admin_key},
        )
        assert response.status_code == 422

    def test_market_fit_for_an_adhoc_profile(self, client: TestClient, admin_key: str) -> None:
        response = client.post(
            "/profiles/market-fit",
            json={
                "target_role": "Python Developer",
                "skills": ["python", "docker"],
                "experience_years": 5,
            },
            params={"window_days": 365},
            headers={"X-API-Key": admin_key},
        )
        assert response.status_code == 200
        body = response.json()
        assert 0 <= body["market_fit_score"] <= 1
        assert body["notes"]


class TestAdminApi:
    def test_admin_requires_authentication(self, client: TestClient) -> None:
        assert client.get("/admin/stats").status_code == 401

    def test_admin_key_unlocks_statistics(self, client: TestClient, admin_key: str) -> None:
        response = client.get("/admin/stats", headers={"X-API-Key": admin_key})
        assert response.status_code == 200
        assert "ingestion" in response.json()

    def test_key_issue_and_revoke(self, client: TestClient, admin_key: str) -> None:
        created = client.post(
            "/admin/keys",
            json={"name": "temp", "role": "viewer"},
            headers={"X-API-Key": admin_key},
        )
        assert created.status_code == 201
        body = created.json()
        assert body["api_key"].startswith("jmi_")

        revoked = client.delete(f"/admin/keys/{body['key_id']}", headers={"X-API-Key": admin_key})
        assert revoked.status_code == 200

        # The revoked key no longer authenticates.
        assert client.get("/admin/stats", headers={"X-API-Key": body["api_key"]}).status_code in (
            401,
            403,
        )

    def test_viewer_cannot_reach_admin_endpoints(self, client: TestClient, admin_key: str) -> None:
        created = client.post(
            "/admin/keys",
            json={"name": "viewer", "role": "viewer"},
            headers={"X-API-Key": admin_key},
        ).json()
        response = client.get("/admin/stats", headers={"X-API-Key": created["api_key"]})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "permission_denied"

    def test_ingestion_can_be_triggered(self, client: TestClient, admin_key: str) -> None:
        response = client.post(
            "/admin/ingest/synthetic",
            params={"limit": 20},
            headers={"X-API-Key": admin_key},
        )
        assert response.status_code == 200
        assert response.json()["records_received"] > 0


class TestOpenApi:
    def test_schema_is_generated(self, client: TestClient) -> None:
        schema = client.get("/openapi.json").json()
        assert schema["info"]["title"]
        assert "/jobs/search" in schema["paths"]
        assert "/analytics/market" in schema["paths"]


class TestRateLimiting:
    def test_requests_over_the_budget_are_rejected(
        self, populated: Database, settings: Settings
    ) -> None:
        limited = settings.model_copy(
            update={
                "api": ApiSettings(
                    rate_limit_enabled=True,
                    rate_limit_requests=3,
                    rate_limit_window_seconds=60,
                    serve_dashboard=False,
                )
            }
        )
        app = create_app(limited, database=populated)
        with TestClient(app) as client:
            statuses = [client.get("/jobs", params={"limit": 1}).status_code for _ in range(5)]
        assert statuses.count(429) >= 1
        assert statuses[0] == 200
