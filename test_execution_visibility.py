"""Regression tests for the visibility of the execution state.

The operator must be able to see why a task waits without reading a log that
the default log level hides: the dashboard carries the reason, the status route
carries the deferred tasks, the orphans and the provider startup errors, and the
quarantined task receives a terminal status.
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import AsyncMock, patch

from core.queue_manager import QueueManager
from core.scheduler import Scheduler
from core.task import STATUS_CANCELED, STATUS_QUEUED
from core.task_types.task_agent import Task_agent


class _Queue:
    """Queue double for the scheduler bookkeeping."""

    def __init__(self, tasks: list[Task_agent] | None = None) -> None:
        """Store the tasks."""
        self._tasks = {task.id: task for task in (tasks or [])}
        self.failed: list[tuple[str, dict]] = []

    def get_task(self, task_id: str):
        """Return a task by identifier."""
        return self._tasks.get(task_id)

    async def add_task(self, task, is_retry=False) -> None:
        """Accept a deferred task."""

    async def mark_failed(self, task, error: dict) -> None:
        """Record a failed task."""
        self.failed.append((task.id, error))

    async def mark_running(self, task_id: str, worker_id: str) -> None:
        """Accept the running transition."""

    async def mark_completed(self, task) -> None:
        """Accept a completed task."""

    async def mark_canceled(self, task) -> None:
        """Accept a canceled task."""


class _Worker:
    """Worker double."""

    id = "openaix"
    task_type = "agent"
    enabled = True


class DeferralVisibilityTests(unittest.TestCase):
    """The scheduler knows why every waiting task waits."""

    def _scheduler(self) -> Scheduler:
        """A scheduler with an empty queue."""
        return Scheduler(queue=_Queue(), workers={"openaix": _Worker()})

    def test_a_deferred_task_records_its_reason(self):
        """The reason is kept with the task and its route."""
        scheduler = self._scheduler()
        task = Task_agent(payload={"model": "m", "messages": []})
        task.route_provider_id = "remote_aidir_110"
        task.model_id = "m-remote"

        scheduler._note_deferral(task, "the remote aidir cannot run the model")

        entry = scheduler.defer_reason_for(task.id)
        self.assertEqual(entry["reason"], "the remote aidir cannot run the model")
        self.assertEqual(entry["provider"], "remote_aidir_110")
        self.assertEqual(entry["model"], "m-remote")
        self.assertIn("since", entry)

    def test_the_snapshot_counts_the_reasons(self):
        """The status carries the number of waiting tasks per reason."""
        scheduler = self._scheduler()
        first = Task_agent(payload={"model": "m", "messages": []})
        second = Task_agent(payload={"model": "m", "messages": []})

        scheduler._note_deferral(first, "insufficient resources")
        scheduler._note_deferral(second, "insufficient resources")

        snapshot = scheduler.deferred_tasks_snapshot()
        self.assertEqual(snapshot["count"], 2)
        self.assertEqual(snapshot["by_reason"], {"insufficient resources": 2})
        self.assertEqual(len(snapshot["tasks"]), 2)

    def test_a_running_task_is_not_waiting_any_more(self):
        """A task that starts is removed from the waiting list."""
        scheduler = self._scheduler()
        task = Task_agent(payload={"model": "m", "messages": []})
        scheduler._note_deferral(task, "insufficient resources")
        scheduler._clear_deferral(task.id)
        self.assertEqual(scheduler.deferred_tasks_snapshot()["count"], 0)


class QuarantineStatusTests(unittest.IsolatedAsyncioTestCase):
    """A quarantined task is terminal, not waiting."""

    async def test_the_quarantined_hash_gets_a_terminal_status(self):
        """The hash of a quarantined task does not keep `queued`."""
        from test_queue_bounds import _FakeRedis

        redis = _FakeRedis()
        queue = QueueManager(redis, instance="aidir")
        await redis.hset("aidir:task:t1", {
            "id": "t1",
            "type": "agent",
            "status": STATUS_QUEUED,
            "external": "1",
            "finished_at": "",
        })

        await queue._mark_quarantined("t1", "agent", "the hash does not match the queue")

        stored = await redis.hgetall("aidir:task:t1")
        self.assertEqual(stored.get("status"), "failed")
        self.assertEqual(stored.get("error_code"), "QUARANTINED")
        self.assertTrue(stored.get("finished_at"))


class RuntimeHealthTests(unittest.IsolatedAsyncioTestCase):
    """The Core reports the state of the execution."""

    async def test_the_health_carries_the_deferred_tasks_and_the_orphans(self):
        """The status route sees the waiting tasks, the orphans and the failures."""
        from core.app import Core

        core = Core.__new__(Core)
        core.scheduler = Scheduler(queue=_Queue(), workers={"openaix": _Worker()})
        core.llama_cpp_server_manager = None
        core.redis = AsyncMock()
        core.redis.get = AsyncMock(return_value="3")
        core.config = AsyncMock()
        core.config.get = lambda key, default=None: "aidir111" if key == "instance" else default

        task = Task_agent(payload={"model": "m", "messages": []})
        core.scheduler._note_deferral(task, "insufficient resources")

        health = await core.get_runtime_health()
        self.assertEqual(health["deferred"]["count"], 1)
        self.assertEqual(health["orphaned_tasks"], 3)
        self.assertEqual(health["provider_startup_errors"], {})


if __name__ == "__main__":
    unittest.main()
