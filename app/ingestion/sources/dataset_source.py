"""Adapter for user-provided datasets (CSV, JSON, JSON Lines, Parquet).

File paths coming from configuration are treated as untrusted input: every
path is resolved and then checked against an allow-listed root, so a
``../../etc/passwd`` style value cannot escape the data directory.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.errors import SourceConfigurationError
from app.core.timeutils import parse_datetime
from app.ingestion.base import BaseJobSource, SourceContext
from app.ingestion.mapping import RecordMapper, stringify
from app.models.enums import SourceKind
from app.models.raw import RawBatch, RawJob

SUPPORTED_SUFFIXES = frozenset({".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".parquet"})
MAX_FILE_BYTES = 512 * 1024 * 1024


def resolve_within(path: str | Path, roots: list[Path]) -> Path:
    """Resolve ``path`` and ensure it stays inside one of ``roots``.

    Raises:
        SourceConfigurationError: on traversal outside the allowed roots.
    """
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = (roots[0] / candidate) if roots else candidate.absolute()
    resolved = candidate.resolve()
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            continue
        return resolved
    raise SourceConfigurationError(
        "dataset path is outside the allowed data directories",
        details={"path": str(resolved), "allowed_roots": [str(r) for r in roots]},
    )


class DatasetSource(BaseJobSource):
    """Reads postings from a local dataset file."""

    kind = SourceKind.DATASET

    def __init__(
        self,
        *,
        name: str,
        options: dict[str, Any] | None = None,
        allowed_roots: list[Path] | None = None,
    ) -> None:
        settings = get_settings()
        self._roots = allowed_roots or [settings.storage.data_dir, settings.storage.raw_dir]
        super().__init__(name=name, options=options)
        self._mapper = RecordMapper(self.options.get("mapping"))

    def validate_options(self) -> None:
        raw_path = self.require_option("path")
        path = resolve_within(str(raw_path), self._roots)
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise SourceConfigurationError(
                f"unsupported dataset format {path.suffix!r}",
                details={"supported": sorted(SUPPORTED_SUFFIXES)},
            )
        self._path = path

    async def fetch_jobs(self, context: SourceContext) -> RawBatch:
        """Read up to ``context.limit`` records, resuming from the stored cursor."""
        if not self._path.exists():
            raise SourceConfigurationError(
                f"dataset {self._path} does not exist", details={"path": str(self._path)}
            )
        if self._path.stat().st_size > MAX_FILE_BYTES:
            raise SourceConfigurationError(
                f"dataset {self._path} exceeds the {MAX_FILE_BYTES} byte limit"
            )

        start = int(context.cursor) if context.cursor and context.cursor.isdigit() else 0
        jobs: list[RawJob] = []
        warnings: list[str] = []
        index = 0

        for record in self._iter_records():
            if index < start:
                index += 1
                continue
            index += 1
            job = self._to_raw_job(record, index)
            if job is None:
                warnings.append(f"row {index}: no usable identifier")
                continue
            jobs.append(job)
            if len(jobs) >= context.limit:
                break

        has_more = len(jobs) >= context.limit
        return self.build_batch(jobs, cursor=str(index), has_more=has_more, warnings=warnings[:20])

    def _iter_records(self) -> Iterator[dict[str, Any]]:
        """Stream records out of the dataset without loading it all at once."""
        suffix = self._path.suffix.lower()
        if suffix in (".csv", ".tsv"):
            delimiter = "\t" if suffix == ".tsv" else ","
            with self._path.open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle, delimiter=delimiter):
                    yield {k: v for k, v in row.items() if k}
        elif suffix in (".jsonl", ".ndjson"):
            with self._path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    payload = json.loads(line)
                    if isinstance(payload, dict):
                        yield payload
        elif suffix == ".json":
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            records = payload if isinstance(payload, list) else payload.get("jobs", [])
            for record in records:
                if isinstance(record, dict):
                    yield record
        elif suffix == ".parquet":
            import pyarrow.parquet as pq

            table = pq.read_table(self._path)
            for batch in table.to_batches(max_chunksize=1000):
                yield from batch.to_pylist()

    def _to_raw_job(self, record: dict[str, Any], index: int) -> RawJob | None:
        fields = self._mapper.to_fields(record)
        source_job_id = stringify(fields.get("source_job_id")) or stringify(fields.get("url"))
        if not source_job_id:
            source_job_id = f"{self._path.stem}-{index}"

        title = stringify(fields.get("title"))
        if not title:
            return None

        return RawJob(
            source=self.name,
            source_kind=self.kind,
            source_job_id=source_job_id[:256],
            url=stringify(fields.get("url")),
            title=title[:512],
            description=(stringify(fields.get("description")) or "")[:200_000] or None,
            company=(stringify(fields.get("company")) or "")[:256] or None,
            location=(stringify(fields.get("location")) or "")[:256] or None,
            salary=(stringify(fields.get("salary")) or "")[:256] or None,
            employment_type=(stringify(fields.get("employment_type")) or "")[:64] or None,
            remote_status=(stringify(fields.get("remote_status")) or "")[:64] or None,
            industry=(stringify(fields.get("industry")) or "")[:128] or None,
            published_at=parse_datetime(fields.get("published_at")),
            raw_payload=_jsonable(record),
            metadata={"adapter": "dataset", "file": self._path.name, "row": index},
        )


def _jsonable(record: dict[str, Any]) -> dict[str, Any]:
    """Coerce a record into something JSON can store."""
    payload: dict[str, Any] = {}
    for key, value in record.items():
        if isinstance(value, (str, int, float, bool, type(None))):
            payload[str(key)] = value
        else:
            payload[str(key)] = str(value)
    return payload


__all__ = ["MAX_FILE_BYTES", "SUPPORTED_SUFFIXES", "DatasetSource", "resolve_within"]
