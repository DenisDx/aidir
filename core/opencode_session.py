"""Protected Redis storage for OpenCode Go session values."""
from __future__ import annotations

import base64
import hashlib
import os
import uuid

from cryptography.fernet import Fernet, InvalidToken


class OpenCodeSessionError(RuntimeError):
    """Raised when a required OpenCode session cannot be stored or recovered."""


class OpenCodeSessionStore:
    """Create and resolve encrypted caller and managed OpenCode Go sessions."""

    def __init__(self, redis, instance: str, secret: str) -> None:
        """Initialize the store with one Redis client, namespace, and HMAC secret."""
        normalized_secret = str(secret or "").strip()
        if not normalized_secret:
            raise OpenCodeSessionError("AIDIR_OPENCODE_SESSION_HMAC_SECRET is required")
        key = base64.urlsafe_b64encode(hashlib.sha256(normalized_secret.encode("utf-8")).digest())
        self._cipher = Fernet(key)
        self._redis = redis
        self._instance = str(instance or "aidir").strip() or "aidir"

    @classmethod
    def from_environment(cls, redis, instance: str) -> "OpenCodeSessionStore":
        """Create one session store using the required process environment secret."""
        return cls(redis, instance, os.environ.get("AIDIR_OPENCODE_SESSION_HMAC_SECRET", ""))

    def _caller_key(self, task_id: str) -> str:
        """Return the single-task encrypted caller-session Redis key."""
        return f"{self._instance}:opencode-caller-session:v1:{task_id}"

    def _managed_key(self, provider_id: str, model_id: str, identity_digest: str) -> str:
        """Return the stable managed-session Redis key for a resolved model."""
        return (
            f"{self._instance}:openai-session:v1:{provider_id}:{model_id}:{identity_digest}"
        )

    async def store_caller_session(self, task_id: str, value: str, ttl_seconds: int) -> str:
        """Encrypt and store one caller session, returning its non-secret Redis reference."""
        session_value = str(value or "").strip()
        if not session_value:
            raise OpenCodeSessionError("Caller session must not be empty")
        if ttl_seconds <= 0:
            raise OpenCodeSessionError("Caller session TTL must be positive")
        key = self._caller_key(task_id)
        encrypted = self._cipher.encrypt(session_value.encode("utf-8")).decode("ascii")
        await self._redis.set(key, encrypted, ex=ttl_seconds)
        return key

    async def resolve_caller_session(self, reference: str) -> str:
        """Decrypt and return one caller session referenced by a task."""
        encrypted = await self._redis.get(str(reference or ""))
        if not encrypted:
            raise OpenCodeSessionError("Caller session is unavailable")
        try:
            return self._cipher.decrypt(str(encrypted).encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise OpenCodeSessionError("Caller session cannot be decrypted") from exc

    async def delete_caller_session(self, reference: str) -> None:
        """Delete one encrypted caller session after its task reaches a terminal state."""
        await self._redis.delete(str(reference or ""))

    async def resolve_managed_session(
        self,
        provider_id: str,
        model_id: str,
        identity_digest: str,
        ttl_seconds: int,
    ) -> tuple[str, bool]:
        """Return an existing managed session or atomically create a new opaque UUID."""
        if not identity_digest or ttl_seconds <= 0:
            raise OpenCodeSessionError("Managed session identity and TTL are required")
        key = self._managed_key(provider_id, model_id, identity_digest)
        created_value = str(uuid.uuid4())
        created = await self._redis.set(key, created_value, ex=ttl_seconds, nx=True)
        if created:
            return created_value, True
        value = await self._redis.get(key)
        if not value:
            raise OpenCodeSessionError("Managed session creation race did not produce a value")
        return str(value), False

    async def refresh_managed_session(
        self,
        provider_id: str,
        model_id: str,
        identity_digest: str,
        ttl_seconds: int,
    ) -> None:
        """Refresh a managed-session expiry after a successful upstream response."""
        key = self._managed_key(provider_id, model_id, identity_digest)
        if not await self._redis.expire(key, ttl_seconds):
            raise OpenCodeSessionError("Managed session expired before it could be refreshed")
