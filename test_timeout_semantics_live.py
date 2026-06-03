"""Runnable live-check for queue, run, and per-call timeout semantics."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from core.scheduler import Scheduler
from core.task_types.task_agent import Task_agent
from core.worker import WorkerResult
from workers.agent.openaix.app import OpenAIxWorker


class _RecordingQueue:
    """Minimal queue double capturing terminal state transitions."""

    def __init__(self) -> None:
        self.failed: list[tuple[str, dict]] = []
        self.completed: list[str] = []
        self.canceled: list[str] = []
        self.running: list[tuple[str, str]] = []

    async def mark_running(self, task_id: str, worker_id: str) -> None:
        self.running.append((task_id, worker_id))

    async def mark_completed(self, task) -> None:
        self.completed.append(task.id)

    async def mark_failed(self, task, error: dict) -> None:
        self.failed.append((task.id, dict(error)))

    async def mark_canceled(self, task) -> None:
        self.canceled.append(task.id)


class _SleepWorker:
    """Worker double that sleeps for a configurable duration."""

    def __init__(self, sleep_seconds: float, worker_id: str = "fake-worker") -> None:
        self.id = worker_id
        self.task_type = "agent"
        self.enabled = True
        self._sleep_seconds = sleep_seconds

    async def execute(self, task, emit_chunk=None):
        await asyncio.sleep(self._sleep_seconds)
        return WorkerResult(ok=True, data={"ok": True})


async def _check_queue_timeout_before_first_run() -> None:
    queue = _RecordingQueue()
    scheduler = Scheduler(queue=queue, workers={})
    task = Task_agent(payload={}, stream=False)
    task.queue_timeout = 5
    task.created_at = datetime.now(timezone.utc) - timedelta(seconds=6)

    expired = await scheduler._expire_queued_task_if_needed(task)
    assert expired, "queued task should expire before first execution"
    assert queue.failed and queue.failed[0][1]["code"] == "QUEUE_TIMEOUT", "expected QUEUE_TIMEOUT"


async def _check_started_task_ignores_queue_timeout() -> None:
    scheduler = Scheduler(queue=_RecordingQueue(), workers={})
    task = Task_agent(payload={}, stream=False)
    task.queue_timeout = 5
    task.created_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    task.started_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    assert not scheduler._queue_timeout_expired(task), "started task must not be killed by queue timeout"


async def _check_run_timeout_after_start() -> None:
    queue = _RecordingQueue()
    worker = _SleepWorker(0.2)
    scheduler = Scheduler(queue=queue, workers={worker.id: worker})
    task = Task_agent(payload={}, stream=False)
    task.run_timeout = 0.05

    await scheduler._run_task(task, worker)
    assert queue.failed and queue.failed[0][1]["code"] == "TIMEOUT", "expected run TIMEOUT"


def _check_one_llm_call_uses_request_timeout() -> None:
    worker = OpenAIxWorker()
    worker._timeout = 17
    task = Task_agent(payload={}, stream=False)
    task.run_timeout = 999
    assert worker._resolve_upstream_timeout(task) == 17, "single upstream call must use worker request_timeout"


async def main() -> None:
    await _check_queue_timeout_before_first_run()
    print("PASS queue timeout before first run")

    await _check_started_task_ignores_queue_timeout()
    print("PASS started task ignores queue timeout")

    await _check_run_timeout_after_start()
    print("PASS run timeout after start")

    _check_one_llm_call_uses_request_timeout()
    print("PASS one upstream call uses REQUEST_TIMEOUT")


if __name__ == "__main__":
    asyncio.run(main())