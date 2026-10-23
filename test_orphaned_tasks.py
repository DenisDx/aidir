"""Regression tests for the reaping of the orphaned tasks by the cron cycle.

A task whose hash says `queued` while no queue holds it can never be
dispatched. Nothing else would ever finish it, so the cycle marks it failed and
the dashboard shows a terminal state instead of a task that waits forever.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from core import cron


class _FakeRedis:
    """Redis double that holds task hashes and queue members."""

    def __init__(self, hashes: dict[str, dict[str, str]], queued: dict[str, set[str]]) -> None:
        """Store the hashes and the queue members."""
        self.hashes = dict(hashes)
        self.queued = {key: set(value) for key, value in queued.items()}
        self.closed = False

    async def zrange(self, key: str, start: int, stop: int):
        """Return the members of one queue."""
        return sorted(self.queued.get(key, set()))

    async def hmget(self, key: str, *fields: str):
        """Return the requested fields of one hash."""
        stored = self.hashes.get(key, {})
        return [stored.get(field) for field in fields]

    async def hset(self, key: str, mapping: dict):
        """Apply a hash write."""
        self.hashes.setdefault(key, {}).update(mapping)
        return 1

    async def delete(self, key: str):
        """Remove a hash."""
        self.hashes.pop(key, None)
        return 1

    async def scan(self, cursor: int, match: str = "*", count: int = 100):
        """Return every task key in one page."""
        keys = [key for key in self.hashes if key.startswith(match.rstrip("*"))]
        return 0, keys

    async def get(self, key: str):
        """Return a plain value."""
        return None

    async def set(self, key: str, value, ex=None):
        """Accept a write."""
        return True

    async def ping(self):
        """Accept a ping."""
        return True

    async def aclose(self):
        """Close the double."""
        self.closed = True


class OrphanedTaskTests(unittest.IsolatedAsyncioTestCase):
    """The cycle must finish the tasks that wait in no queue."""

    async def test_a_task_waiting_in_no_queue_is_failed(self):
        """An orphaned task becomes terminal instead of waiting forever."""
        hashes = {
            "aidir111:task:orphan": {
                "id": "orphan",
                "type": "agent",
                "status": "queued",
                "external": "1",
                "finished_at": "",
            },
            "aidir111:task:waiting": {
                "id": "waiting",
                "type": "agent",
                "status": "queued",
                "external": "1",
                "finished_at": "",
            },
        }
        redis = _FakeRedis(hashes, {"aidir111:queue:agent": {"waiting"}})

        with patch.object(cron, "config", {"instance": "aidir111", "cron_period": 1}), \
             patch.object(cron, "_should_run", AsyncMock(return_value=True)), \
             patch.object(cron, "log", lambda *args, **kwargs: None):
            await cron.cleanup_expired_tasks(redis)

        orphan = redis.hashes["aidir111:task:orphan"]
        self.assertEqual(orphan["status"], "failed")
        self.assertEqual(orphan["error_code"], "ORPHANED_TASK")
        self.assertTrue(orphan["finished_at"])

        # A task that is in the queue keeps waiting.
        self.assertEqual(redis.hashes["aidir111:task:waiting"]["status"], "queued")

    async def test_a_finished_task_is_not_touched(self):
        """A terminal task keeps its status."""
        from datetime import datetime, timezone

        hashes = {
            "aidir111:task:done": {
                "id": "done",
                "type": "agent",
                "status": "completed",
                "external": "1",
                "finished_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        redis = _FakeRedis(hashes, {})

        with patch.object(cron, "config", {"instance": "aidir111", "cron_period": 1}), \
             patch.object(cron, "_should_run", AsyncMock(return_value=True)), \
             patch.object(cron, "log", lambda *args, **kwargs: None):
            await cron.cleanup_expired_tasks(redis)

        self.assertEqual(redis.hashes["aidir111:task:done"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
