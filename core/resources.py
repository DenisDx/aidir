"""
Resource registry.
Builds and manages runtime Resource objects from config.
Supports alive_time soft-tracking and force-unload via provider API.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from core import log
from core.resource import Resource

if TYPE_CHECKING:
    import redis.asyncio as aioredis


class Resources:
    """Collection of Resource objects keyed by id."""

    def __init__(self, items: list[dict] | None = None) -> None:
        self._items: dict[str, Resource] = {}
        for it in (items or []):
            rid = str(it.get("id", "")).strip()
            if not rid:
                continue
            rtype = str(it.get("type", "generic"))
            limits = it.get("limits") or {}
            self._items[rid] = Resource(
                rid, rtype, limits,
                alive_time=int(it.get("alive_time") or 0),
                keep_alive=int(it.get("keep_alive") or 0),
                keep_alive_period=int(it.get("keep_alive_period") or 0),
                provider=it.get("provider"),
            )
        self._redis: "aioredis.Redis | None" = None
        self._ns: str = "aidir"

    def set_redis(self, redis: "aioredis.Redis", ns: str = "aidir") -> None:
        """Inject Redis client for model activity persistence (needed by cron keep_alive)."""
        self._redis = redis
        self._ns = ns

    def all(self) -> list[Resource]:
        """Return all resources."""
        return list(self._items.values())

    def get(self, rid: str) -> Resource | None:
        """Return resource by id."""
        return self._items.get(rid)

    def check_available(self, requirements: dict[str, dict[str, int]] | None = None) -> bool:
        """Return True if all resources have enough capacity including alive-time soft usage."""
        reqs = requirements or {}
        for rid, need in reqs.items():
            res = self._items.get(rid)
            if res is None:
                return False
            if not res.is_available(need):
                return False
        return True

    def check_available_after_unload(self, requirements: dict[str, dict[str, int]] | None = None) -> bool:
        """Return True if resources would be available after force-unloading all soft consumers."""
        reqs = requirements or {}
        for rid, need in reqs.items():
            res = self._items.get(rid)
            if res is None:
                return False
            if not res.is_available_after_unload(need):
                return False
        return True

    async def force_unload_for(
        self,
        requirements: dict[str, dict[str, int]] | None = None,
        full_config: dict | None = None,
    ) -> None:
        """Force-unload soft consumers that block needed resources, calling provider API."""
        reqs = requirements or {}
        for rid in reqs:
            res = self._items.get(rid)
            if res is None or not res.alive_time:
                continue
            for entry in res.get_active_soft_consumers():
                model_id = entry.get("model_id") or ""
                if model_id:
                    await self._call_provider_unload(res, model_id, full_config)
                res.clear_soft_consumer(model_id)

    async def _call_provider_unload(
        self,
        res: Resource,
        model_id: str,
        full_config: dict | None,
    ) -> None:
        """Call provider API to force-unload a model (Ollama: POST /api/generate keep_alive=0)."""
        provider_id = res.provider
        if not provider_id or not full_config:
            log("system", "info",
                f"Soft-releasing {model_id} from {res.id} (no provider configured, memory freed in tracking only)")
            return
        providers = ((full_config.get("models") or {}).get("providers") or {})
        provider = providers.get(provider_id) or {}
        base_url = (provider.get("baseUrl") or "").rstrip("/")
        api_type = provider.get("api") or ""
        if not base_url or api_type != "ollama":
            log("system", "info",
                f"Soft-releasing {model_id} from {res.id} (provider {provider_id} not ollama)")
            return
        try:
            import httpx
            url = f"{base_url}/api/generate"
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(url, json={"model": model_id, "keep_alive": 0})
            log("system", "info", f"Force-unloaded {model_id} from {res.id} via {provider_id}")
        except Exception as exc:
            log("system", "warn", f"Force-unload {model_id} on {res.id} failed: {exc}")

    async def reserve_blind(self, requirements: dict[str, dict[str, int]] | None = None) -> None:
        """Blindly reserve all requested resources."""
        await self.reserve_blind_for(requirements, consumer_id="")

    async def reserve_blind_for(
        self,
        requirements: dict[str, dict[str, int]] | None = None,
        consumer_id: str = "",
    ) -> None:
        """Blindly reserve all requested resources for a specific consumer."""
        reqs = requirements or {}
        for rid, need in reqs.items():
            res = self._items.get(rid)
            if res is None:
                continue
            await res.reserve_blind(need, consumer_id=consumer_id)

    async def release(self, requirements: dict[str, dict[str, int]] | None = None) -> None:
        """Release previously reserved resources."""
        await self.release_for(requirements, consumer_id="")

    async def release_for(
        self,
        requirements: dict[str, dict[str, int]] | None = None,
        consumer_id: str = "",
        model_id: str | None = None,
    ) -> None:
        """Release previously reserved resources for a specific consumer."""
        reqs = requirements or {}
        for rid, need in reqs.items():
            res = self._items.get(rid)
            if res is None:
                continue
            await res.release(need, consumer_id=consumer_id, model_id=model_id)
        # Persist model activity to Redis so cron can track keep_alive pings
        if model_id and self._redis:
            await self._persist_model_activity(model_id, reqs)

    async def _persist_model_activity(
        self,
        model_id: str,
        requirements: dict[str, dict[str, int]],
    ) -> None:
        """Write model last-activity timestamp to Redis for cron keep_alive tracking."""
        now = time.time()
        for rid in requirements:
            res = self._items.get(rid)
            if res is None or (not res.keep_alive and not res.alive_time):
                continue
            key = f"{self._ns}:resource:{rid}:activity:{model_id}"
            ttl = max(res.keep_alive, res.alive_time, 3600)
            try:
                import json
                data = {
                    "model_id": model_id,
                    "resource_id": rid,
                    "released_at": now,
                    "keep_alive": res.keep_alive,
                    "keep_alive_period": res.keep_alive_period,
                    "provider": res.provider or "",
                }
                await self._redis.set(key, json.dumps(data), ex=ttl)
            except Exception as exc:
                log("system", "warn", f"Failed to persist model activity for {model_id}: {exc}")

    def snapshot(self) -> list[dict]:
        """Return list of all runtime resource snapshots."""
        return [res.snapshot() for res in self.all()]
