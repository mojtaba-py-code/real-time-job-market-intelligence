"""Alembic environment.

The connection URL comes from the application settings, so migrations always
target the same database the application does and no credential is stored in
``alembic.ini``.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import JSON
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import get_settings
from app.storage.base import Base
from app.storage.session import build_engine

# Importing the ORM module registers every table on ``Base.metadata``.
import app.storage.models  # noqa: F401  (side-effect import)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    override = config.get_main_option("sqlalchemy.url", None)
    if override:
        return override
    return get_settings().database.url


def _render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """Render our dialect-aware JSON type readably in generated migrations.

    Without this, autogenerate emits the internal repr of the PostgreSQL JSONB
    variant, which does not round-trip as valid Python.
    """
    if type_ == "type" and isinstance(obj, JSON) and getattr(obj, "_variant_mapping", None):
        autogen_context.imports.add("from sqlalchemy.dialects import postgresql")  # type: ignore[attr-defined]
        return "sa.JSON().with_variant(postgresql.JSONB(), 'postgresql')"
    return False


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to a database."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_item=_render_item,
        compare_type=True,
        compare_server_default=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: object) -> None:
    context.configure(
        connection=connection,  # type: ignore[arg-type]
        target_metadata=target_metadata,
        render_item=_render_item,
        compare_type=True,
        compare_server_default=True,
        # Batch mode lets SQLite handle ALTER TABLE operations it cannot do
        # natively; it is a no-op on PostgreSQL.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online_async() -> None:
    """Run migrations against a live (async) database connection."""
    settings = get_settings()
    engine: AsyncEngine = build_engine(settings.database)
    async with engine.connect() as connection:
        await connection.run_sync(_run_migrations)
        await connection.commit()
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online_async())
