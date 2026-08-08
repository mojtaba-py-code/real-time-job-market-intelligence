"""The skill taxonomy.

Loaded from YAML at startup, validated once, then treated as immutable. The
extractor asks the taxonomy what exists and how it is spelled; it never
contains a technology list of its own, which is what lets the vocabulary grow
without touching code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.core.errors import TaxonomyError
from app.core.text import canonical_key
from app.models.enums import SkillCategory

DEFAULT_TAXONOMY_PATH = Path("configs") / "skill_taxonomy.yaml"


@dataclass(frozen=True, slots=True)
class SkillNode:
    """One detectable skill."""

    slug: str
    name: str
    category: SkillCategory
    parent: str | None = None
    aliases: tuple[str, ...] = ()
    ambiguous: bool = False
    weight: float = 1.0

    @property
    def surface_forms(self) -> tuple[str, ...]:
        """Every spelling that should match this skill."""
        return (self.name, *self.aliases)


@dataclass(frozen=True, slots=True)
class GroupNode:
    """A hierarchy node that groups skills (``Backend``, ``Databases``)."""

    slug: str
    name: str
    parent: str | None = None


@dataclass(slots=True)
class SkillTaxonomy:
    """An immutable, validated view of the configured skill vocabulary."""

    skills: dict[str, SkillNode] = field(default_factory=dict)
    groups: dict[str, GroupNode] = field(default_factory=dict)
    version: int = 1
    source_path: Path | None = None
    _alias_index: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        index: dict[str, str] = {}
        for skill in self.skills.values():
            for form in skill.surface_forms:
                key = canonical_key(form) or form.lower()
                # First declaration wins, so a specific skill cannot be
                # shadowed by a later, broader alias.
                index.setdefault(key, skill.slug)
                index.setdefault(form.lower(), skill.slug)
        self._alias_index = index

    # ------------------------------------------------------------------ #
    # Lookup
    # ------------------------------------------------------------------ #
    def get(self, slug: str) -> SkillNode | None:
        return self.skills.get(slug)

    def resolve(self, text: str) -> SkillNode | None:
        """Find the skill a surface form refers to."""
        if not text:
            return None
        slug = self._alias_index.get(text.lower()) or self._alias_index.get(canonical_key(text))
        return self.skills.get(slug) if slug else None

    def alias_map(self) -> dict[str, str]:
        """Every recognised surface form mapped to its skill slug."""
        return dict(self._alias_index)

    def ancestors(self, slug: str) -> list[str]:
        """Group slugs from the immediate parent up to the root."""
        chain: list[str] = []
        skill = self.skills.get(slug)
        current = (
            skill.parent if skill else self.groups[slug].parent if slug in self.groups else None
        )
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            chain.append(current)
            group = self.groups.get(current)
            current = group.parent if group else None
        return chain

    def children_of(self, group_slug: str) -> list[SkillNode]:
        return [skill for skill in self.skills.values() if skill.parent == group_slug]

    def by_category(self, category: SkillCategory) -> list[SkillNode]:
        return [skill for skill in self.skills.values() if skill.category is category]

    def tree(self) -> dict[str, Any]:
        """Nested representation for the API and the dashboard."""
        roots = [g for g in self.groups.values() if g.parent is None]
        return {
            "version": self.version,
            "groups": [self._group_payload(root) for root in sorted(roots, key=lambda g: g.name)],
        }

    def _group_payload(self, group: GroupNode) -> dict[str, Any]:
        children = [g for g in self.groups.values() if g.parent == group.slug]
        return {
            "slug": group.slug,
            "name": group.name,
            "skills": [
                {"slug": s.slug, "name": s.name, "category": str(s.category)}
                for s in sorted(self.children_of(group.slug), key=lambda s: s.name)
            ],
            "groups": [
                self._group_payload(child) for child in sorted(children, key=lambda g: g.name)
            ],
        }

    def to_rows(self) -> list[dict[str, Any]]:
        """Flat rows for persisting the taxonomy into the database."""
        rows: list[dict[str, Any]] = []
        for group in self.groups.values():
            rows.append(
                {
                    "slug": group.slug,
                    "name": group.name,
                    "category": "other",
                    "parent": group.parent,
                    "aliases": [],
                }
            )
        for skill in self.skills.values():
            rows.append(
                {
                    "slug": skill.slug,
                    "name": skill.name,
                    "category": str(skill.category),
                    "parent": skill.parent,
                    "aliases": list(skill.aliases),
                }
            )
        return rows

    def __len__(self) -> int:
        return len(self.skills)


def _parse_category(value: Any, slug: str) -> SkillCategory:
    try:
        return SkillCategory(str(value or "other").strip().lower())
    except ValueError as exc:
        raise TaxonomyError(
            f"skill {slug!r} has unknown category {value!r}",
            details={"allowed": [c.value for c in SkillCategory]},
        ) from exc


def load_taxonomy(path: Path | str | None = None) -> SkillTaxonomy:
    """Read, validate and build the taxonomy."""
    config_path = Path(path) if path else DEFAULT_TAXONOMY_PATH
    if not config_path.exists():
        raise TaxonomyError(
            f"skill taxonomy not found at {config_path}", details={"path": str(config_path)}
        )

    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise TaxonomyError(f"{config_path} is not valid YAML: {exc}") from exc
    if not isinstance(payload, dict):
        raise TaxonomyError(f"{config_path} must contain a mapping")

    groups: dict[str, GroupNode] = {}
    for entry in payload.get("groups") or []:
        if not isinstance(entry, dict) or not entry.get("slug"):
            raise TaxonomyError("every group needs a slug")
        slug = str(entry["slug"])
        if slug in groups:
            raise TaxonomyError(f"duplicate group slug {slug!r}")
        groups[slug] = GroupNode(
            slug=slug,
            name=str(entry.get("name") or slug),
            parent=str(entry["parent"]) if entry.get("parent") else None,
        )

    skills: dict[str, SkillNode] = {}
    for entry in payload.get("skills") or []:
        if not isinstance(entry, dict) or not entry.get("slug"):
            raise TaxonomyError("every skill needs a slug")
        slug = str(entry["slug"])
        if slug in skills:
            raise TaxonomyError(f"duplicate skill slug {slug!r}")
        aliases = entry.get("aliases") or []
        if not isinstance(aliases, list):
            raise TaxonomyError(f"skill {slug!r}: aliases must be a list")
        parent = str(entry["parent"]) if entry.get("parent") else None
        if parent and parent not in groups:
            raise TaxonomyError(
                f"skill {slug!r} references unknown group {parent!r}",
                details={"known_groups": sorted(groups)},
            )
        skills[slug] = SkillNode(
            slug=slug,
            name=str(entry.get("name") or slug),
            category=_parse_category(entry.get("category"), slug),
            parent=parent,
            aliases=tuple(str(a) for a in aliases if str(a).strip()),
            ambiguous=bool(entry.get("ambiguous", False)),
            weight=float(entry.get("weight", 1.0)),
        )

    if not skills:
        raise TaxonomyError(f"{config_path} does not define any skills")

    _validate_group_hierarchy(groups)

    return SkillTaxonomy(
        skills=skills,
        groups=groups,
        version=int(payload.get("version", 1)),
        source_path=config_path,
    )


def _validate_group_hierarchy(groups: dict[str, GroupNode]) -> None:
    """Reject unknown parents and cycles."""
    for group in groups.values():
        seen: set[str] = {group.slug}
        current = group.parent
        while current is not None:
            if current not in groups:
                raise TaxonomyError(f"group {group.slug!r} references unknown parent {current!r}")
            if current in seen:
                raise TaxonomyError(f"cycle detected in the taxonomy at {current!r}")
            seen.add(current)
            current = groups[current].parent


@lru_cache(maxsize=4)
def get_taxonomy(path: str | None = None) -> SkillTaxonomy:
    """Process-wide cached taxonomy."""
    return load_taxonomy(path)


__all__ = [
    "DEFAULT_TAXONOMY_PATH",
    "GroupNode",
    "SkillNode",
    "SkillTaxonomy",
    "get_taxonomy",
    "load_taxonomy",
]
