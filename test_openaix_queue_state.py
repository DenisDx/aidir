"""Regression tests for OpenAIx queue state aggregation."""

from __future__ import annotations

import unittest
import sys
import types


redis_module = types.ModuleType("redis")
redis_asyncio = types.ModuleType("redis.asyncio")


class _Redis:
    """Stub redis client type for import-time compatibility."""


redis_asyncio.Redis = _Redis
redis_module.asyncio = redis_asyncio
sys.modules.setdefault("redis", redis_module)
sys.modules.setdefault("redis.asyncio", redis_asyncio)

from core.queue_manager import QueueManager


class _FakeRedis:
    def __init__(self, zsets: dict[str, list[tuple[str, int]]], hashes: dict[str, dict[str, str]]) -> None:
        self._zsets = zsets
        self._hashes = hashes

    async def zrange(self, key: str, start: int, end: int, withscores: bool = False):
        items = list(self._zsets.get(key, []))
        if end == -1:
            items = items[start:]
        else:
            items = items[start : end + 1]
        if withscores:
            return items
        return [task_id for task_id, _ in items]

    async def hgetall(self, key: str):
        return dict(self._hashes.get(key, {}))


class TestOpenAIxQueueState(unittest.IsolatedAsyncioTestCase):
    """Validate queue state aggregation against queued Redis data."""

    async def test_resource_queue_state_counts_and_priority_histogram(self) -> None:
        """Aggregates queue counts for matching resource requirements only."""
        queue_key = "aidir:queue:agent"
        task_1 = "task-1"
        task_2 = "task-2"
        task_3 = "task-3"
        task_4 = "task-4"

        target_requirements = {"local_machine": {"VRAM": 16000}}

        fake = _FakeRedis(
            zsets={
                queue_key: [
                    (task_1, 5),
                    (task_2, 10),
                    (task_3, 20),
                    (task_4, 5),
                ]
            },
            hashes={
                "aidir:task:task-1": {
                    "status": "queued",
                    "priority": "5",
                    "resource_requirements": '{"local_machine": {"VRAM": 16000}}',
                },
                "aidir:task:task-2": {
                    "status": "queued",
                    "priority": "10",
                    "resource_requirements": '{"local_machine": {"VRAM": 16000}}',
                },
                "aidir:task:task-3": {
                    "status": "queued",
                    "priority": "20",
                    "resource_requirements": '{"local_machine": {"VRAM": 12000}}',
                },
                "aidir:task:task-4": {
                    "status": "running",
                    "priority": "5",
                    "resource_requirements": '{"local_machine": {"VRAM": 16000}}',
                },
            },
        )

        queue = QueueManager(fake)
        state = await queue.get_resource_queue_state(target_requirements, priority=5)

        self.assertEqual(state["queued_count_total"], 2)
        self.assertEqual(state["queued_count_below_priority"], 1)
        self.assertEqual(
            state["priority_counts"],
            [
                {"priority": 5, "count": 1},
                {"priority": 10, "count": 1},
            ],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)