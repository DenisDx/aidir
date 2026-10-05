"""Regression tests for scheduling independent work behind blocked tasks."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock

from core.resource_monitor import ResourceMonitor
from core.scheduler import Scheduler
from core.task import STATUS_QUEUED
from core.task_types.task_agent import Task_agent
from core.worker import WorkerResult
from core.resources import Resources


class _Queue:
    """Minimal priority queue double for scheduler dispatch tests."""

    def __init__(self, tasks: list[Task_agent]) -> None:
        """Store tasks in their configured priority order."""
        self._tasks = {task.id: task for task in tasks}
        self._queued = sorted(tasks, key=lambda task: task.priority)
        self.completed: list[str] = []

    async def pop_next(self, task_type: str) -> str | None:
        """Remove and return the next task of the requested type."""
        for index, task in enumerate(self._queued):
            if task.type == task_type:
                return self._queued.pop(index).id
        return None

    def get_task(self, task_id: str) -> Task_agent | None:
        """Return a queued task by identifier."""
        return self._tasks.get(task_id)

    async def add_task(self, task: Task_agent) -> None:
        """Return a deferred task to the priority queue."""
        task.status = STATUS_QUEUED
        self._queued.append(task)
        self._queued.sort(key=lambda queued_task: queued_task.priority)

    async def mark_running(self, task_id: str, worker_id: str) -> None:
        """Accept the running transition required by Scheduler."""

    async def mark_completed(self, task: Task_agent) -> None:
        """Record a successfully executed task."""
        self.completed.append(task.id)

    async def mark_failed(self, task: Task_agent, error: dict) -> None:
        """Accept failures not expected by this regression."""

    async def mark_canceled(self, task: Task_agent) -> None:
        """Accept cancellations not expected by this regression."""


class _Worker:
    """Worker double that records every dispatched task."""

    id = "openaix"
    task_type = "agent"
    enabled = True

    def __init__(self, started: asyncio.Event) -> None:
        """Store the event used to observe dispatch."""
        self.started = started
        self.executed: list[str] = []

    async def execute(self, task: Task_agent, emit_chunk=None) -> WorkerResult:
        """Record the task and return a successful result."""
        self.executed.append(task.id)
        self.started.set()
        return WorkerResult(ok=True, data={})


class _BlockingWorker(_Worker):
    """Worker double that holds active reservations until the test releases it."""

    def __init__(self, expected_tasks: int) -> None:
        """Create start and release events for a known number of tasks."""
        super().__init__(asyncio.Event())
        self.expected_tasks = expected_tasks
        self.release = asyncio.Event()

    async def execute(self, task: Task_agent, emit_chunk=None) -> WorkerResult:
        """Record dispatch and retain the reservation until explicitly released."""
        self.executed.append(task.id)
        if len(self.executed) >= self.expected_tasks:
            self.started.set()
        await self.release.wait()
        return WorkerResult(ok=True, data={})


class TestSchedulerQueueBypass(unittest.IsolatedAsyncioTestCase):
    """Verify independent work can pass a blocked queue head."""

    async def test_embedding_runs_behind_resource_blocked_llama_task(self) -> None:
        """Dispatch an embedding task while a higher-priority llama task waits for VRAM."""
        llama_task = Task_agent(payload={"model": "llama"}, stream=False)
        llama_task.priority = 0
        llama_task.resource_requirements = {"local_machine": {"VRAM": 22000}}
        embedding_task = Task_agent(payload={"model": "embedding"}, stream=False)
        embedding_task.priority = 5
        embedding_task.resource_requirements = {}
        queue = _Queue([llama_task, embedding_task])
        resources = Resources([{"id": "local_machine", "type": "cuda", "limits": {"VRAM": 22000}}])
        await resources.reserve_blind_for({"local_machine": {"VRAM": 22000}}, consumer_id="active-llama")
        started = asyncio.Event()
        worker = _Worker(started)
        scheduler = Scheduler(
            queue=queue,
            workers={worker.id: worker},
            resources=resources,
        )

        scheduler_task = asyncio.create_task(scheduler.run())
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
        finally:
            scheduler.stop()
            await scheduler_task

        self.assertEqual(worker.executed, [embedding_task.id])
        self.assertIn(llama_task, queue._queued)

    async def test_tasks_within_one_resource_capacity_start_together(self) -> None:
        """Start queued tasks whose combined VRAM demand exactly fits one resource."""
        first_task = Task_agent(payload={"model": "first"}, stream=False)
        first_task.priority = 0
        first_task.resource_requirements = {"gpu": {"VRAM": 6}}
        second_task = Task_agent(payload={"model": "second"}, stream=False)
        second_task.priority = 5
        second_task.resource_requirements = {"gpu": {"VRAM": 4}}
        queue = _Queue([first_task, second_task])
        resources = Resources([{"id": "gpu", "type": "cuda", "limits": {"VRAM": 10}}])
        worker = _BlockingWorker(expected_tasks=2)
        scheduler = Scheduler(queue=queue, workers={worker.id: worker}, resources=resources)

        scheduler_task = asyncio.create_task(scheduler.run())
        try:
            await asyncio.wait_for(worker.started.wait(), timeout=1)
            self.assertCountEqual(worker.executed, [first_task.id, second_task.id])
            self.assertEqual(resources.get("gpu").used["VRAM"], 10)
        finally:
            worker.release.set()
            await scheduler.wait_for_active_tasks(timeout=1)
            scheduler.stop()
            await scheduler_task

    async def test_tasks_on_independent_resources_start_together(self) -> None:
        """Start queued tasks that each fully consume a distinct resource."""
        local_task = Task_agent(payload={"model": "local"}, stream=False)
        local_task.priority = 0
        local_task.resource_requirements = {"local_gpu": {"VRAM": 10}}
        remote_task = Task_agent(payload={"model": "remote"}, stream=False)
        remote_task.priority = 5
        remote_task.resource_requirements = {"remote_gpu": {"VRAM": 10}}
        queue = _Queue([local_task, remote_task])
        resources = Resources([
            {"id": "local_gpu", "type": "cuda", "limits": {"VRAM": 10}},
            {"id": "remote_gpu", "type": "cuda", "limits": {"VRAM": 10}},
        ])
        worker = _BlockingWorker(expected_tasks=2)
        scheduler = Scheduler(queue=queue, workers={worker.id: worker}, resources=resources)

        scheduler_task = asyncio.create_task(scheduler.run())
        try:
            await asyncio.wait_for(worker.started.wait(), timeout=1)
            self.assertCountEqual(worker.executed, [local_task.id, remote_task.id])
        finally:
            worker.release.set()
            await scheduler.wait_for_active_tasks(timeout=1)
            scheduler.stop()
            await scheduler_task

    async def test_command_availability_is_refreshed_before_dispatch(self) -> None:
        """Dispatch only when a fresh command availability observation fits the task."""
        task = Task_agent(payload={"model": "observed"}, stream=False)
        task.resource_requirements = {"gpu": {"VRAM": 6}}
        queue = _Queue([task])
        resources = Resources([{
            "id": "gpu",
            "type": "cuda",
            "limits": {"VRAM": 10},
            "availability": {
                "command": "nvidia-smi",
                "metric": "VRAM",
            },
        }])
        monitor = ResourceMonitor(resources)
        monitor._run_command = AsyncMock(return_value="6")  # type: ignore[method-assign]
        started = asyncio.Event()
        worker = _Worker(started)
        scheduler = Scheduler(
            queue=queue,
            workers={worker.id: worker},
            resources=resources,
            resource_monitor=monitor,
        )

        scheduler_task = asyncio.create_task(scheduler.run())
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
        finally:
            scheduler.stop()
            await scheduler_task

        self.assertEqual(worker.executed, [task.id])
        self.assertEqual(monitor._run_command.await_count, 1)

    async def test_failed_command_availability_defers_dispatch(self) -> None:
        """Keep a task queued when its required command availability probe fails."""
        task = Task_agent(payload={"model": "observed"}, stream=False)
        task.resource_requirements = {"gpu": {"VRAM": 1}}
        queue = _Queue([task])
        resources = Resources([{
            "id": "gpu",
            "type": "cuda",
            "limits": {"VRAM": 10},
            "availability": {
                "command": "nvidia-smi",
                "metric": "VRAM",
            },
        }])
        monitor = ResourceMonitor(resources)
        monitor._run_command = AsyncMock(return_value="unavailable")  # type: ignore[method-assign]
        worker = _Worker(asyncio.Event())
        scheduler = Scheduler(
            queue=queue,
            workers={worker.id: worker},
            resources=resources,
            resource_monitor=monitor,
        )

        scheduler_task = asyncio.create_task(scheduler.run())
        try:
            await asyncio.sleep(0.05)
        finally:
            scheduler.stop()
            await scheduler_task

        self.assertEqual(worker.executed, [])
        self.assertIn(task, queue._queued)
        self.assertEqual(resources.snapshot()[0]["availability"]["status"], "error")


if __name__ == "__main__":
    unittest.main(verbosity=2)