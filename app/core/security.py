"""Cryptographic primitives used for authentication and authorization.

Design decisions:

* API keys and passwords are **never** stored in plaintext. They are hashed
  with Argon2id, which is memory-hard and therefore resistant to GPU cracking.
* API keys carry a public, non-secret prefix so that a key can be located in
  the database with an indexed lookup without ever comparing secrets in SQL.
* Access tokens are compact HMAC-SHA256 signed documents. Signature comparison
  is constant-time and the payload is validated before it is trusted.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from argon2 import PasswordHasher
from argon2 import exceptions as argon2_exceptions

from app.core.errors import AuthenticationError

API_KEY_PREFIX: Final[str] = "jmi"
API_KEY_ID_BYTES: Final[int] = 6
API_KEY_SECRET_BYTES: Final[int] = 32
_TOKEN_VERSION: Final[str] = "v1"  # noqa: S105 - a format version, not a secret

_hasher = PasswordHasher(time_cost=2, memory_cost=65_536, parallelism=1)


def configure_hasher(*, time_cost: int, memory_cost_kib: int, parallelism: int) -> None:
    """Re-configure the Argon2 parameters (called once at startup)."""
    global _hasher
    _hasher = PasswordHasher(
        time_cost=time_cost, memory_cost=memory_cost_kib, parallelism=parallelism
    )


def hash_secret(secret: str) -> str:
    """Hash a password or API key with Argon2id."""
    if not secret:
        raise ValueError("refusing to hash an empty secret")
    return _hasher.hash(secret)


def verify_secret(hashed: str, secret: str) -> bool:
    """Verify a secret against its Argon2 hash without leaking timing details."""
    if not hashed or not secret:
        return False
    try:
        return _hasher.verify(hashed, secret)
    except (argon2_exceptions.VerificationError, argon2_exceptions.InvalidHashError):
        return False


def needs_rehash(hashed: str) -> bool:
    """Report whether a stored hash uses outdated Argon2 parameters."""
    try:
        return _hasher.check_needs_rehash(hashed)
    except argon2_exceptions.InvalidHashError:
        return True


@dataclass(frozen=True, slots=True)
class GeneratedApiKey:
    """A freshly minted API key.

    ``secret`` is shown to the user exactly once; only ``key_id`` and the
    Argon2 ``hashed`` value are persisted.
    """

    key_id: str
    secret: str
    hashed: str

    @property
    def full_key(self) -> str:
        return f"{API_KEY_PREFIX}_{self.key_id}_{self.secret}"


def generate_api_key() -> GeneratedApiKey:
    """Create a new API key with a public identifier and a secret component."""
    key_id = secrets.token_hex(API_KEY_ID_BYTES)
    secret = secrets.token_urlsafe(API_KEY_SECRET_BYTES)
    full = f"{API_KEY_PREFIX}_{key_id}_{secret}"
    return GeneratedApiKey(key_id=key_id, secret=secret, hashed=hash_secret(full))


def parse_api_key(raw: str) -> tuple[str, str]:
    """Split a presented API key into ``(key_id, full_key)``.

    Raises:
        AuthenticationError: if the key is not in the expected format.
    """
    if not raw:
        raise AuthenticationError("API key missing")
    # The secret component is URL-safe base64 and may itself contain "_",
    # so only the first two separators are structural.
    cleaned = raw.strip()
    parts = cleaned.split("_", 2)
    if len(parts) != 3 or parts[0] != API_KEY_PREFIX or not parts[1] or not parts[2]:
        raise AuthenticationError("Malformed API key")
    if len(parts[1]) != API_KEY_ID_BYTES * 2 or not all(c in "0123456789abcdef" for c in parts[1]):
        raise AuthenticationError("Malformed API key")
    return parts[1], cleaned


def constant_time_compare(left: str, right: str) -> bool:
    """Compare two strings without leaking their contents through timing."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def mask_secret(value: str, *, visible: int = 4) -> str:
    """Return a display-safe representation of a secret."""
    if not value:
        return ""
    if len(value) <= visible:
        return "*" * len(value)
    return f"{value[:visible]}{'*' * (len(value) - visible)}"


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def sign_token(payload: dict[str, Any], *, secret_key: str, ttl_seconds: int) -> str:
    """Create a compact HMAC-signed token carrying ``payload``."""
    if not secret_key:
        raise ValueError("secret_key is required to sign tokens")
    now = int(datetime.now(UTC).timestamp())
    body = {
        **payload,
        "iat": now,
        "exp": now + int(ttl_seconds),
        "jti": secrets.token_urlsafe(12),
        "ver": _TOKEN_VERSION,
    }
    encoded = _b64url_encode(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
    signature = hmac.new(secret_key.encode(), encoded.encode(), hashlib.sha256).digest()
    return f"{encoded}.{_b64url_encode(signature)}"


def verify_token(token: str, *, secret_key: str) -> dict[str, Any]:
    """Validate a token's signature and expiry and return its payload.

    Raises:
        AuthenticationError: if the token is malformed, forged or expired.
    """
    if not token or token.count(".") != 1:
        raise AuthenticationError("Malformed token")
    encoded, provided_signature = token.split(".", 1)
    expected = hmac.new(secret_key.encode(), encoded.encode(), hashlib.sha256).digest()
    try:
        provided = _b64url_decode(provided_signature)
    except (ValueError, TypeError) as exc:
        raise AuthenticationError("Malformed token signature") from exc
    if not hmac.compare_digest(expected, provided):
        raise AuthenticationError("Invalid token signature")

    try:
        payload = json.loads(_b64url_decode(encoded))
    except (ValueError, TypeError) as exc:
        raise AuthenticationError("Malformed token payload") from exc
    if not isinstance(payload, dict):
        raise AuthenticationError("Malformed token payload")
    if payload.get("ver") != _TOKEN_VERSION:
        raise AuthenticationError("Unsupported token version")

    expires_at = payload.get("exp")
    if not isinstance(expires_at, int):
        raise AuthenticationError("Token is missing an expiry")
    if expires_at < int(datetime.now(UTC).timestamp()):
        raise AuthenticationError("Token has expired")
    return payload


__all__ = [
    "API_KEY_PREFIX",
    "GeneratedApiKey",
    "configure_hasher",
    "constant_time_compare",
    "generate_api_key",
    "hash_secret",
    "mask_secret",
    "needs_rehash",
    "parse_api_key",
    "sign_token",
    "verify_secret",
    "verify_token",
]
