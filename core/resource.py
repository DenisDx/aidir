"""
Runtime resource object.
Tracks used capacity (for example VRAM) and supports blind reservation.
"""
from __future__ import annotations

import asyncio


class Resource:
    """Runtime resource with simple capacity accounting."""

    def __init__(self, rid: str, rtype: str, limits: dict[str, int] | None = None) -> None:
        self.id = rid
        self.type = rtype
        self.limits: dict[str, int] = {k: int(v) for k, v in (limits or {}).items()}
        self.used: dict[str, int] = {k: 0 for k in self.limits}
        self.consumers: dict[str, dict[str, int]] = {}
        self._lock = asyncio.Lock()

    def is_available(self, required: dict[str, int] | None = None) -> bool:
        """Return True if all requested amounts fit into remaining capacity."""
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
    ) -> None:
        """Release previously reserved amounts."""
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

    def snapshot(self) -> dict:
        """Return serializable runtime state for UI/API."""
        consumers = [
            {"id": cid, "usage": dict(usage)}
            for cid, usage in sorted(self.consumers.items())
        ]
        return {
            "id": self.id,
            "type": self.type,
            "limits": dict(self.limits),
            "used": dict(self.used),
            "consumers": consumers,
        }
