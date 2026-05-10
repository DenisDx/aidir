"""
Envid - environment identity configuration object.
EnvidRegistry - manages the in-memory list of Envid objects, persisted to Redis.

On startup:
  1. Load all Envid objects from Redis.
  2. Merge envids from config.envids.items (config values have priority).

Separate from Envid config, per-envid conversation contexts are stored under
  aidir:ctx:<envid_id>  as a JSON string.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import redis.asyncio as aioredis

from core import log


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except (ValueError, TypeError):
        return None


# ── Envid ─────────────────────────────────────────────────────────────────────

@dataclass
class Envid:
    """
    Environment identity config.
    Stores context-building spec (workspace, tooling, files, etc.) for one envid.
    Input: envid_id str + config dict.  Output: Envid instance.
    Persisted to Redis as a HASH; saved_at/used_at track access timestamps.
    """

    id: str
    description: str = ""
    parent: str | None = None          # reference to another envid used as template
    context: dict = field(default_factory=dict)  # context building config
    saved_at: datetime | None = None   # last time this envid was saved to Redis
    used_at: datetime | None = None    # last time this envid was used
    extra: dict = field(default_factory=dict)    # any additional config fields

    # ── Serialization ─────────────────────────────────────────────────────

    def to_redis_hash(self) -> dict[str, str]:
        """Serialize envid to Redis HSET-compatible flat string dict."""
        return {
            "id":          self.id,
            "description": self.description,
            "parent":      self.parent or "",
            "context":     json.dumps(self.context, ensure_ascii=False),
            "saved_at":    self.saved_at.isoformat() if self.saved_at else "",
            "used_at":     self.used_at.isoformat() if self.used_at else "",
            "extra":       json.dumps(self.extra, ensure_ascii=False),
        }

    @classmethod
    def from_redis_hash(cls, data: dict) -> "Envid":
        """Deserialize from Redis HGETALL result dict."""
        return cls(
            id=data.get("id", ""),
            description=data.get("description", ""),
            parent=data.get("parent") or None,
            context=_safe_json(data.get("context", "")),
            saved_at=_parse_dt(data.get("saved_at")),
            used_at=_parse_dt(data.get("used_at")),
            extra=_safe_json(data.get("extra", "")),
        )

    @classmethod
    def from_config(cls, envid_id: str, cfg: dict) -> "Envid":
        """Build Envid from a config.envids.items.<id> entry."""
        known = {"description", "parent", "context"}
        return cls(
            id=envid_id,
            description=cfg.get("description", ""),
            parent=cfg.get("parent") or None,
            context=cfg.get("context", {}),
            extra={k: v for k, v in cfg.items() if k not in known},
        )

    def apply_config_override(self, cfg: dict) -> None:
        """Apply config values over existing fields. Config has priority."""
        if "description" in cfg:
            self.description = cfg["description"]
        if "parent" in cfg:
            self.parent = cfg["parent"] or None
        if "context" in cfg:
            self.context = cfg["context"]
        known = {"description", "parent", "context"}
        for k, v in cfg.items():
            if k not in known:
                self.extra[k] = v


# ── EnvidRegistry ─────────────────────────────────────────────────────────────

class EnvidRegistry:
    """
    In-memory registry of Envid config objects.
    Loads from Redis on startup, merges from config (config values take priority).
    Also manages per-envid conversation contexts stored as JSON in Redis.
    Input: redis client, namespace string.  Output: EnvidRegistry instance.
    """

    def __init__(self, redis: "aioredis.Redis", ns: str = "aidir") -> None:
        self._redis = redis
        self._ns = ns
        self._envids: dict[str, Envid] = {}

    # ── Redis key helpers ─────────────────────────────────────────────────

    def _list_key(self) -> str:
        """Redis SET key holding all envid IDs."""
        return f"{self._ns}:envids"

    def _item_key(self, envid_id: str) -> str:
        """Redis HASH key for one envid config."""
        return f"{self._ns}:envid:{envid_id}"

    def _ctx_key(self, envid_id: str) -> str:
        """Redis STRING key for one envid's conversation context (JSON)."""
        return f"{self._ns}:ctx:{envid_id}"

    # ── Startup ───────────────────────────────────────────────────────────

    async def load_from_redis(self) -> None:
        """Load all stored envid configs from Redis into memory."""
        ids = await self._redis.smembers(self._list_key())
        for raw_id in ids:
            eid = raw_id if isinstance(raw_id, str) else raw_id.decode()
            data = await self._redis.hgetall(self._item_key(eid))
            if data:
                self._envids[eid] = Envid.from_redis_hash(data)
        log("system", "info", f"EnvidRegistry: loaded {len(self._envids)} envid(s) from Redis")

    async def merge_from_config(self, config: dict) -> None:
        """
        Add/update envids from config.envids.items.
        Config values override existing Redis-loaded values.
        """
        items = config.get("envids", {}).get("items", {}) or {}
        if not isinstance(items, dict):
            return
        for envid_id, cfg in items.items():
            if not isinstance(cfg, dict):
                continue
            if envid_id in self._envids:
                self._envids[envid_id].apply_config_override(cfg)
                await self.save(self._envids[envid_id])
                log("system", "info", f"EnvidRegistry: updated envid '{envid_id}' from config")
            else:
                envid = Envid.from_config(envid_id, cfg)
                self._envids[envid_id] = envid
                await self.save(envid)
                log("system", "info", f"EnvidRegistry: added envid '{envid_id}' from config")

    # ── Envid CRUD ────────────────────────────────────────────────────────

    async def save(self, envid: Envid) -> None:
        """Persist envid config to Redis and stamp saved_at."""
        envid.saved_at = _now()
        pipe = self._redis.pipeline(transaction=True)
        pipe.sadd(self._list_key(), envid.id)
        pipe.hset(self._item_key(envid.id), mapping=envid.to_redis_hash())
        await pipe.execute()

    async def mark_used(self, envid_id: str) -> None:
        """Update used_at for envid in memory and Redis."""
        envid = self._envids.get(envid_id)
        if envid:
            envid.used_at = _now()
            await self._redis.hset(
                self._item_key(envid_id), "used_at", envid.used_at.isoformat()
            )

    def get(self, envid_id: str) -> Envid | None:
        """Return Envid config by id, or None."""
        return self._envids.get(envid_id)

    def all(self) -> list[Envid]:
        """Return all Envid configs."""
        return list(self._envids.values())

    def __len__(self) -> int:
        return len(self._envids)

    # ── Context storage ───────────────────────────────────────────────────

    async def load_context(self, envid_id: str) -> dict | None:
        """
        Load stored conversation context for envid from Redis.
        Returns dict with keys: system, history (or None if not found).
        """
        raw = await self._redis.get(self._ctx_key(envid_id))
        if not raw:
            return None
        try:
            ctx = json.loads(raw)
            return ctx if isinstance(ctx, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None

    async def save_context(self, envid_id: str, context: dict) -> None:
        """
        Persist conversation context for envid to Redis.
        Stamps context.saved_at with current UTC time.
        """
        context = dict(context)
        context["saved_at"] = _now().isoformat()
        await self._redis.set(
            self._ctx_key(envid_id),
            json.dumps(context, ensure_ascii=False),
        )
        await self.mark_used(envid_id)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_json(raw: str | None) -> dict:
    """Parse JSON string; return empty dict on any error."""
    if not raw:
        return {}
    try:
        result = json.loads(raw)
        return result if isinstance(result, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}
