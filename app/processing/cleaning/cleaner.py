"""Cleaning of raw records.

Cleaning happens before validation so that a posting is not rejected for
cosmetic reasons (HTML markup, mojibake, stray whitespace). Nothing is deleted
here - values are only repaired or, when they carry no information, replaced
with ``None`` so the validator can make an explicit decision about them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.text import clean_text, collapse_whitespace, normalize_unicode, strip_html
from app.models.raw import RawJob

#: Mojibake produced when UTF-8 bytes are decoded as CP-1252. Cheap to repair,
#: and it noticeably improves skill extraction on European job boards.
MOJIBAKE_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("â€™", "'"),
    ("â€\u02dc", "'"),
    ("â€œ", '"'),
    ("â€\x9d", '"'),
    ("â€“", "-"),
    ("â€”", "-"),
    ("â€¦", "..."),
    ("Â ", " "),
    ("Ã©", "e"),
    ("Ã¨", "e"),
    ("Ã¼", "u"),
    ("Ã¶", "o"),
    ("Ã¤", "a"),
    ("Ã±", "n"),
    ("ï»¿", ""),
)

#: Placeholder values that mean "no data" even though the field is populated.
NULL_TOKENS = frozenset(
    {
        "",
        "-",
        "--",
        "n/a",
        "na",
        "none",
        "null",
        "nil",
        "unknown",
        "not specified",
        "not disclosed",
        "tbd",
        "undefined",
        "no description",
        "string",
    }
)

_BULLET_RE = re.compile(
    r"^[\s]*[\u2022\u25aa\u25e6\u2023\u00b7*\-\u2013\u2014]+[\s]*", re.MULTILINE
)
_REPEATED_PUNCT_RE = re.compile(r"([!?.,;:])\1{2,}")
_LONG_RUN_RE = re.compile(r"(.)\1{12,}")


@dataclass(slots=True)
class CleaningReport:
    """What the cleaner changed, for data-quality accounting."""

    fields_cleaned: list[str] = field(default_factory=list)
    fields_emptied: list[str] = field(default_factory=list)
    html_removed: bool = False
    mojibake_fixed: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.fields_cleaned or self.fields_emptied)


def repair_mojibake(value: str) -> tuple[str, bool]:
    """Undo the most common UTF-8/CP-1252 double-encoding artefacts."""
    if "Ã" not in value and "â€" not in value and "ï»¿" not in value:
        return value, False
    repaired = value
    for broken, fixed in MOJIBAKE_REPLACEMENTS:
        repaired = repaired.replace(broken, fixed)
    return repaired, repaired != value


def is_null_token(value: str | None) -> bool:
    """Whether a value is a placeholder that carries no information."""
    if value is None:
        return True
    return value.strip().lower() in NULL_TOKENS


def tidy_description(value: str) -> str:
    """Normalise a long free-text body without destroying its structure."""
    text = strip_html(value)
    text, _ = repair_mojibake(text)
    text = normalize_unicode(text)
    text = _BULLET_RE.sub("- ", text)
    text = _REPEATED_PUNCT_RE.sub(r"\1", text)
    text = _LONG_RUN_RE.sub(r"\1\1\1", text)
    return collapse_whitespace(text, keep_newlines=True)


class RecordCleaner:
    """Repairs the text fields of a raw posting."""

    def __init__(self, *, max_description_chars: int = 60_000) -> None:
        self._max_description = max_description_chars

    def clean(self, job: RawJob) -> tuple[RawJob, CleaningReport]:
        """Return a cleaned copy of ``job`` plus a report of what changed."""
        report = CleaningReport()
        updates: dict[str, object] = {}

        for field_name, limit in (
            ("title", 512),
            ("company", 256),
            ("location", 256),
            ("salary", 256),
            ("employment_type", 64),
            ("remote_status", 64),
            ("industry", 128),
        ):
            original = getattr(job, field_name)
            if original is None:
                continue
            if "<" in original or "&" in original:
                report.html_removed = True
            repaired, fixed = repair_mojibake(original)
            report.mojibake_fixed = report.mojibake_fixed or fixed
            cleaned = clean_text(repaired, max_length=limit)
            if is_null_token(cleaned):
                updates[field_name] = None
                report.fields_emptied.append(field_name)
            elif cleaned != original:
                updates[field_name] = cleaned
                report.fields_cleaned.append(field_name)

        if job.description is not None:
            if "<" in job.description:
                report.html_removed = True
            repaired, fixed = repair_mojibake(job.description)
            report.mojibake_fixed = report.mojibake_fixed or fixed
            body = tidy_description(repaired)[: self._max_description]
            if is_null_token(body):
                updates["description"] = None
                report.fields_emptied.append("description")
            elif body != job.description:
                updates["description"] = body
                report.fields_cleaned.append("description")

        if job.url is not None:
            url = job.url.strip()
            if url != job.url:
                updates["url"] = url
                report.fields_cleaned.append("url")

        if not updates:
            return job, report
        return job.model_copy(update=updates), report


__all__ = [
    "MOJIBAKE_REPLACEMENTS",
    "NULL_TOKENS",
    "CleaningReport",
    "RecordCleaner",
    "is_null_token",
    "repair_mojibake",
    "tidy_description",
]
