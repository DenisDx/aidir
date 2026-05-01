"""
Task scheduler.
Background asyncio loop that pops tasks from the queue and dispatches them
to the appropriate worker. Runs concurrently with uvicorn servers.
TODO: add resource availability checks before dispatching (VRAM etc.).
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from core.task import Task, STATUS_QUEUED
from core.worker import WorkerResult
from core import log

if TYPE_CHECKING:
    from core.queue_manager import QueueManager
    from core.worker import BaseWorker

_SUPPORTED_TYPES = ("agent",)  # TODO: extend with request, tool, tts, stt…


class Scheduler:
    """
    Polls the priority queue and runs tasks via workers.
    Each task is executed as an independent asyncio task for concurrency.
    """

    def __init__(
        self,
        queue: "QueueManager",
        workers: dict[str, "BaseWorker"],
    ) -> None:
        self._queue = queue
        self._workers = workers
        self._running = False
        self._wake = asyncio.Event()

    def notify_new_task(self) -> None:
        """Wake the scheduler loop when a new task is enqueued."""
        self._wake.set()

    async def run(self) -> None:
        """Main scheduler loop. Runs until stop() is called."""
        self._running = True
        log("system", "info", "Scheduler started")

        while self._running:
            dispatched = False

            for task_type in _SUPPORTED_TYPES:
                task_id = await self._queue.pop_next(task_type)
                if task_id is None:
                    continue

                task = self._queue.get_task(task_id)
                if task is None:
                    log("system", "warn", f"Task {task_id} popped but not in memory")
                    continue

                worker = self._select_worker(task)
                if worker is None:
                    # No compatible worker – re-enqueue and skip
                    await self._queue.add_task(task)
                    log("system", "warn",
                        f"No worker for task {task_id} (type={task_type}), re-queued")
                    continue

                asyncio.create_task(self._run_task(task, worker))
                dispatched = True

            if not dispatched:
                # Sleep until a new task arrives or polling interval elapses
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()

    def stop(self) -> None:
        self._running = False
        self._wake.set()

    # ── Internal ─────────────────────────────────────────────────────────────

    def _select_worker(self, task: Task) -> "BaseWorker | None":
        """
        Select a worker for the task.
        Explicit worker_id in task takes priority; otherwise first match by type.
        TODO: check resource availability (VRAM etc.) before selecting.
        """
        if task.worker_id and task.worker_id in self._workers:
            w = self._workers[task.worker_id]
            if w.enabled:
                return w

        for w in self._workers.values():
            if w.task_type == task.type and w.enabled:
                return w

        return None

    async def _run_task(self, task: Task, worker: "BaseWorker") -> None:
        """Execute one task; handle timeouts and exceptions."""
        log("worker", "info", f"Starting task {task.id}", worker.id)
        await self._queue.mark_running(task.id, worker.id)

        try:
            result: WorkerResult = await asyncio.wait_for(
                worker.execute(task, emit_chunk=self._make_emitter(task)),
                timeout=task.run_timeout or None,
            )
            if result.ok:
                task.result = result.data
                await self._queue.mark_completed(task)
                log("worker", "info", f"Task {task.id} completed", worker.id)
            else:
                await self._queue.mark_failed(
                    task, result.error or {"code": "WORKER_ERROR", "message": "Worker returned error"}
                )
                log("worker", "warn", f"Task {task.id} failed: {result.error}", worker.id)

        except asyncio.TimeoutError:
            log("worker", "error", f"Task {task.id} timed out", worker.id)
            await self._queue.mark_failed(
                task, {"code": "TIMEOUT", "message": "Task run timeout exceeded"}
            )

        except Exception as exc:
            log("worker", "error", f"Task {task.id} exception: {exc}", worker.id)
            await self._queue.mark_failed(
                task, {"code": "EXCEPTION", "message": str(exc)}
            )

    @staticmethod
    def _make_emitter(task: Task):
        """Return async callable that puts a chunk into task's queue."""
        async def emit_chunk(chunk: dict) -> None:
            await task._chunk_queue.put(chunk)
        return emit_chunk
