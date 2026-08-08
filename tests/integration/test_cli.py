"""Integration tests for the command-line interface and the worker wiring."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from app.cli.main import app as cli_app
from app.core.config import get_settings, reset_settings_cache

pytestmark = pytest.mark.integration

runner = CliRunner()


@pytest.fixture
def cli_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the CLI at a throwaway SQLite database and data directory."""
    database = tmp_path / "cli.db"
    monkeypatch.setenv("JOBINTEL_ENVIRONMENT", "testing")
    monkeypatch.setenv("JOBINTEL_DATABASE__URL", f"sqlite+aiosqlite:///{database.as_posix()}")
    monkeypatch.setenv("JOBINTEL_STORAGE__DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JOBINTEL_STORAGE__RAW_DIR", str(tmp_path / "raw"))
    monkeypatch.setenv("JOBINTEL_STORAGE__PROCESSED_DIR", str(tmp_path / "processed"))
    monkeypatch.setenv("JOBINTEL_STORAGE__ANALYTICS_DIR", str(tmp_path / "analytics"))
    monkeypatch.setenv("JOBINTEL_OBSERVABILITY__LOG_LEVEL", "ERROR")
    reset_settings_cache()

    from app.storage.session import set_database

    set_database(None)
    try:
        yield tmp_path
    finally:
        set_database(None)
        reset_settings_cache()


def invoke(*args: str) -> object:
    result = runner.invoke(cli_app, list(args))
    assert result.exit_code == 0, result.output + str(result.exception)
    return result


class TestCliBasics:
    def test_help_lists_every_command(self) -> None:
        output = runner.invoke(cli_app, ["--help"]).output
        for command in ("ingest", "process", "analyze", "trends", "quality", "report"):
            assert command in output

    def test_version(self, cli_environment: Path) -> None:
        assert "jobintel" in invoke("version").output  # type: ignore[attr-defined]

    def test_scopes_are_documented(self) -> None:
        output = runner.invoke(cli_app, ["scopes"]).output
        assert "analytics" in output

    def test_taxonomy_summary(self, cli_environment: Path) -> None:
        assert "taxonomy" in invoke("taxonomy").output.lower()  # type: ignore[attr-defined]


class TestCliPipeline:
    def test_full_flow(self, cli_environment: Path) -> None:
        invoke("db", "init")
        invoke("taxonomy", "--sync")

        ingest = invoke("ingest", "--source", "synthetic", "--limit", "120", "--json")
        payload = json.loads(ingest.output)  # type: ignore[attr-defined]
        assert payload[0]["created"] > 50

        report = invoke("report", "--window-days", "1825", "--json")
        assert json.loads(report.output)["overview"]["active_jobs"] > 50  # type: ignore[attr-defined]

        trends = invoke("trends", "--days", "90", "--json")
        assert isinstance(json.loads(trends.output), list)  # type: ignore[attr-defined]

        quality = invoke("quality", "--json")
        assert "ingestion" in json.loads(quality.output)  # type: ignore[attr-defined]

        search = invoke("search", "python", "--json")
        assert "items" in json.loads(search.output)  # type: ignore[attr-defined]

        fit = invoke("fit", "--role", "Python Developer", "--skills", "python,docker", "--json")
        assert 0 <= json.loads(fit.output)["market_fit_score"] <= 1  # type: ignore[attr-defined]

        dedup = invoke("deduplicate", "--json")
        assert json.loads(dedup.output)["scanned"] >= 0  # type: ignore[attr-defined]

        process = invoke("process", "--json")
        assert "rows_exported" in json.loads(process.output)  # type: ignore[attr-defined]

    def test_analyze_reports_missing_skills_cleanly(self, cli_environment: Path) -> None:
        invoke("db", "init")
        result = runner.invoke(cli_app, ["analyze", "--skill", "nonexistent"])
        assert result.exit_code == 1
        assert "not_found" in result.output

    def test_generate_writes_a_dataset(self, cli_environment: Path) -> None:
        destination = cli_environment / "raw" / "generated.jsonl"
        invoke("generate", "--count", "50", "--output", str(destination))
        lines = destination.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 50
        assert json.loads(lines[0])["source"] == "synthetic"


class TestCliCredentials:
    def test_key_lifecycle(self, cli_environment: Path) -> None:
        invoke("db", "init")
        created = invoke("keys", "create", "--name", "cli-test", "--role", "analyst")
        assert "jmi_" in created.output  # type: ignore[attr-defined]

        listed = invoke("keys", "list")
        assert "cli-test" in listed.output  # type: ignore[attr-defined]

        key_id = next(
            line.split(":")[1].strip()
            for line in created.output.splitlines()  # type: ignore[attr-defined]
            if "key id" in line
        )
        assert "revoked" in invoke("keys", "revoke", key_id).output  # type: ignore[attr-defined]

    def test_revoking_an_unknown_key_fails(self, cli_environment: Path) -> None:
        invoke("db", "init")
        result = runner.invoke(cli_app, ["keys", "revoke", "deadbeefcafe"])
        assert result.exit_code == 1


class TestCliSources:
    def test_list_and_toggle(self, cli_environment: Path) -> None:
        invoke("db", "init")
        invoke("ingest", "--source", "synthetic", "--limit", "20", "--json")

        listed = invoke("sources", "list")
        assert "synthetic" in listed.output  # type: ignore[attr-defined]

        assert "disabled" in invoke("sources", "disable", "synthetic").output  # type: ignore[attr-defined]
        assert "enabled" in invoke("sources", "enable", "synthetic").output  # type: ignore[attr-defined]

    def test_unknown_source_fails(self, cli_environment: Path) -> None:
        invoke("db", "init")
        assert runner.invoke(cli_app, ["sources", "enable", "ghost"]).exit_code == 1


class TestWorkerWiring:
    def test_context_and_scheduler_are_built(self, cli_environment: Path) -> None:
        from app.workers.runner import build_context, build_scheduler

        context = build_context(get_settings())
        scheduler = build_scheduler(context)
        names = {task.name for task in scheduler.tasks}
        assert {"ingest", "expire", "analytics", "alerts"} == names
        assert context.ingestion is not None
        assert context.analytics is not None

    async def test_scheduled_tasks_execute(self, cli_environment: Path) -> None:
        from app.workers.runner import build_context, build_scheduler

        context = build_context(get_settings())
        await context.database.create_all()
        scheduler = build_scheduler(context)

        result = await scheduler.run_once("ingest")
        assert result["sources"] >= 1

        assert await scheduler.run_once("expire") >= 0
        assert "evaluated" in await scheduler.run_once("alerts")

        await context.database.dispose()
