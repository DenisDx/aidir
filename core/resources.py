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
        self._full_config: dict = {}

    def set_redis(self, redis: "aioredis.Redis", ns: str = "aidir") -> None:
        """Inject Redis client for model activity persistence (needed by cron keep_alive)."""
        self._redis = redis
        self._ns = ns

    def set_local_server_manager(self, manager) -> None:
        """Inject the manager used to stop locally owned llama.cpp servers."""
        self._local_server_manager = manager

    def set_full_config(self, full_config: dict) -> None:
        """Store the current configuration for provider-specific resource behavior."""
        self._full_config = full_config if isinstance(full_config, dict) else {}

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

    def check_available_for_reuse(
        self,
        requirements: dict[str, dict[str, int]] | None = None,
        model_id: str | None = None,
        provider_id: str | None = None,
    ) -> bool:
        """Return True when all resources can reuse the same warm model without unload."""
        reqs = requirements or {}
        mid = str(model_id or "").strip()
        if not mid:
            return False
        for rid, need in reqs.items():
            res = self._items.get(rid)
            if res is None:
                return False
            if not res.is_available_for_reuse(need, mid, provider_id):
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
        keep_model_id: str | None = None,
        keep_provider_id: str | None = None,
    ) -> bool:
        """Force-unload soft consumers that block needed resources, calling provider API."""
        reqs = requirements or {}
        keep_mid = str(keep_model_id or "").strip()
        all_unloaded = True
        for rid in reqs:
            res = self._items.get(rid)
            if res is None or not res.alive_time:
                continue
            for entry in res.get_active_soft_consumers():
                model_id = entry.get("model_id") or ""
                entry_provider_id = str(entry.get("provider_id") or "").strip()
                provider_id = entry_provider_id or str(res.provider or "").strip()
                if keep_mid and model_id == keep_mid and (
                    not keep_provider_id or provider_id == keep_provider_id
                ):
                    continue
                if model_id:
                    unloaded = await self._call_provider_unload(res, model_id, provider_id, full_config)
                    if not unloaded:
                        all_unloaded = False
                        continue
                res.clear_soft_consumer(model_id, entry_provider_id or None)
        return all_unloaded

    async def force_unload_all(self, full_config: dict | None = None) -> bool:
        """Unload every currently tracked idle model before service shutdown."""
        all_unloaded = True
        for resource in self.all():
            for entry in list(resource.get_active_soft_consumers()):
                model_id = str(entry.get("model_id") or "").strip()
                entry_provider_id = str(entry.get("provider_id") or "").strip()
                provider_id = entry_provider_id or str(resource.provider or "").strip()
                if not model_id:
                    continue
                unloaded = await self._call_provider_unload(
                    resource,
                    model_id,
                    provider_id,
                    full_config or self._full_config,
                )
                if unloaded:
                    resource.clear_soft_consumer(model_id, entry_provider_id or None)
                else:
                    all_unloaded = False
        return all_unloaded

    def clear_soft_consumers_for_providers(self, provider_ids: list[str]) -> None:
        """Clear tracked model occupancy after their managed server processes stopped."""
        providers = {str(provider_id).strip() for provider_id in provider_ids if str(provider_id).strip()}
        if not providers:
            return
        for resource in self.all():
            resource._soft_used = [
                entry for entry in resource._soft_used
                if str(entry.get("provider_id") or "").strip() not in providers
            ]

    async def _call_provider_unload(
        self,
        res: Resource,
        model_id: str,
        provider_id: str,
        full_config: dict | None,
    ) -> bool:
        """Call provider API to force-unload a model (Ollama: POST /api/generate keep_alive=0)."""
        if not provider_id or not full_config:
            log("system", "info",
                f"Soft-releasing {model_id} from {res.id} (no provider configured, memory freed in tracking only)")
            return False
        providers = ((full_config.get("models") or {}).get("providers") or {})
        provider = providers.get(provider_id) or {}
        base_url = (provider.get("baseUrl") or "").rstrip("/")
        api_type = provider.get("api") or ""
        if api_type == "llama-cpp":
            manager = getattr(self, "_local_server_manager", None)
            if manager is None:
                log("system", "warn", f"Cannot stop llama.cpp provider {provider_id}: server manager unavailable")
                return False
            try:
                stopped = await manager.stop(provider_id)
                if stopped:
                    log("system", "info", f"Force-unloaded {model_id} from {res.id} via {provider_id}")
                return stopped
            except Exception as exc:
                log("system", "warn", f"Force-unload {model_id} on {res.id} failed: {exc}")
                return False
        if not base_url or api_type != "ollama":
            log("system", "info",
                f"Soft-releasing {model_id} from {res.id} (provider {provider_id} not ollama)")
            return False
        try:
            import httpx
            url = f"{base_url}/api/generate"
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, json={"model": model_id, "keep_alive": 0})
                if response.status_code < 200 or response.status_code >= 300:
                    log("system", "warn", f"Force-unload {model_id} on {res.id} returned HTTP {response.status_code}")
                    return False
            log("system", "info", f"Force-unloaded {model_id} from {res.id} via {provider_id}")
            return True
        except Exception as exc:
            log("system", "warn", f"Force-unload {model_id} on {res.id} failed: {exc}")
            return False

    async def reserve_blind(self, requirements: dict[str, dict[str, int]] | None = None) -> None:
        """Blindly reserve all requested resources."""
        await self.reserve_blind_for(requirements, consumer_id="")

    async def reserve_blind_for(
        self,
        requirements: dict[str, dict[str, int]] | None = None,
        consumer_id: str = "",
        model_id: str | None = None,
        provider_id: str | None = None,
    ) -> None:
        """Blindly reserve all requested resources for a specific consumer."""
        reqs = requirements or {}
        for rid, need in reqs.items():
            res = self._items.get(rid)
            if res is None:
                continue
            await res.reserve_blind(need, consumer_id=consumer_id, model_id=model_id, provider_id=provider_id)

    async def release(self, requirements: dict[str, dict[str, int]] | None = None) -> None:
        """Release previously reserved resources."""
        await self.release_for(requirements, consumer_id="")

    async def release_for(
        self,
        requirements: dict[str, dict[str, int]] | None = None,
        consumer_id: str = "",
        model_id: str | None = None,
        provider_id: str | None = None,
    ) -> None:
        """Release previously reserved resources for a specific consumer."""
        reqs = requirements or {}
        persistent = self._provider_api_type(provider_id, self._full_config) == "llama-cpp"
        for rid, need in reqs.items():
            res = self._items.get(rid)
            if res is None:
                continue
            await res.release(
                need,
                consumer_id=consumer_id,
                model_id=model_id,
                provider_id=provider_id,
                persistent=persistent,
            )
        # Persist model activity to Redis so cron can track keep_alive pings
        if model_id and self._redis:
            await self._persist_model_activity(model_id, reqs, provider_id)

    async def _persist_model_activity(
        self,
        model_id: str,
        requirements: dict[str, dict[str, int]],
        provider_id: str | None = None,
    ) -> None:
        """Write model last-activity timestamp to Redis for cron keep_alive tracking."""
        now = time.time()
        for rid in requirements:
            res = self._items.get(rid)
            if res is None or (not res.keep_alive and not res.alive_time):
                continue
            effective_provider_id = str(provider_id or res.provider or "").strip()
            key = f"{self._ns}:resource:{rid}:activity:{effective_provider_id}:{model_id}"
            ttl = max(res.keep_alive, res.alive_time, 3600)
            try:
                import json
                data = {
                    "model_id": model_id,
                    "resource_id": rid,
                    "released_at": now,
                    "keep_alive": res.keep_alive,
                    "keep_alive_period": res.keep_alive_period,
                    "provider": effective_provider_id,
                }
                await self._redis.set(key, json.dumps(data), ex=ttl)
            except Exception as exc:
                log("system", "warn", f"Failed to persist model activity for {model_id}: {exc}")

    def snapshot(self) -> list[dict]:
        """Return list of all runtime resource snapshots."""
        return [res.snapshot() for res in self.all()]

    def restore_owned_llama_cpp_consumers(self, full_config: dict, manager) -> None:
        """Restore persistent VRAM reservations for surviving aidir-owned llama.cpp servers."""
        records = manager.owned_records()
        providers = ((full_config.get("models") or {}).get("providers") or {})
        for provider_id, record in records.items():
            provider = providers.get(provider_id) if isinstance(providers, dict) else None
            if not isinstance(provider, dict) or provider.get("api") != "llama-cpp":
                continue
            model_id = str(record.get("model_id") or "").strip()
            for model in provider.get("models") or []:
                if not isinstance(model, dict) or str(model.get("id") or "").strip() != model_id:
                    continue
                for resource_id, requirements in (model.get("resources") or {}).items():
                    resource = self._items.get(str(resource_id))
                    if resource is not None and isinstance(requirements, dict):
                        resource.add_soft_consumer(requirements, model_id, provider_id, persistent=True)

    @staticmethod
    def _provider_api_type(provider_id: str | None, full_config: dict | None) -> str:
        """Return provider API type when the registry has a current full configuration."""
        if full_config is None:
            return ""
        providers = ((full_config.get("models") or {}).get("providers") or {})
        provider = providers.get(provider_id) if isinstance(providers, dict) else None
        return str((provider or {}).get("api") or "")
