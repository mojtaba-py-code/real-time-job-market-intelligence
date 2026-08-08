"""Location normalization.

Postings describe where the work happens in a dozen incompatible ways:
``Berlin, Germany``, ``Remote (EMEA)``, ``US-TX-Austin``, ``London / Hybrid``.
This module turns all of them into a :class:`LocationInfo` plus a remote-work
hint, and always reports how confident it is.
"""

from __future__ import annotations

import re

from app.core.text import canonical_key, clean_text, contains_word
from app.models.enums import RemoteType
from app.models.job import LocationInfo
from app.processing.normalization.geography import (
    HYBRID_TOKENS,
    MACRO_REGIONS,
    ONSITE_TOKENS,
    REMOTE_TOKENS,
    US_STATES,
    country_name,
    lookup_city,
    lookup_country,
)

_SPLIT_RE = re.compile(r"\s*[,/|;•]\s*|\s+-\s+")
_PARENS_RE = re.compile(r"[(\[]([^)\]]*)[)\]]")
_US_PATTERN = re.compile(r"^(?P<country>[A-Z]{2})-(?P<state>[A-Z]{2})-(?P<city>.+)$")


class LocationNormalizer:
    """Parses a free-form location string into structured geography."""

    def normalize(
        self, raw: str | None, *, remote_hint: str | None = None, description: str | None = None
    ) -> LocationInfo:
        """Resolve a location string, optionally helped by other fields."""
        text = clean_text(raw, max_length=256)
        remote = self._detect_remote(text, remote_hint, description)

        if not text:
            return LocationInfo(raw=raw, remote_hint=remote, confidence=0.0)

        structured = self._parse_structured(text)
        if structured is not None:
            return structured.model_copy(update={"raw": raw, "remote_hint": remote})

        tokens = self._tokens(text)
        city: str | None = None
        region: str | None = None
        country_code: str | None = None
        matched = 0

        for token in tokens:
            key = canonical_key(token)
            if not key or key in MACRO_REGIONS or key in REMOTE_TOKENS:
                continue

            code = lookup_country(token)
            if code and country_code is None:
                country_code = code
                matched += 1
                continue

            hit = lookup_city(token)
            if hit and city is None:
                city, region = token.strip(), hit[1]
                if country_code is None:
                    country_code = hit[0]
                matched += 1
                continue

            upper = token.strip().upper()
            if len(upper) == 2 and upper in US_STATES and region is None:
                region = US_STATES[upper]
                country_code = country_code or "US"
                matched += 1
                continue

            if city is None and len(token.strip()) > 2:
                # Unrecognised but plausible city name; keep it verbatim.
                city = token.strip()[:96]

        confidence = self._confidence(city, region, country_code, matched, len(tokens))
        return LocationInfo(
            raw=raw,
            city=city[:96] if city else None,
            region=region[:96] if region else None,
            country_code=country_code,
            country=country_name(country_code),
            remote_hint=remote,
            confidence=confidence,
        )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    @staticmethod
    def _tokens(text: str) -> list[str]:
        """Split a location string into candidate parts."""
        without_parens = _PARENS_RE.sub(" ", text)
        parts = [part.strip() for part in _SPLIT_RE.split(without_parens) if part.strip()]
        # Parenthesised content often holds the country: "Remote (Germany)".
        parts.extend(match.strip() for match in _PARENS_RE.findall(text) if match.strip())
        return parts

    @staticmethod
    def _parse_structured(text: str) -> LocationInfo | None:
        """Handle the ``US-TX-Austin`` shape used by several ATS providers."""
        match = _US_PATTERN.match(text.strip())
        if match is None:
            return None
        country_code = match.group("country").upper()
        if lookup_country(country_code) is None:
            return None
        state = match.group("state").upper()
        return LocationInfo(
            city=match.group("city").strip()[:96],
            region=US_STATES.get(state, state),
            country_code=country_code,
            country=country_name(country_code),
            confidence=0.9,
        )

    @staticmethod
    def _detect_remote(text: str, remote_hint: str | None, description: str | None) -> RemoteType:
        """Infer the work arrangement from the location, the hint and the body."""
        hint_key = canonical_key(remote_hint or "")
        if hint_key:
            if hint_key in {"true", "yes", "1"} or hint_key in REMOTE_TOKENS:
                return RemoteType.REMOTE
            if hint_key in HYBRID_TOKENS:
                return RemoteType.HYBRID
            if hint_key in ONSITE_TOKENS or hint_key in {"false", "no", "0"}:
                return RemoteType.ONSITE

        haystack = canonical_key(text)
        if any(token in haystack for token in HYBRID_TOKENS):
            return RemoteType.HYBRID
        if any(token in haystack for token in REMOTE_TOKENS):
            return RemoteType.REMOTE
        if any(token in haystack for token in ONSITE_TOKENS):
            return RemoteType.ONSITE

        if description:
            body = description[:4000]
            if contains_word(body, "hybrid"):
                return RemoteType.HYBRID
            if contains_word(body, "fully remote") or contains_word(body, "remote"):
                return RemoteType.REMOTE
            if contains_word(body, "on-site") or contains_word(body, "onsite"):
                return RemoteType.ONSITE
        return RemoteType.UNKNOWN

    @staticmethod
    def _confidence(
        city: str | None,
        region: str | None,
        country_code: str | None,
        matched: int,
        token_count: int,
    ) -> float:
        """Score how much of the string we actually understood."""
        score = 0.0
        if country_code:
            score += 0.5
        if region:
            score += 0.15
        if city:
            score += 0.25 if lookup_city(city) else 0.1
        if token_count:
            score += 0.1 * min(1.0, matched / token_count)
        return round(min(score, 1.0), 3)


__all__ = ["LocationNormalizer"]
