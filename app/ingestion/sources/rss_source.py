"""Adapter for RSS 2.0 and Atom job feeds.

XML parsing is a security-sensitive operation. This adapter refuses documents
that declare a DTD or entities, which removes the entity-expansion ("billion
laughs") and external-entity (XXE) attack surface, and it relies on the HTTP
client's byte budget to bound the input size.
"""

from __future__ import annotations

import re
from typing import Any
from xml.etree import ElementTree

from app.core.errors import SourceUnavailableError
from app.core.text import clean_text
from app.core.timeutils import parse_datetime
from app.ingestion.base import BaseJobSource, SourceContext
from app.ingestion.http import SafeHttpClient
from app.models.enums import SourceKind
from app.models.raw import RawBatch, RawJob

_DOCTYPE_RE = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)

ATOM_NS = "{http://www.w3.org/2005/Atom}"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"
DC_NS = "{http://purl.org/dc/elements/1.1/}"


def parse_feed_safely(document: str) -> ElementTree.Element:
    """Parse an XML feed, rejecting anything that declares a DTD or entities."""
    if _DOCTYPE_RE.search(document):
        raise SourceUnavailableError(
            "feed declares a DTD or entity; refusing to parse",
            details={"reason": "xxe_protection"},
        )
    try:
        # Safe here: the document was checked for DTD/entity declarations
        # above and its size is bounded by the HTTP client's byte budget.
        parser = ElementTree.XMLParser()  # noqa: S314  # nosec B314
        return ElementTree.fromstring(document, parser=parser)  # noqa: S314  # nosec B314
    except ElementTree.ParseError as exc:
        raise SourceUnavailableError(f"malformed XML feed: {exc}") from exc


class RssJobSource(BaseJobSource):
    """Reads postings from an RSS/Atom feed."""

    kind = SourceKind.RSS

    def __init__(
        self,
        *,
        name: str,
        options: dict[str, Any] | None = None,
        client: SafeHttpClient,
    ) -> None:
        self._client = client
        super().__init__(name=name, options=options)

    def validate_options(self) -> None:
        self.require_option("url")

    async def fetch_jobs(self, context: SourceContext) -> RawBatch:
        url = str(self.require_option("url"))
        headers: dict[str, str] = {"Accept": "application/rss+xml, application/atom+xml, text/xml"}
        if context.etag:
            headers["If-None-Match"] = context.etag

        response = await self._client.get(url, headers=headers)
        root = parse_feed_safely(response.text)
        entries = self._entries(root)

        jobs: list[RawJob] = []
        warnings: list[str] = []
        for entry in entries[: context.limit]:
            job = self._to_raw_job(entry)
            if job is None:
                warnings.append("entry skipped: missing link and guid")
                continue
            jobs.append(job)

        return self.build_batch(jobs, cursor=response.etag, warnings=warnings[:20])

    @staticmethod
    def _entries(root: ElementTree.Element) -> list[ElementTree.Element]:
        """Return feed entries for either RSS or Atom."""
        items = root.findall(".//item")
        if items:
            return items
        return root.findall(f".//{ATOM_NS}entry")

    def _to_raw_job(self, entry: ElementTree.Element) -> RawJob | None:
        link = _text(entry, "link") or _atom_link(entry)
        guid = _text(entry, "guid") or _text(entry, f"{ATOM_NS}id") or link
        if not guid:
            return None

        title = _text(entry, "title") or _text(entry, f"{ATOM_NS}title")
        description = (
            _text(entry, f"{CONTENT_NS}encoded")
            or _text(entry, "description")
            or _text(entry, f"{ATOM_NS}summary")
            or _text(entry, f"{ATOM_NS}content")
        )
        published = (
            _text(entry, "pubDate")
            or _text(entry, f"{DC_NS}date")
            or _text(entry, f"{ATOM_NS}published")
            or _text(entry, f"{ATOM_NS}updated")
        )
        company = (
            _text(entry, f"{DC_NS}creator") or _text(entry, "author") or self.options.get("company")
        )
        category = _text(entry, "category") or _text(entry, f"{ATOM_NS}category")

        return RawJob(
            source=self.name,
            source_kind=self.kind,
            source_job_id=guid[:256],
            url=link,
            title=clean_text(title, max_length=512) or None,
            description=clean_text(description, keep_newlines=True, max_length=200_000) or None,
            company=clean_text(str(company) if company else None, max_length=256) or None,
            location=clean_text(_text(entry, "location"), max_length=256) or None,
            industry=clean_text(category, max_length=128) or None,
            published_at=parse_datetime(published),
            raw_payload={"xml": _to_dict(entry)},
            metadata={"adapter": "rss"},
        )


def _text(element: ElementTree.Element, tag: str) -> str | None:
    """Read the text of a child element."""
    child = element.find(tag)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def _atom_link(entry: ElementTree.Element) -> str | None:
    """Extract the alternate link of an Atom entry."""
    for link in entry.findall(f"{ATOM_NS}link"):
        rel = link.get("rel", "alternate")
        if rel == "alternate" and link.get("href"):
            return link.get("href")
    return None


def _to_dict(element: ElementTree.Element) -> dict[str, Any]:
    """Flatten an XML element into a JSON-serialisable dictionary."""
    payload: dict[str, Any] = {}
    for child in element:
        tag = child.tag.split("}")[-1]
        value = (child.text or "").strip()
        if tag in payload:
            existing = payload[tag]
            payload[tag] = [*existing, value] if isinstance(existing, list) else [existing, value]
        else:
            payload[tag] = value
    return payload


__all__ = ["RssJobSource", "parse_feed_safely"]
