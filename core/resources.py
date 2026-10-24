"""
Resource registry.
Builds and manages runtime Resource objects from config.
Supports alive_time soft-tracking and force-unload via provider API.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Callable

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
                monitoring=it.get("monitoring"),
                availability=it.get("availability"),
                units=it.get("units"),
                telemetry=it.get("telemetry"),
            )
        self._redis: "aioredis.Redis | None" = None
        self._ns: str = "aidir"
        self._full_config: dict = {}
        self._admission_lock = asyncio.Lock()
        self._admission_locks: dict[str, asyncio.Lock] = {}
        self._model_active: dict[tuple[str, str], int] = {}
        self._candidate_invalidator: Callable[[str, str], None] | None = None

    @asynccontextmanager
    async def _admission_guard(self, *requirement_sets: dict[str, dict[str, int]] | None):
        """Serialize admission only for the resources a request touches."""
        resource_ids = sorted({
            resource_id
            for requirements in requirement_sets
            for resource_id in (requirements or {})
            if resource_id in self._items
        })
        if not resource_ids:
            async with self._admission_lock:
                yield
            return
        locks = [self._admission_locks.setdefault(resource_id, asyncio.Lock()) for resource_id in resource_ids]
        for lock in locks:
            await lock.acquire()
        try:
            yield
        finally:
            for lock in reversed(locks):
                lock.release()

    def apply_config(self, items: list[dict] | None = None) -> dict:
        """Update the registry in place from a new configuration.

        The reservations of the resources that stay are kept: a reload must not
        throw away the memory a running task holds. A resource that appears
        starts empty, and one that disappears is dropped with its state.
        """
        wanted: dict[str, dict] = {}
        for item in (items or []):
            rid = str(item.get("id", "")).strip()
            if rid:
                wanted[rid] = item

        added: list[str] = []
        updated: list[str] = []
        for rid, item in wanted.items():
            existing = self._items.get(rid)
            if existing is None:
                self._items[rid] = Resource(
                    rid,
                    str(item.get("type") or "generic"),
                    item.get("limits") or {},
                    alive_time=int(item.get("alive_time") or 0),
                    keep_alive=int(item.get("keep_alive") or 0),
                    keep_alive_period=int(item.get("keep_alive_period") or 0),
                    provider=item.get("provider"),
                    monitoring=item.get("monitoring"),
                    availability=item.get("availability"),
                    units=item.get("units"),
                    telemetry=item.get("telemetry"),
                )
                added.append(rid)
                continue

            limits = {str(k): int(v) for k, v in (item.get("limits") or {}).items()}
            existing.type = str(item.get("type") or existing.type)
            existing.limits = limits
            existing.used = {metric: existing.used.get(metric, 0) for metric in limits}
            existing.alive_time = int(item.get("alive_time") or 0)
            existing.keep_alive = int(item.get("keep_alive") or 0)
            existing.keep_alive_period = int(item.get("keep_alive_period") or 0)
            existing.provider = item.get("provider")
            existing.monitoring = dict(item.get("monitoring") or {})
            existing.availability = dict(item.get("availability") or {})
            existing.units = (
                {str(metric): str(unit) for metric, unit in (item.get("units") or {}).items()}
                if isinstance(item.get("units"), dict)
                else {}
            )
            existing.telemetry = dict(item.get("telemetry") or {})
            updated.append(rid)

        removed: list[str] = []
        for rid in list(self._items):
            if rid not in wanted:
                removed.append(rid)
                del self._items[rid]

        return {"added": added, "updated": updated, "removed": removed}

    def set_redis(self, redis: "aioredis.Redis", ns: str = "aidir") -> None:
        """Inject Redis client for model activity persistence (needed by cron keep_alive)."""
        self._redis = redis
        self._ns = ns

    def set_local_server_manager(self, manager) -> None:
        """Inject the manager used to stop locally owned llama.cpp servers."""
        self._local_server_manager = manager

    def set_candidate_invalidator(self, callback: Callable[[str, str], None] | None) -> None:
        """Inject the callback that drops routing affinity for an unloaded model."""
        self._candidate_invalidator = callback

    def _invalidate_candidate(self, provider_id: str, model_id: str) -> None:
        """Tell the routing layer that an executor lost its KV cache."""
        if self._candidate_invalidator is None:
            return
        try:
            self._candidate_invalidator(str(provider_id or ""), str(model_id or ""))
        except Exception:
            pass

    def set_full_config(self, full_config: dict) -> None:
        """Store the current configuration for provider-specific resource behavior."""
        self._full_config = full_config if isinstance(full_config, dict) else {}

    def all(self) -> list[Resource]:
        """Return all resources."""
        return list(self._items.values())

    def get(self, rid: str) -> Resource | None:
        """Return resource by id."""
        return self._items.get(rid)

    def set_use(self, rid: str, use: bool) -> Resource | None:
        """Enable or disable a resource for new task reservations during this runtime."""
        resource = self._items.get(rid)
        if resource is not None:
            resource.use = bool(use)
        return resource

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

    def local_admission_requirements(
        self,
        requirements: dict[str, dict[str, int]] | None = None,
    ) -> dict[str, dict[str, int]]:
        """Return requirements that must be reserved locally rather than by a peer aidir."""
        return {
            resource_id: dict(metrics)
            for resource_id, metrics in (requirements or {}).items()
            if (
                (resource := self._items.get(resource_id)) is None
                or resource.availability_snapshot().get("source") != "peer"
            )
        }

    @staticmethod
    def _normalize_requirements(requirements: dict[str, dict[str, int]] | None) -> dict[str, dict[str, int]]:
        """Normalize a resource requirement map to positive integers."""
        out: dict[str, dict[str, int]] = {}
        for resource_id, metrics in (requirements or {}).items():
            if not isinstance(metrics, dict):
                continue
            normalized: dict[str, int] = {}
            for metric, amount in metrics.items():
                try:
                    value = int(amount)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    normalized[str(metric)] = value
            if normalized:
                out[str(resource_id)] = normalized
        return out

    @staticmethod
    def _merge_requirements(*maps: dict[str, dict[str, int]] | None) -> dict[str, dict[str, int]]:
        """Sum several requirement maps."""
        out: dict[str, dict[str, int]] = {}
        for requirements in maps:
            for resource_id, metrics in (requirements or {}).items():
                bucket = out.setdefault(str(resource_id), {})
                for metric, amount in (metrics or {}).items():
                    bucket[str(metric)] = bucket.get(str(metric), 0) + int(amount)
        return out

    def _find_model_cfg(self, provider_id: str, model_id: str) -> dict | None:
        """Return the configured model entry for a concrete provider/model."""
        providers = (((self._full_config or {}).get("models") or {}).get("providers") or {})
        provider = providers.get(provider_id) if isinstance(providers, dict) else None
        if not isinstance(provider, dict):
            return None
        for model in provider.get("models") or []:
            if not isinstance(model, dict):
                continue
            if model_id in {model.get("id"), model.get("name"), model.get("alias")}:
                return model
        return None

    def validate_model_parallel_config(self) -> list[str]:
        """Return configuration errors for models that enable parallel sessions."""
        errors: list[str] = []
        providers = (((self._full_config or {}).get("models") or {}).get("providers") or {})
        if not isinstance(providers, dict):
            return errors
        for provider_id, provider in providers.items():
            if not isinstance(provider, dict):
                continue
            for model in provider.get("models") or []:
                if not isinstance(model, dict):
                    continue
                try:
                    maximum = int(model.get("maxConcurrentRequests", 1) or 1)
                except (TypeError, ValueError):
                    maximum = 1
                if maximum <= 1:
                    continue
                resources = self._normalize_requirements(model.get("resources") or {})
                session = self._normalize_requirements(model.get("sessionResources") or {})
                model_id = str(model.get("id") or model.get("alias") or model.get("name") or "")
                for resource_id, metrics in resources.items():
                    session_metrics = session.get(resource_id) or {}
                    missing = [metric for metric in metrics if int(session_metrics.get(metric, 0) or 0) <= 0]
                    if missing:
                        errors.append(
                            f"{provider_id}/{model_id}: maxConcurrentRequests > 1 requires "
                            f"sessionResources for {resource_id} ({', '.join(missing)})"
                        )
        return errors

    def model_parallel_config(self, provider_id: str, model_id: str) -> tuple[int, dict[str, dict[str, int]]]:
        """Return (max concurrent requests, per-request session requirements)."""
        model = self._find_model_cfg(provider_id, model_id) or {}
        try:
            maximum = max(1, int(model.get("maxConcurrentRequests", 1) or 1))
        except (TypeError, ValueError):
            maximum = 1
        session = self._normalize_requirements(model.get("sessionResources") or {})
        return maximum, session

    @staticmethod
    def _base_consumer_id(provider_id: str, model_id: str) -> str:
        """Return the stable consumer id of one model's base reservation."""
        return f"base:{provider_id}:{model_id}"

    def model_active_count(self, provider_id: str, model_id: str) -> int:
        """Return the number of active requests for one concrete model."""
        return int(self._model_active.get((str(provider_id or ""), str(model_id or "")), 0))

    def mark_model_started(self, provider_id: str, model_id: str) -> int:
        """Increment and return the active request count of one model."""
        key = (str(provider_id or ""), str(model_id or ""))
        self._model_active[key] = int(self._model_active.get(key, 0)) + 1
        return self._model_active[key]

    def mark_model_finished(self, provider_id: str, model_id: str) -> int:
        """Decrement and return the active request count of one model."""
        key = (str(provider_id or ""), str(model_id or ""))
        remaining = max(0, int(self._model_active.get(key, 0)) - 1)
        if remaining:
            self._model_active[key] = remaining
        else:
            self._model_active.pop(key, None)
        return remaining

    def model_is_loaded(
        self,
        requirements: dict[str, dict[str, int]] | None,
        model_id: str,
        provider_id: str,
    ) -> bool:
        """Return whether the model's base footprint is resident on every required resource."""
        reqs = self._normalize_requirements(requirements)
        if not reqs:
            return False
        base_id = self._base_consumer_id(provider_id, model_id)
        for resource_id, metrics in reqs.items():
            resource = self._items.get(resource_id)
            if resource is None:
                return False
            if base_id in resource.consumers:
                continue
            if resource._matching_soft_consumer(model_id, metrics, provider_id) is None:
                return False
        return True

    def _available_snapshot(self, requirements: dict[str, dict[str, int]]) -> dict[str, dict[str, int | float]]:
        """Return the observed or estimated free capacity for the required resources."""
        out: dict[str, dict[str, int | float]] = {}
        for resource_id, metrics in requirements.items():
            resource = self._items.get(resource_id)
            if resource is None:
                continue
            snapshot = resource.availability_snapshot()
            available = snapshot.get("available") or {}
            out[resource_id] = {
                metric: available.get(metric, 0)
                for metric in metrics
            }
        return out

    def evaluate_model_admission(
        self,
        requirements: dict[str, dict[str, int]] | None,
        model_id: str,
        provider_id: str,
    ) -> dict:
        """Return a model-aware admission decision without reserving anything."""
        reqs = self._normalize_requirements(requirements)
        reqs = self.local_admission_requirements(reqs)
        maximum, session = self.model_parallel_config(provider_id, model_id)
        session = self.local_admission_requirements(session)
        active = self.model_active_count(provider_id, model_id)
        result = {
            "admission": "ready",
            "admissible_now": True,
            "admissible_after_unload": True,
            "model_loaded": False,
            "active_count": active,
            "max_concurrent_requests": maximum,
            "session_resources": session,
            "free": self._available_snapshot(reqs),
            "needed": {},
            "resource_ready": True,
            "resource_ready_after_unload": True,
        }
        if not reqs:
            if active >= maximum:
                result.update({"admission": "concurrency", "admissible_now": False})
            return result

        if active >= maximum:
            result.update({"admission": "concurrency", "admissible_now": False, "admissible_after_unload": True})
            return result

        loaded = self.model_is_loaded(reqs, model_id, provider_id)
        result["model_loaded"] = loaded
        if maximum <= 1:
            needed = {} if loaded else reqs
            ready = loaded or self.check_available(reqs)
            after = ready or self.check_available_after_unload(reqs)
            reason = "ready" if ready else ("waiting_unload" if after else "base_memory")
            result.update(
                {
                    "admission": reason,
                    "admissible_now": ready,
                    "admissible_after_unload": after,
                    "needed": needed,
                    "resource_ready": ready,
                    "resource_ready_after_unload": after,
                }
            )
            return result

        needed = session if loaded else self._merge_requirements(reqs, session)
        ready = self.check_available(needed)
        if loaded:
            after = all(
                (resource := self._items.get(resource_id)) is not None
                and resource.available_after_unloading_others(
                    (session.get(resource_id) or {}),
                    model_id,
                    provider_id,
                )
                for resource_id in reqs
            )
        else:
            after = self.check_available_after_unload(needed)
        reason = "ready" if ready else ("waiting_unload" if after else ("session_memory" if loaded else "base_memory"))
        result.update(
            {
                "admission": reason,
                "admissible_now": ready,
                "admissible_after_unload": after,
                "needed": needed,
                "resource_ready": ready,
                "resource_ready_after_unload": after,
            }
        )
        return result

    def memory_slots(self, model_id: str, provider_id: str) -> int | None:
        """Return how many additional sessions fit right now, or None when unknown."""
        maximum, session = self.model_parallel_config(provider_id, model_id)
        session = self.local_admission_requirements(session)
        if maximum <= 1 or not session:
            return None
        remaining = max(0, maximum - self.model_active_count(provider_id, model_id))
        available = self._available_snapshot(session)
        for resource_id, metrics in session.items():
            for metric, amount in metrics.items():
                if int(amount) <= 0:
                    continue
                free = (available.get(resource_id) or {}).get(metric, 0)
                try:
                    remaining = min(remaining, int(float(free) // int(amount)))
                except (TypeError, ValueError, ZeroDivisionError):
                    return None
        return max(0, remaining)

    async def reserve_model_start(
        self,
        requirements: dict[str, dict[str, int]] | None,
        model_id: str,
        provider_id: str,
        consumer_id: str,
    ) -> dict | None:
        """Atomically reserve the base and one session for a parallel model start."""
        reqs = self._normalize_requirements(requirements)
        reqs = self.local_admission_requirements(reqs)
        maximum, session = self.model_parallel_config(provider_id, model_id)
        session = self.local_admission_requirements(session)
        if maximum <= 1:
            return None
        async with self._admission_guard(reqs, session):
            admission = self.evaluate_model_admission(reqs, model_id, provider_id)
            if not admission.get("admissible_now"):
                return admission
            base_id = self._base_consumer_id(provider_id, model_id)
            for resource_id, metrics in reqs.items():
                resource = self._items.get(resource_id)
                if resource is None:
                    return admission
                if base_id not in resource.consumers:
                    await resource.reserve_blind(
                        metrics,
                        consumer_id=base_id,
                        model_id=model_id,
                        provider_id=provider_id,
                    )
            for resource_id, metrics in session.items():
                resource = self._items.get(resource_id)
                if resource is not None:
                    await resource.reserve_active(metrics, consumer_id=consumer_id)
            self.mark_model_started(provider_id, model_id)
            result = dict(admission)
            result["reserved"] = True
            result["base_requirements"] = reqs
            result["session_requirements"] = session
            return result

    async def release_model_session(
        self,
        session_requirements: dict[str, dict[str, int]] | None,
        model_id: str,
        provider_id: str,
        consumer_id: str,
        base_requirements: dict[str, dict[str, int]] | None = None,
        retain_base: bool = True,
    ) -> None:
        """Release one session and, when it was the last, return the base to soft tracking."""
        session = self._normalize_requirements(session_requirements)
        base = self._normalize_requirements(base_requirements)
        async with self._admission_guard(session, base):
            for resource_id, metrics in session.items():
                resource = self._items.get(resource_id)
                if resource is not None:
                    await resource.release(metrics, consumer_id=consumer_id)
            remaining = self.mark_model_finished(provider_id, model_id)
            if remaining:
                return
            base_id = self._base_consumer_id(provider_id, model_id)
            persistent = self._provider_api_type(provider_id, self._full_config) == "llama-cpp"
            for resource_id, metrics in base.items():
                resource = self._items.get(resource_id)
                if resource is None or base_id not in resource.consumers:
                    continue
                await resource.release(
                    metrics,
                    consumer_id=base_id,
                    model_id=model_id if retain_base else None,
                    provider_id=provider_id,
                    persistent=persistent and retain_base,
                )

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
                    self._invalidate_candidate(provider_id, model_id)
                    await self._clear_model_activity(rid, model_id, provider_id)
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
                    self._invalidate_candidate(provider_id, model_id)
                    await self._clear_model_activity(resource.id, model_id, provider_id)
                    resource.clear_soft_consumer(model_id, entry_provider_id or None)
                else:
                    all_unloaded = False
        return all_unloaded

    async def force_release(self, rid: str) -> dict | None:
        """Unload idle models from one resource without interrupting active task reservations."""
        resource = self._items.get(rid)
        if resource is None:
            return None

        active_consumers = sorted(resource.consumers)
        if active_consumers:
            return {
                "released": False,
                "active_consumers": active_consumers,
                "unloaded_models": [],
            }

        before = [
            (str(entry.get("model_id") or ""), str(entry.get("provider_id") or ""))
            for entry in resource.get_active_soft_consumers()
        ]
        released = await self.force_unload_for({rid: {}}, full_config=self._full_config)
        remaining = {
            (str(entry.get("model_id") or ""), str(entry.get("provider_id") or ""))
            for entry in resource.get_active_soft_consumers()
        }
        unloaded_models = [model_id for model_id, provider_id in before if (model_id, provider_id) not in remaining]
        for model_id, provider_id in before:
            if (model_id, provider_id) not in remaining:
                await self._clear_model_activity(rid, model_id, provider_id)
        return {
            "released": released and not remaining,
            "active_consumers": [],
            "unloaded_models": unloaded_models,
        }

    def clear_soft_consumers_for_providers(self, provider_ids: list[str]) -> None:
        """Clear tracked model occupancy after their managed server processes stopped."""
        providers = {str(provider_id).strip() for provider_id in provider_ids if str(provider_id).strip()}
        if not providers:
            return
        for resource in self.all():
            removed = [
                entry for entry in resource._soft_used
                if str(entry.get("provider_id") or "").strip() in providers
            ]
            for entry in removed:
                self._invalidate_candidate(
                    str(entry.get("provider_id") or ""),
                    str(entry.get("model_id") or ""),
                )
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

    async def reserve_if_available(
        self,
        requirements: dict[str, dict[str, int]] | None = None,
        consumer_id: str = "",
        model_id: str | None = None,
        provider_id: str | None = None,
    ) -> bool:
        """Atomically check and reserve all requested resources for one consumer."""
        reqs = requirements or {}
        async with self._admission_guard(reqs):
            if not self.check_available(reqs):
                return False
            await self.reserve_blind_for(
                reqs,
                consumer_id=consumer_id,
                model_id=model_id,
                provider_id=provider_id,
            )
            return True

    async def reserve_if_available_or_reuse(
        self,
        requirements: dict[str, dict[str, int]] | None = None,
        consumer_id: str = "",
        model_id: str | None = None,
        provider_id: str | None = None,
    ) -> bool:
        """Atomically reserve resources for a new or matching warm model."""
        reqs = requirements or {}
        async with self._admission_guard(reqs):
            reusable = bool(model_id) and self.check_available_for_reuse(
                reqs,
                str(model_id),
                str(provider_id or ""),
            )
            if not reusable and not self.check_available(reqs):
                return False
            await self.reserve_blind_for(
                reqs,
                consumer_id=consumer_id,
                model_id=model_id,
                provider_id=provider_id,
            )
            return True

    async def release(self, requirements: dict[str, dict[str, int]] | None = None) -> None:
        """Release previously reserved resources."""
        await self.release_for(requirements, consumer_id="")

    async def release_for(
        self,
        requirements: dict[str, dict[str, int]] | None = None,
        consumer_id: str = "",
        model_id: str | None = None,
        provider_id: str | None = None,
        retain_model: bool = True,
    ) -> None:
        """Release one reservation and optionally retain its model as loaded."""
        reqs = requirements or {}
        tracked_model_id = model_id if retain_model else None
        persistent = retain_model and self._provider_api_type(provider_id, self._full_config) == "llama-cpp"
        for rid, need in reqs.items():
            res = self._items.get(rid)
            if res is None:
                continue
            await res.release(
                need,
                consumer_id=consumer_id,
                model_id=tracked_model_id,
                provider_id=provider_id,
                persistent=persistent,
            )
        # Persist model activity to Redis so cron can track keep_alive pings
        if tracked_model_id and self._redis:
            await self._persist_model_activity(tracked_model_id, reqs, provider_id)

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
                    "last_used_at": now,
                    "last_ping_at": now,
                    "keep_alive": res.keep_alive,
                    "keep_alive_period": res.keep_alive_period,
                    "provider": effective_provider_id,
                }
                await self._redis.set(key, json.dumps(data), ex=ttl)
            except Exception as exc:
                log("system", "warn", f"Failed to persist model activity for {model_id}: {exc}")

    async def _clear_model_activity(self, rid: str, model_id: str, provider_id: str) -> None:
        """Remove a keep-alive activity record after explicitly unloading a model."""
        if not self._redis:
            return
        key = f"{self._ns}:resource:{rid}:activity:{provider_id}:{model_id}"
        try:
            await self._redis.delete(key)
        except Exception as exc:
            log("system", "warn", f"Failed to clear model activity for {model_id}: {exc}")

    def snapshot(self) -> list[dict]:
        """Return list of all runtime resource snapshots."""
        return [self._snapshot_resource(resource) for resource in self.all()]

    def _snapshot_resource(self, resource: Resource) -> dict:
        """Return a resource snapshot enriched with relevant llama.cpp startup errors."""
        snapshot = resource.snapshot()
        manager = getattr(self, "_local_server_manager", None)
        providers = ((self._full_config.get("models") or {}).get("providers") or {})
        errors: list[dict[str, str]] = []
        if manager is not None and isinstance(providers, dict):
            for provider_id, provider in providers.items():
                if not isinstance(provider, dict) or provider.get("api") != "llama-cpp":
                    continue
                error = manager.startup_error(str(provider_id))
                if not error:
                    continue
                for model in provider.get("models") or []:
                    if not isinstance(model, dict) or resource.id not in (model.get("resources") or {}):
                        continue
                    errors.append({
                        "provider_id": str(provider_id),
                        "model_id": str(model.get("id") or ""),
                        "code": str(error.get("code") or "LLAMA_CPP_START_FAILED"),
                        "message": str(error.get("message") or "llama.cpp startup failed"),
                    })
        if errors:
            snapshot["startup_errors"] = errors
        return snapshot

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

    async def restore_activity_consumers(self, full_config: dict) -> int:
        """Restore idle-model occupancy persisted for keep-alive across an aidir restart."""
        if self._redis is None:
            return 0
        import json
        cursor = 0
        restored = 0
        while True:
            cursor, keys = await self._redis.scan(
                cursor,
                match=f"{self._ns}:resource:*:activity:*",
                count=100,
            )
            for key in keys:
                raw = await self._redis.get(key)
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(data, dict):
                    continue
                resource_id = str(data.get("resource_id") or "").strip()
                model_id = str(data.get("model_id") or "").strip()
                provider_id = str(data.get("provider") or "").strip()
                resource = self._items.get(resource_id)
                if resource is None or not model_id or not provider_id:
                    continue
                if self._provider_api_type(provider_id, full_config) == "llama-cpp":
                    # Managed llama.cpp occupancy is restored from its verified PID record.
                    continue
                requirements = self._model_requirements_for(resource_id, model_id, provider_id, full_config)
                if not requirements:
                    continue
                keep_alive = int(data.get("keep_alive") or 0)
                last_used = float(data.get("last_used_at") or data.get("released_at") or 0)
                retention = max(resource.alive_time, keep_alive)
                if retention > 0 and time.time() - last_used >= retention:
                    continue
                resource.add_soft_consumer(
                    requirements,
                    model_id,
                    provider_id,
                    persistent=False,
                    released_at=last_used,
                    expires_at=last_used + retention,
                )
                restored += 1
            if cursor == 0:
                break
        if restored:
            log("system", "info", f"Restored {restored} idle model reservation(s) from activity records")
        return restored

    @staticmethod
    def _model_requirements_for(
        resource_id: str,
        model_id: str,
        provider_id: str,
        full_config: dict | None,
    ) -> dict[str, int] | None:
        """Return configured resource requirements for one provider model."""
        if not full_config:
            return None
        providers = ((full_config.get("models") or {}).get("providers") or {})
        provider = providers.get(provider_id) if isinstance(providers, dict) else None
        if not isinstance(provider, dict):
            return None
        for model in provider.get("models") or []:
            if not isinstance(model, dict):
                continue
            if model_id not in {model.get("id"), model.get("name"), model.get("alias")}:
                continue
            requirements = (model.get("resources") or {}).get(resource_id)
            if not isinstance(requirements, dict):
                return None
            return {str(key): int(value) for key, value in requirements.items()}
        return None

    @staticmethod
    def _provider_api_type(provider_id: str | None, full_config: dict | None) -> str:
        """Return provider API type when the registry has a current full configuration."""
        if full_config is None:
            return ""
        providers = ((full_config.get("models") or {}).get("providers") or {})
        provider = providers.get(provider_id) if isinstance(providers, dict) else None
        return str((provider or {}).get("api") or "")
