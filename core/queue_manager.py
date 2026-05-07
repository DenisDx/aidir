"""
Redis-backed priority task queue.
Uses ZSET for priority ordering and HASH for task state persistence.
All ZSET scores are task.priority (lower = higher priority per spec: 0=max, 100=lowest).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import redis.asyncio as aioredis

from core.task import (
    Task,
    STATUS_QUEUED, STATUS_RUNNING,
    STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELED,
)

if TYPE_CHECKING:
    pass


class QueueManager:
    """
    Manages task lifecycle in Redis + in-memory Task registry.
    In-memory registry holds live Task objects for event signaling.
    """

    def __init__(self, redis_client: aioredis.Redis, instance: str = "aidir") -> None:
        self._redis = redis_client
        self._ns = instance                        # key namespace
        self._tasks: dict[str, Task] = {}          # task_id -> Task

    # ── Key helpers ──────────────────────────────────────────────────────────

    def _q(self, task_type: str) -> str:
        return f"{self._ns}:queue:{task_type}"

    def _tk(self, task_id: str) -> str:
        return f"{self._ns}:task:{task_id}"

    # ── Public API ───────────────────────────────────────────────────────────

    async def add_task(self, task: Task) -> None:
        """Enqueue task atomically: ZADD to queue + HSET state. status→queued."""
        task.status = STATUS_QUEUED
        pipe = self._redis.pipeline(transaction=True)
        pipe.zadd(self._q(task.type), {task.id: task.priority})
        pipe.hset(self._tk(task.id), mapping=task.to_redis_hash())
        await pipe.execute()
        self._tasks[task.id] = task

    async def pop_next(self, task_type: str) -> Optional[str]:
        """Pop the highest-priority (lowest score) task id from the queue."""
        result = await self._redis.zpopmin(self._q(task_type), count=1)
        if not result:
            return None
        task_id = result[0][0]
        return task_id.decode() if isinstance(task_id, bytes) else task_id

    async def mark_running(self, task_id: str, worker_id: str) -> None:
        """Transition task to running state."""
        now = datetime.now(timezone.utc)
        task = self._tasks.get(task_id)
        if task:
            task.status = STATUS_RUNNING
            task.started_at = now
            task.worker_id = worker_id
        await self._redis.hset(self._tk(task_id), mapping={
            "status":     STATUS_RUNNING,
            "started_at": now.isoformat(),
            "worker_id":  worker_id,
        })

    async def mark_completed(self, task: Task) -> None:
        """Finalize task as completed; signal endpoint and push stream sentinel."""
        task.status = STATUS_COMPLETED
        task.finished_at = datetime.now(timezone.utc)
        await self._redis.hset(self._tk(task.id), mapping={
            "status":      STATUS_COMPLETED,
            "finished_at": task.finished_at.isoformat(),
            "result":      json.dumps(task.result) if task.result is not None else "",
        })
        await task._chunk_queue.put(None)   # stream sentinel
        task._done_event.set()

    async def mark_failed(self, task: Task, error: dict) -> None:
        """Finalize task as failed; signal endpoint."""
        task.status = STATUS_FAILED
        task.finished_at = datetime.now(timezone.utc)
        task.error = error
        await self._redis.hset(self._tk(task.id), mapping={
            "status":      STATUS_FAILED,
            "finished_at": task.finished_at.isoformat(),
            "error":       json.dumps(error),
        })
        await task._chunk_queue.put(None)   # stream sentinel
        task._done_event.set()

    async def mark_canceled(self, task: Task) -> None:
        """Finalize task as canceled; signal endpoint."""
        task.status = STATUS_CANCELED
        task.finished_at = datetime.now(timezone.utc)
        await self._redis.hset(self._tk(task.id), mapping={
            "status":      STATUS_CANCELED,
            "finished_at": task.finished_at.isoformat(),
        })
        await task._chunk_queue.put(None)   # stream sentinel
        task._done_event.set()

    async def delete_task(self, task_id: str) -> None:
        """Remove task from memory and ZSET queue.
        External tasks (created by endpoints) keep their Redis HASH for cron cleanup;
        internal tasks are fully deleted from Redis immediately.
        """
        task = self._tasks.pop(task_id, None)
        is_external = task.external if task is not None else False
        pipe = self._redis.pipeline()
        if not is_external:
            pipe.delete(self._tk(task_id))
        # Always remove from queue ZSET (task is no longer waiting)
        for task_type in ("agent", "request", "tool", "context_builder"):
            pipe.zrem(self._q(task_type), task_id)
        await pipe.execute()

    def get_task(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[Task]:
        return list(self._tasks.values())
