"""Adapter for public JSON job APIs.

Only sources that explicitly permit automated access should be configured
here. The adapter never attempts to authenticate on a user's behalf, never
solves challenges and never ignores an error status - if a source says no, the
run fails loudly instead of working around it.
"""

from __future__ import annotations

import os
from typing import Any

from app.core.errors import SourceConfigurationError
from app.core.timeutils import parse_datetime
from app.ingestion.base import BaseJobSource, SourceContext
from app.ingestion.http import SafeHttpClient
from app.ingestion.mapping import RecordMapper, extract_path, stringify
from app.models.enums import SourceKind
from app.models.raw import RawBatch, RawJob

PAGINATION_MODES = frozenset({"none", "page", "offset", "cursor"})


class ApiJobSource(BaseJobSource):
    """Fetches postings from a paginated JSON endpoint."""

    kind = SourceKind.API

    def __init__(
        self,
        *,
        name: str,
        options: dict[str, Any] | None = None,
        client: SafeHttpClient,
    ) -> None:
        self._client = client
        super().__init__(name=name, options=options)
        self._mapper = RecordMapper(self.options.get("mapping"))

    def validate_options(self) -> None:
        self.require_option("url")
        mode = self._pagination.get("mode", "none")
        if mode not in PAGINATION_MODES:
            raise SourceConfigurationError(
                f"unsupported pagination mode {mode!r}",
                details={"supported": sorted(PAGINATION_MODES)},
            )

    @property
    def _pagination(self) -> dict[str, Any]:
        pagination = self.options.get("pagination") or {}
        if not isinstance(pagination, dict):
            raise SourceConfigurationError("pagination must be a mapping")
        return pagination

    def _auth_headers(self) -> dict[str, str]:
        """Read the credential from the environment - never from the config file."""
        env_var = self.options.get("api_key_env")
        if not env_var:
            return {}
        token = os.environ.get(str(env_var))
        if not token:
            raise SourceConfigurationError(
                f"source {self.name!r} expects the {env_var} environment variable",
                details={"source": self.name},
            )
        header = str(self.options.get("auth_header", "Authorization"))
        scheme = str(self.options.get("auth_scheme", "Bearer")).strip()
        return {header: f"{scheme} {token}".strip()}

    async def fetch_jobs(self, context: SourceContext) -> RawBatch:
        """Fetch one page (or several, up to ``context.limit``) of postings."""
        url = str(self.require_option("url"))
        records_path = self.options.get("records_path")
        pagination = self._pagination
        mode = str(pagination.get("mode", "none"))
        page_size = int(pagination.get("size", 100))

        headers = self._auth_headers()
        if context.etag and self.options.get("use_etag", True):
            headers["If-None-Match"] = context.etag

        collected: list[RawJob] = []
        warnings: list[str] = []
        cursor = context.cursor
        page = int(pagination.get("start_page", 1))
        offset = 0
        has_more = False

        while len(collected) < context.limit:
            params = dict(self.options.get("params") or {})
            if mode == "page":
                params[str(pagination.get("page_param", "page"))] = page
                params[str(pagination.get("size_param", "limit"))] = page_size
            elif mode == "offset":
                params[str(pagination.get("offset_param", "offset"))] = offset
                params[str(pagination.get("size_param", "limit"))] = page_size
            elif mode == "cursor" and cursor:
                params[str(pagination.get("cursor_param", "cursor"))] = cursor

            payload = await self._client.get_json(url, headers=headers, params=params)
            records = self._records(payload, records_path)
            if not records:
                break

            for record in records:
                job = self._to_raw_job(record)
                if job is not None:
                    collected.append(job)
                else:
                    warnings.append("record skipped: no usable identifier")
                if len(collected) >= context.limit:
                    break

            if mode == "none":
                break
            if mode == "cursor":
                next_cursor = extract_path(payload, str(pagination.get("cursor_path", "next")))
                cursor = stringify(next_cursor)
                if not cursor:
                    break
            elif len(records) < page_size:
                break
            else:
                page += 1
                offset += page_size
            has_more = True

        return self.build_batch(
            collected,
            cursor=cursor,
            has_more=has_more and len(collected) >= context.limit,
            warnings=warnings[:20],
        )

    @staticmethod
    def _records(payload: Any, records_path: Any) -> list[Any]:
        """Locate the list of records inside the response body."""
        if records_path:
            records = extract_path(payload, str(records_path))
        elif isinstance(payload, list):
            records = payload
        elif isinstance(payload, dict):
            records = next((value for value in payload.values() if isinstance(value, list)), None)
        else:
            records = None
        if records is None:
            return []
        if not isinstance(records, list):
            return []
        return [record for record in records if isinstance(record, (dict, list))]

    def _to_raw_job(self, record: Any) -> RawJob | None:
        """Map one API record onto the canonical raw model."""
        fields = self._mapper.to_fields(record)
        source_job_id = stringify(fields.get("source_job_id"))
        url = stringify(fields.get("url"))
        if not source_job_id:
            # Fall back to the URL so that records without an explicit id are
            # still tracked idempotently.
            source_job_id = url
        if not source_job_id:
            return None

        return RawJob(
            source=self.name,
            source_kind=self.kind,
            source_job_id=source_job_id[:256],
            url=url,
            title=_trim(stringify(fields.get("title")), 512),
            description=_trim(stringify(fields.get("description")), 200_000),
            company=_trim(stringify(fields.get("company")), 256),
            location=_trim(stringify(fields.get("location")), 256),
            salary=_trim(stringify(fields.get("salary")), 256),
            employment_type=_trim(stringify(fields.get("employment_type")), 64),
            remote_status=_trim(stringify(fields.get("remote_status")), 64),
            industry=_trim(stringify(fields.get("industry")), 128),
            published_at=parse_datetime(fields.get("published_at")),
            raw_payload=record if isinstance(record, dict) else {"value": record},
            metadata={"adapter": "api"},
        )


def _trim(value: str | None, limit: int) -> str | None:
    """Clamp a mapped string to the schema's maximum length."""
    if value is None:
        return None
    return value[:limit]


__all__ = ["PAGINATION_MODES", "ApiJobSource"]
