"""Async engine, session factory and database lifecycle management.

The same code runs on SQLite (local development, tests) and PostgreSQL
(production). Dialect-specific tuning - foreign-key enforcement and WAL for
SQLite, statement timeouts for PostgreSQL - is applied here so that no other
module has to care which database it is talking to.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool, StaticPool

from app.core.config import DatabaseSettings, Settings, get_settings
from app.core.errors import StorageError
from app.core.logging import get_logger
from app.storage.base import Base

log = get_logger(__name__)


def _apply_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    """Enable foreign keys and a sane journal mode on every SQLite connection."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def build_engine(settings: DatabaseSettings) -> AsyncEngine:
    """Create an :class:`AsyncEngine` configured for the target dialect."""
    kwargs: dict[str, Any] = {"echo": settings.echo, "future": True}

    if settings.is_sqlite:
        database_path = settings.url.split("///", 1)[-1]
        if ":memory:" in settings.url or not database_path:
            # An in-memory database lives inside its connection, so every
            # session must share the same one.
            kwargs["poolclass"] = StaticPool
            kwargs["connect_args"] = {"check_same_thread": False}
        else:
            # SQLite has no server-side pooling story worth tuning; NullPool
            # keeps file locking predictable, especially on Windows.
            kwargs["poolclass"] = NullPool
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    else:
        kwargs.update(
            pool_size=settings.pool_size,
            max_overflow=settings.max_overflow,
            pool_recycle=settings.pool_recycle_seconds,
            pool_pre_ping=settings.pool_pre_ping,
        )
        if settings.is_postgres and settings.statement_timeout_ms:
            kwargs["connect_args"] = {
                "server_settings": {"statement_timeout": str(settings.statement_timeout_ms)}
            }

    engine = create_async_engine(settings.url, **kwargs)
    if settings.is_sqlite:
        event.listens_for(engine.sync_engine, "connect")(_apply_sqlite_pragmas)
    return engine


class Database:
    """Owns the engine and hands out sessions."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None

    @property
    def engine(self) -> AsyncEngine:
        """The lazily created engine."""
        if self._engine is None:
            self._engine = build_engine(self._settings.database)
            log.debug("database.engine_created", dialect=self._engine.dialect.name)
        return self._engine

    @property
    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        """The lazily created session factory."""
        if self._sessionmaker is None:
            self._sessionmaker = async_sessionmaker(
                bind=self.engine,
                class_=AsyncSession,
                expire_on_commit=False,
                autoflush=False,
            )
        return self._sessionmaker

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session, committing on success and rolling back on error."""
        async with self.sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except SQLAlchemyError as exc:
                await session.rollback()
                raise StorageError(f"database operation failed: {exc}") from exc
            except Exception:
                await session.rollback()
                raise

    @asynccontextmanager
    async def read_session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session for read-only work (never commits)."""
        async with self.sessionmaker() as session:
            try:
                yield session
            finally:
                await session.rollback()

    @staticmethod
    def _load_metadata() -> None:
        """Import the ORM module so every table is registered on the metadata.

        ``Base.metadata`` is populated as a side effect of importing the model
        definitions. Without this, a caller that only imported ``session`` would
        create an empty schema and fail later with "no such table".
        """
        import app.storage.models  # noqa: F401

    async def create_all(self) -> None:
        """Create the schema directly (tests and first-run bootstrap)."""
        self._load_metadata()
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def drop_all(self) -> None:
        """Drop every table. Guarded so it can never run against production."""
        if self._settings.environment.is_production_like:
            raise StorageError("refusing to drop tables in a production-like environment")
        self._load_metadata()
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)

    async def healthcheck(self) -> bool:
        """Return whether the database answers a trivial query."""
        try:
            async with self.read_session() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            log.warning("database.healthcheck_failed", error=str(exc))
            return False

    async def dispose(self) -> None:
        """Close all pooled connections."""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessionmaker = None


_database: Database | None = None


def get_database(settings: Settings | None = None) -> Database:
    """Return the process-wide :class:`Database` instance."""
    global _database
    if _database is None:
        _database = Database(settings)
    return _database


def set_database(database: Database | None) -> None:
    """Replace the global database (used by tests and the app factory)."""
    global _database
    _database = database


async def dispose_database() -> None:
    """Dispose and forget the global database."""
    global _database
    if _database is not None:
        await _database.dispose()
        _database = None


def is_sqlite(engine: AsyncEngine | Engine) -> bool:
    """Whether an engine talks to SQLite."""
    return engine.dialect.name == "sqlite"


__all__ = [
    "Database",
    "build_engine",
    "dispose_database",
    "get_database",
    "is_sqlite",
    "set_database",
]
