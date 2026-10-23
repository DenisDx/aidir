"""Regression tests for the bound on a task that waits in the queue.

A task that waits must always have a bound, and a task that leaves the queue
must not keep a non-terminal status: a hash that says `queued` while no queue
holds it is displayed as waiting forever and nothing ever finishes it.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from core.endpoints.endpoint_openaix import Endpoint_openaix
from core.queue_manager import QueueManager
from core.task import STATUS_CANCELED, STATUS_QUEUED, Task
from core.task_types.task_agent import Task_agent


class RequestTimeoutTests(unittest.TestCase):
    """A request may not remove the bound of its own task."""

    def test_a_zero_timeout_falls_back_to_the_configured_default(self):
        """`timeout: 0` would mean "no limit" in the task model."""
        self.assertEqual(Endpoint_openaix._resolve_timeout(0, 300), 300)
        self.assertEqual(Endpoint_openaix._resolve_timeout(0, 120, "queue_timeout"), 120)

    def test_a_positive_timeout_is_kept(self):
        """An explicit timeout is honored."""
        self.assertEqual(Endpoint_openaix._resolve_timeout(45, 300), 45)

    def test_a_negative_timeout_is_refused(self):
        """A negative timeout is a bad request."""
        with self.assertRaises(Exception):
            Endpoint_openaix._resolve_timeout(-1, 300)


class _FakeRedis:
    """Redis double that records the writes of the queue manager."""

    def __init__(self) -> None:
        """Start with an empty store."""
        self.hashes: dict[str, dict[str, str]] = {}
        self.pipelines: list[list[tuple]] = []

    def pipeline(self, transaction: bool = True):
        """Return a pipeline double."""
        return _FakePipeline(self)

    async def hset(self, key: str, mapping: dict) -> int:
        """Record a hash write."""
        self.hashes.setdefault(key, {}).update(mapping)
        return 1

    async def hgetall(self, key: str) -> dict[str, str]:
        """Return a recorded hash."""
        return dict(self.hashes.get(key, {}))

    async def zadd(self, key: str, mapping: dict) -> int:
        """Accept a queue write."""
        return 1

    async def delete(self, key: str) -> int:
        """Accept a delete."""
        return 1

    async def zrem(self, key: str, *members) -> int:
        """Accept a queue removal."""
        return 1

    async def execute(self) -> None:
        """Accept the transaction."""


class _FakePipeline:
    """Pipeline double that applies the commands immediately."""

    def __init__(self, redis: _FakeRedis) -> None:
        """Store the redis double."""
        self._redis = redis
        self._commands: list[tuple] = []

    def hset(self, key: str, mapping: dict):
        """Queue a hash write."""
        self._commands.append(("hset", key, mapping))
        return self

    def delete(self, key: str):
        """Queue a delete."""
        self._commands.append(("delete", key))
        return self

    def zrem(self, key: str, *members):
        """Queue a queue removal."""
        self._commands.append(("zrem", key, members))
        return self

    async def execute(self) -> None:
        """Apply the queued commands."""
        for command in self._commands:
            if command[0] == "hset":
                await self._redis.hset(command[1], command[2])
            elif command[0] == "delete":
                await self._redis.delete(command[1])


class DeleteTaskTests(unittest.IsolatedAsyncioTestCase):
    """A removed task must not keep a waiting status."""

    async def test_a_waiting_external_task_is_marked_canceled(self):
        """The hash of a removed waiting task becomes terminal."""
        redis = _FakeRedis()
        queue = QueueManager(redis, instance="aidir")
        task = Task_agent(payload={"model": "m", "messages": []})
        task.status = STATUS_QUEUED
        task.external = True
        queue._tasks[task.id] = task
        await redis.hset(f"aidir:task:{task.id}", task.to_redis_hash())

        await queue.delete_task(task.id)

        stored = await redis.hgetall(f"aidir:task:{task.id}")
        self.assertEqual(stored.get("status"), STATUS_CANCELED)
        self.assertEqual(stored.get("error_code"), "TASK_REMOVED")
        self.assertTrue(stored.get("finished_at"))

    async def test_a_finished_external_task_keeps_its_status(self):
        """A completed task is not rewritten by the removal."""
        redis = _FakeRedis()
        queue = QueueManager(redis, instance="aidir")
        task = Task_agent(payload={"model": "m", "messages": []})
        task.status = "completed"
        task.external = True
        queue._tasks[task.id] = task
        await redis.hset(f"aidir:task:{task.id}", task.to_redis_hash())

        await queue.delete_task(task.id)

        stored = await redis.hgetall(f"aidir:task:{task.id}")
        self.assertEqual(stored.get("status"), "completed")


if __name__ == "__main__":
    unittest.main()
