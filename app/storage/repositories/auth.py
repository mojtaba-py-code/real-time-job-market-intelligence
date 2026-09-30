"""Persistence for API credentials.

The database only ever sees the Argon2 hash of a key. The public ``key_id``
component makes the lookup a single indexed query, so verification cost is one
hash comparison regardless of how many keys exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select, update

from app.core.security import GeneratedApiKey, generate_api_key, verify_secret
from app.core.timeutils import ensure_utc, utcnow
from app.models.enums import ROLE_SCOPES, Scope, UserRole
from app.storage.models import ApiKey
from app.storage.repositories.base import BaseRepository, affected_rows


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller behind a request."""

    id: str
    key_id: str
    name: str
    role: UserRole
    scopes: frozenset[Scope]

    def has_scope(self, scope: Scope) -> bool:
        """Whether the principal may perform an action (admin implies all)."""
        return Scope.ADMIN in self.scopes or scope in self.scopes


class ApiKeyRepository(BaseRepository):
    """Issue, verify and revoke API keys."""

    async def create(
        self,
        *,
        name: str,
        role: UserRole = UserRole.VIEWER,
        scopes: set[Scope] | None = None,
        expires_at: datetime | None = None,
    ) -> tuple[GeneratedApiKey, ApiKey]:
        """Mint a new key. The plaintext is returned exactly once."""
        generated = generate_api_key()
        granted = scopes if scopes is not None else set(ROLE_SCOPES[role])
        row = ApiKey(
            key_id=generated.key_id,
            hashed_key=generated.hashed,
            name=name,
            role=str(role),
            scopes=sorted(str(s) for s in granted),
            active=True,
            expires_at=expires_at,
        )
        self.session.add(row)
        await self.session.flush()
        return generated, row

    async def authenticate(self, key_id: str, presented_key: str) -> Principal | None:
        """Verify a presented key and return the principal it belongs to."""
        stmt = select(ApiKey).where(ApiKey.key_id == key_id).where(ApiKey.active.is_(True))
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        if row.expires_at is not None and ensure_utc(row.expires_at) < utcnow():
            return None
        if not verify_secret(row.hashed_key, presented_key):
            return None

        row.last_used_at = utcnow()
        return Principal(
            id=row.id,
            key_id=row.key_id,
            name=row.name,
            role=UserRole(row.role),
            scopes=frozenset(Scope(s) for s in (row.scopes or []) if s in set(Scope)),
        )

    async def revoke(self, key_id: str) -> bool:
        result = await self.session.execute(
            update(ApiKey).where(ApiKey.key_id == key_id).values(active=False)
        )
        return affected_rows(result) > 0

    async def delete(self, key_id: str) -> bool:
        result = await self.session.execute(delete(ApiKey).where(ApiKey.key_id == key_id))
        return affected_rows(result) > 0

    async def list_keys(self, *, include_inactive: bool = False) -> list[ApiKey]:
        stmt = select(ApiKey).order_by(ApiKey.created_at.desc())
        if not include_inactive:
            stmt = stmt.where(ApiKey.active.is_(True))
        return list((await self.session.execute(stmt)).scalars())

    async def count_active(self) -> int:
        rows = await self.list_keys()
        return len(rows)


__all__ = ["ApiKeyRepository", "Principal"]
