"""Declarative base and shared column types.

A explicit naming convention is configured so that Alembic produces stable,
readable constraint names instead of database-generated ones.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, MetaData
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, mapped_column
from sqlalchemy.types import TypeDecorator, TypeEngine

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: JSON on SQLite, JSONB on PostgreSQL (indexable and more compact).
JSONType: TypeEngine[Any] = JSON().with_variant(JSONB(), "postgresql")


class UtcDateTime(TypeDecorator[datetime]):
    """A timestamp column that is timezone-aware UTC on both sides.

    PostgreSQL preserves the offset; SQLite does not, and hands back naive
    datetimes that then explode the first time they are compared with an aware
    one. Normalising in the type keeps that difference out of every caller.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


#: Always store (and return) timezone-aware UTC timestamps.
TimestampType = UtcDateTime()


class Base(DeclarativeBase):
    """Base class for every ORM model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {
        dict[str, Any]: JSONType,
        list[str]: JSONType,
        list[int]: JSONType,
        datetime: TimestampType,
    }

    def to_dict(self) -> dict[str, Any]:
        """Shallow column dictionary (handy in tests and exports)."""
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        pk = self.__mapper__.primary_key
        values = ", ".join(f"{c.name}={getattr(self, c.name)!r}" for c in pk)
        return f"<{type(self).__name__} {values}>"


def json_column(**kwargs: Any) -> Any:
    """A JSON column that defaults to an empty object."""
    kwargs.setdefault("default", dict)
    kwargs.setdefault("nullable", False)
    return mapped_column(JSONType, **kwargs)


__all__ = [
    "NAMING_CONVENTION",
    "Base",
    "JSONType",
    "TimestampType",
    "UtcDateTime",
    "json_column",
]
