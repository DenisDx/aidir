"""
Resource registry.
Builds and manages runtime Resource objects from config.
"""
from __future__ import annotations

from core.resource import Resource


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
            self._items[rid] = Resource(rid, rtype, limits)

    def all(self) -> list[Resource]:
        """Return all resources."""
        return list(self._items.values())

    def get(self, rid: str) -> Resource | None:
        """Return resource by id."""
        return self._items.get(rid)

    def check_available(self, requirements: dict[str, dict[str, int]] | None = None) -> bool:
        """Return True if all referenced resources have enough free capacity."""
        reqs = requirements or {}
        for rid, need in reqs.items():
            res = self._items.get(rid)
            if res is None:
                return False
            if not res.is_available(need):
                return False
        return True

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
    ) -> None:
        """Release previously reserved resources for a specific consumer."""
        reqs = requirements or {}
        for rid, need in reqs.items():
            res = self._items.get(rid)
            if res is None:
                continue
            await res.release(need, consumer_id=consumer_id)

    def snapshot(self) -> list[dict]:
        """Return list of all runtime resource snapshots."""
        return [res.snapshot() for res in self.all()]
