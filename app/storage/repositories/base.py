"""Shared repository plumbing."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy.engine import CursorResult, Result
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import BoundLogger, get_logger


def affected_rows(result: Result[Any]) -> int:
    """Number of rows an UPDATE or DELETE matched.

    ``AsyncSession.execute`` is typed to return ``Result``, but for DML the
    object is a ``CursorResult``, which is the class that carries ``rowcount``.
    SQLAlchemy 2.1 no longer types ``rowcount`` on ``Result``, so it is read
    through the concrete class here rather than at every call site.
    """
    return int(cast("CursorResult[Any]", result).rowcount or 0)


class BaseRepository:
    """A repository bound to a single :class:`AsyncSession`.

    Repositories never open or commit transactions themselves - the service
    layer owns the unit of work. That keeps multi-repository operations atomic.
    """

    __slots__ = ("log", "session")

    def __init__(self, session: AsyncSession, log: BoundLogger | None = None) -> None:
        self.session = session
        self.log = log or get_logger(type(self).__module__)

    @property
    def dialect(self) -> str:
        """Name of the active SQL dialect (``sqlite`` / ``postgresql``)."""
        bind = self.session.get_bind()
        return bind.dialect.name

    @property
    def supports_full_text_search(self) -> bool:
        """Whether the backing database offers native full-text search."""
        return self.dialect == "postgresql"

    async def flush(self) -> None:
        """Push pending changes so that generated keys become available."""
        await self.session.flush()

    @staticmethod
    def _chunked(items: list[Any], size: int) -> list[list[Any]]:
        """Split a list into fixed-size chunks (keeps SQL parameter counts sane)."""
        if size <= 0:
            raise ValueError("chunk size must be positive")
        return [items[i : i + size] for i in range(0, len(items), size)]


#: Databases limit the number of bound parameters per statement; batching keeps
#: us comfortably below the SQLite ceiling of 999.
MAX_BIND_PARAMS = 400

__all__ = ["MAX_BIND_PARAMS", "BaseRepository", "affected_rows"]
