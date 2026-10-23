"""Regression tests for terminating a task that only exists as a Redis hash.

A cancel that silently does nothing leaves the task visible as queued forever:
the scheduler never picks it up and the operator believes the task was stopped.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from core.app import Core
from core.task import STATUS_CANCELED, STATUS_QUEUED
from core.task_types.task_agent import Task_agent


class _Queue:
    """Queue double whose in-memory registry is empty but whose hash exists."""

    def __init__(self, persisted: Task_agent | None) -> None:
        """Store the task that only exists as a persisted hash."""
        self._persisted = persisted
        self.canceled: list[str] = []
        self.deleted: list[str] = []

    def get_task(self, task_id: str) -> Task_agent | None:
        """Report that the task is not in the in-memory registry."""
        return None

    async def get_persisted_task(self, task_id: str) -> Task_agent | None:
        """Return the task that only exists as a Redis hash."""
        return self._persisted

    async def mark_canceled(self, task) -> None:
        """Record the cancellation."""
        self.canceled.append(task.id)
        task.status = STATUS_CANCELED

    async def delete_task(self, task_id: str) -> None:
        """Record the removal."""
        self.deleted.append(task_id)


class TerminatePersistedTaskTests(unittest.IsolatedAsyncioTestCase):
    """A task that is only a Redis hash must still be terminable."""

    async def test_a_persisted_queued_task_is_canceled(self):
        """The cancel reaches the hash instead of doing nothing."""
        task = Task_agent(payload={"model": "m", "messages": []})
        task.status = STATUS_QUEUED
        queue = _Queue(task)

        core = Core.__new__(Core)
        core.queue = queue
        core.scheduler = None

        result = await core.terminate_task(task.id)

        self.assertIsNotNone(result)
        self.assertEqual(queue.canceled, [task.id])
        self.assertEqual(queue.deleted, [task.id])

    async def test_an_unknown_task_is_reported_as_absent(self):
        """A task that does not exist at all is reported as absent."""
        queue = _Queue(None)

        core = Core.__new__(Core)
        core.queue = queue
        core.scheduler = None

        self.assertIsNone(await core.terminate_task("no-such-task"))
        self.assertEqual(queue.canceled, [])


if __name__ == "__main__":
    unittest.main()
