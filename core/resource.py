"""
Runtime resource object.
Tracks used capacity (for example VRAM) and supports blind reservation.
Implements alive_time (soft-used window after release) and keep_alive tracking.
"""
from __future__ import annotations

import asyncio
import time


class Resource:
    """
    Runtime resource with capacity accounting and alive-time tracking.

    alive_time: seconds a model stays in VRAM after task release (e.g. Ollama 5-min default).
                Memory is considered occupied during this window; force-unload is needed to free earlier.
    keep_alive: seconds to actively keep a model loaded after last use (via cron pings). 0=disabled.
    keep_alive_period: cron ping interval for keep_alive.
    provider: id in models.providers used to resolve API URL for force-unload calls.
    """

    def __init__(
        self,
        rid: str,
        rtype: str,
        limits: dict[str, int] | None = None,
        alive_time: int = 0,
        keep_alive: int = 0,
        keep_alive_period: int = 0,
        provider: str | None = None,
    ) -> None:
        self.id = rid
        self.type = rtype
        self.limits: dict[str, int] = {k: int(v) for k, v in (limits or {}).items()}
        self.used: dict[str, int] = {k: 0 for k in self.limits}
        self.consumers: dict[str, dict[str, int]] = {}
        self.alive_time: int = int(alive_time)
        self.keep_alive: int = int(keep_alive)
        self.keep_alive_period: int = int(keep_alive_period)
        self.provider: str | None = provider
        # Soft consumers: models still in memory after task release (within alive_time window).
        # Each entry: {consumer_id, resources, released_at, model_id}
        self._soft_used: list[dict] = []
        self._lock = asyncio.Lock()

    def _compute_soft_used(self) -> dict[str, int]:
        """Sum resources held by soft consumers still within alive_time window."""
        if not self.alive_time:
            return {}
        now = time.time()
        total: dict[str, int] = {}
        for entry in self._soft_used:
            if now - entry["released_at"] < self.alive_time:
                for k, v in entry["resources"].items():
                    total[k] = total.get(k, 0) + v
        return total

    def is_available(self, required: dict[str, int] | None = None) -> bool:
        """Return True if requested amounts fit (accounting for alive-time soft usage)."""
        req = required or {}
        soft = self._compute_soft_used()
        for key, amount in req.items():
            need = int(amount)
            if need <= 0:
                continue
            limit = int(self.limits.get(key, 0))
            used = int(self.used.get(key, 0))
            soft_amount = int(soft.get(key, 0))
            if used + soft_amount + need > limit:
                return False
        return True

    def is_available_after_unload(self, required: dict[str, int] | None = None) -> bool:
        """Return True if amounts fit assuming all soft consumers are force-unloaded."""
        req = required or {}
        for key, amount in req.items():
            need = int(amount)
            if need <= 0:
                continue
            limit = int(self.limits.get(key, 0))
            used = int(self.used.get(key, 0))
            if used + need > limit:
                return False
        return True

    def get_active_soft_consumers(self) -> list[dict]:
        """Return soft consumers still within alive_time window."""
        if not self.alive_time:
            return []
        now = time.time()
        return [e for e in self._soft_used if now - e["released_at"] < self.alive_time]

    def clear_soft_consumer(self, model_id: str) -> None:
        """Remove soft consumer entry for a model (after force-unload)."""
        self._soft_used = [e for e in self._soft_used if e.get("model_id") != model_id]


    async def reserve_blind(
        self,
        required: dict[str, int] | None = None,
        consumer_id: str = "",
    ) -> None:
        """Blindly reserve amounts without availability checks."""
        req = required or {}
        cid = consumer_id.strip()
        async with self._lock:
            for key, amount in req.items():
                inc = int(amount)
                if inc <= 0:
                    continue
                self.used[key] = int(self.used.get(key, 0)) + inc
                if cid:
                    by_consumer = self.consumers.setdefault(cid, {})
                    by_consumer[key] = int(by_consumer.get(key, 0)) + inc

    async def release(
        self,
        reserved: dict[str, int] | None = None,
        consumer_id: str = "",
        model_id: str | None = None,
    ) -> None:
        """Release previously reserved amounts. Adds to soft-used tracking if alive_time > 0."""
        req = reserved or {}
        cid = consumer_id.strip()
        async with self._lock:
            for key, amount in req.items():
                dec = int(amount)
                if dec <= 0:
                    continue
                self.used[key] = max(0, int(self.used.get(key, 0)) - dec)
                if cid and cid in self.consumers:
                    by_consumer = self.consumers[cid]
                    by_consumer[key] = max(0, int(by_consumer.get(key, 0)) - dec)
                    if by_consumer[key] == 0:
                        by_consumer.pop(key, None)
            if cid and cid in self.consumers and not self.consumers[cid]:
                self.consumers.pop(cid, None)
            # Track soft consumer - model may remain in VRAM for alive_time seconds after release
            mid = (model_id or "").strip()
            if self.alive_time > 0 and req and mid:
                # Refresh existing entry so timestamp reflects the latest release
                self._soft_used = [e for e in self._soft_used if e.get("model_id") != mid]
                self._soft_used.append({
                    "consumer_id": cid,
                    "resources": {k: int(v) for k, v in req.items() if int(v) > 0},
                    "released_at": time.time(),
                    "model_id": mid,
                })

    def snapshot(self) -> dict:
        """Return serializable runtime state for UI/API."""
        consumers = [
            {"id": cid, "usage": dict(usage)}
            for cid, usage in sorted(self.consumers.items())
        ]
        soft = self._compute_soft_used()
        soft_consumers = [
            {
                "model_id": e["model_id"],
                "resources": e["resources"],
                "expires_in": max(0, round(self.alive_time - (time.time() - e["released_at"]))),
            }
            for e in self.get_active_soft_consumers()
        ]
        return {
            "id": self.id,
            "type": self.type,
            "limits": dict(self.limits),
            "used": dict(self.used),
            "soft_used": soft,
            "consumers": consumers,
            "soft_consumers": soft_consumers,
        }
