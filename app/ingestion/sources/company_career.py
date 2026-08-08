"""Adapter for company career APIs.

Most applicant-tracking systems expose a documented, public JSON endpoint per
company (board tokens, career feeds and so on). This adapter is a thin
specialisation of :class:`ApiJobSource` that knows the company up front, which
means postings arrive with a reliable employer even when the payload omits it.
"""

from __future__ import annotations

from typing import Any

from app.ingestion.http import SafeHttpClient
from app.ingestion.sources.api_source import ApiJobSource
from app.models.enums import SourceKind
from app.models.raw import RawJob

#: Shapes of well-known applicant tracking systems, so a source only has to
#: declare ``provider: greenhouse`` plus its board token.
PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "greenhouse": {
        "url_template": "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true",
        "records_path": "jobs",
        "mapping": {
            "source_job_id": "id",
            "title": "title",
            "description": "content",
            "url": "absolute_url",
            "location": "location.name",
            "published_at": "updated_at",
        },
    },
    "lever": {
        "url_template": "https://api.lever.co/v0/postings/{board}?mode=json",
        "records_path": "",
        "mapping": {
            "source_job_id": "id",
            "title": "text",
            "description": "descriptionPlain",
            "url": "hostedUrl",
            "location": "categories.location",
            "employment_type": "categories.commitment",
            "industry": "categories.team",
            "published_at": "createdAt",
        },
    },
    "generic": {},
}


class CompanyCareerSource(ApiJobSource):
    """Fetches postings from one company's public career endpoint."""

    kind = SourceKind.CAREER_PAGE

    def __init__(
        self,
        *,
        name: str,
        options: dict[str, Any] | None = None,
        client: SafeHttpClient,
    ) -> None:
        merged = dict(options or {})
        provider = str(merged.get("provider", "generic")).lower()
        preset = PROVIDER_PRESETS.get(provider, {})

        if "url" not in merged and preset.get("url_template") and merged.get("board"):
            merged["url"] = str(preset["url_template"]).format(board=merged["board"])
        if "records_path" not in merged and "records_path" in preset:
            merged["records_path"] = preset["records_path"]
        if "mapping" not in merged and "mapping" in preset:
            merged["mapping"] = dict(preset["mapping"])

        super().__init__(name=name, options=merged, client=client)
        self._company = str(merged.get("company") or name)
        self._industry = merged.get("industry")

    def _to_raw_job(self, record: Any) -> RawJob | None:
        job = super()._to_raw_job(record)
        if job is None:
            return None
        # The company is known from configuration, so it is authoritative.
        if not job.company:
            job.company = self._company[:256]
        if self._industry and not job.industry:
            job.industry = str(self._industry)[:128]
        job.metadata = {**job.metadata, "adapter": "career_page", "company": self._company}
        return job


__all__ = ["PROVIDER_PRESETS", "CompanyCareerSource"]
