"""Regression coverage for Redis-backed task recovery after service restart."""
from __future__ import annotations

import asyncio
import json
import unittest

from core.queue_manager import QueueManager
from core.task import STATUS_FAILED, STATUS_QUEUED, STATUS_RUNNING
from core.task_types.task_agent import Task_agent
from core.task_types.task_tool import Task_tool


class _Pipeline:
    """Collect fake Redis operations and apply them on execute."""

    def __init__(self, redis):
        """Store the target fake Redis instance."""
        self.redis = redis
        self.operations = []

    def hset(self, key, mapping):
        """Queue a hash update."""
        self.operations.append(("hset", key, mapping))
        return self

    def zrem(self, key, member):
        """Queue a sorted-set removal."""
        self.operations.append(("zrem", key, member))
        return self

    async def execute(self):
        """Apply queued operations in order."""
        for operation, key, value in self.operations:
            if operation == "hset":
                self.redis.hashes.setdefault(key, {}).update(value)
            else:
                self.redis.zsets.setdefault(key, []).remove(value) if value in self.redis.zsets.setdefault(key, []) else None


class _Redis:
    """Small Redis fake supporting only startup recovery operations."""

    def __init__(self):
        """Initialize empty hashes and priority queues."""
        self.hashes = {}
        self.zsets = {}

    async def zrange(self, key, start, stop):
        """Return all task ids in one fake priority queue."""
        return list(self.zsets.get(key, []))

    async def hgetall(self, key):
        """Return a copy of one stored task hash."""
        return dict(self.hashes.get(key, {}))

    async def zrem(self, key, member):
        """Remove one queue member."""
        members = self.zsets.get(key, [])
        if member in members:
            members.remove(member)

    async def hset(self, key, field=None, value=None, mapping=None):
        """Write hash fields using Redis-compatible call forms."""
        target = self.hashes.setdefault(key, {})
        if mapping is not None:
            target.update(mapping)
        elif field is not None:
            target[field] = value

    def pipeline(self, transaction=True):
        """Return a batched operation collector."""
        return _Pipeline(self)

    async def scan_iter(self, match, count):
        """Yield stored task keys for the requested namespace."""
        for key in list(self.hashes):
            if key.startswith("aidir:task:"):
                yield key


class QueueRestartRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """Verify queued hydration and stale-running task terminalization."""

    async def test_recovery_hydrates_queue_quarantines_invalid_and_fails_running(self):
        """Restore valid queue entries without replaying abandoned running work."""
        redis = _Redis()
        queued_agent = Task_agent(payload={"model": "queued"}, external=True)
        queued_agent.status = STATUS_QUEUED
        queued_tool = Task_tool(payload={"tool": "queued"}, external=True)
        queued_tool.status = STATUS_QUEUED
        running = Task_agent(payload={"model": "running"}, external=True)
        running.status = STATUS_RUNNING
        redis.hashes[f"aidir:task:{queued_agent.id}"] = queued_agent.to_redis_hash()
        redis.hashes[f"aidir:task:{queued_tool.id}"] = queued_tool.to_redis_hash()
        redis.hashes[f"aidir:task:{running.id}"] = running.to_redis_hash()
        redis.zsets["aidir:queue:agent"] = [queued_agent.id, "missing-task"]
        redis.zsets["aidir:queue:tool"] = [queued_tool.id]

        queue = QueueManager(redis)
        result = await queue.recover_startup_tasks()

        self.assertEqual(result, {"recovered": 2, "quarantined": 1, "restarted": 1})
        self.assertEqual(queue.get_task(queued_agent.id).payload["model"], "queued")
        self.assertEqual(queue.get_task(queued_tool.id).payload["tool"], "queued")
        self.assertNotIn("missing-task", redis.zsets["aidir:queue:agent"])
        quarantine = json.loads(redis.hashes["aidir:queue:quarantine"]["missing-task"])
        self.assertEqual(quarantine["task_type"], "agent")
        self.assertEqual(redis.hashes[f"aidir:task:{running.id}"]["status"], STATUS_FAILED)
        self.assertEqual(json.loads(redis.hashes[f"aidir:task:{running.id}"]["error"])["code"], "SERVICE_RESTARTED")


if __name__ == "__main__":
    unittest.main()