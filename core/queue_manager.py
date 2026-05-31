"""
Redis-backed priority task queue.
Uses ZSET for priority ordering and HASH for task state persistence.
All ZSET scores are task.priority (lower = higher priority per spec: 0=max, 100=lowest).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

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

    def __init__(
        self,
        redis_client: aioredis.Redis,
        instance: str = "aidir",
        status_change_callback: Callable[[Task], Awaitable[None]] | None = None,
    ) -> None:
        self._redis = redis_client
        self._ns = instance                        # key namespace
        self._tasks: dict[str, Task] = {}          # task_id -> Task
        self._status_change_callback = status_change_callback

    _QUEUE_TASK_TYPES = ("agent", "request", "tool", "context_builder")

    # ── Key helpers ──────────────────────────────────────────────────────────

    def _q(self, task_type: str) -> str:
        return f"{self._ns}:queue:{task_type}"

    def _tk(self, task_id: str) -> str:
        return f"{self._ns}:task:{task_id}"

    # ── Public API ───────────────────────────────────────────────────────────

    async def add_task(self, task: Task) -> None:
        """Enqueue task atomically: ZADD to queue + HSET state. status→queued."""
        task.status = STATUS_QUEUED
        task.updated_at = datetime.now(timezone.utc)
        pipe = self._redis.pipeline(transaction=True)
        pipe.zadd(self._q(task.type), {task.id: task.priority})
        pipe.hset(self._tk(task.id), mapping=task.to_redis_hash())
        await pipe.execute()
        self._tasks[task.id] = task
        await self._notify_status_change(task)

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
            task.updated_at = now
            task.started_at = now
            task.worker_id = worker_id
        await self._redis.hset(self._tk(task_id), mapping={
            "status":     STATUS_RUNNING,
            "updated_at": now.isoformat(),
            "started_at": now.isoformat(),
            "worker_id":  worker_id,
        })
        if task:
            await self._notify_status_change(task)

    async def mark_completed(self, task: Task) -> None:
        """Finalize task as completed; signal endpoint and push stream sentinel."""
        now = datetime.now(timezone.utc)
        task.status = STATUS_COMPLETED
        task.updated_at = now
        task.finished_at = now
        await self._redis.hset(self._tk(task.id), mapping={
            "status":      STATUS_COMPLETED,
            "updated_at":  now.isoformat(),
            "finished_at": task.finished_at.isoformat(),
            "result":      json.dumps(task.result) if task.result is not None else "",
        })
        await task._chunk_queue.put(None)   # stream sentinel
        task._done_event.set()
        await self._notify_status_change(task)

    async def mark_failed(self, task: Task, error: dict) -> None:
        """Finalize task as failed; signal endpoint."""
        now = datetime.now(timezone.utc)
        task.status = STATUS_FAILED
        task.updated_at = now
        task.finished_at = now
        task.error = error
        await self._redis.hset(self._tk(task.id), mapping={
            "status":      STATUS_FAILED,
            "updated_at":  now.isoformat(),
            "finished_at": task.finished_at.isoformat(),
            "error":       json.dumps(error),
        })
        await task._chunk_queue.put(None)   # stream sentinel
        task._done_event.set()
        await self._notify_status_change(task)

    async def mark_canceled(self, task: Task) -> None:
        """Finalize task as canceled; signal endpoint."""
        now = datetime.now(timezone.utc)
        task.status = STATUS_CANCELED
        task.updated_at = now
        task.finished_at = now
        await self._redis.hset(self._tk(task.id), mapping={
            "status":      STATUS_CANCELED,
            "updated_at":  now.isoformat(),
            "finished_at": task.finished_at.isoformat(),
        })
        await task._chunk_queue.put(None)   # stream sentinel
        task._done_event.set()
        await self._notify_status_change(task)

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
        for task_type in self._QUEUE_TASK_TYPES:
            pipe.zrem(self._q(task_type), task_id)
        await pipe.execute()

    def get_task(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[Task]:
        return list(self._tasks.values())

    async def get_resource_queue_state(
        self,
        requirements: dict[str, dict[str, int]] | None = None,
        priority: int = 5,
    ) -> dict:
        """Summarize queued tasks that match a resource requirement set."""
        target = self._normalize_requirements(requirements)
        counts_by_priority: dict[int, int] = {}
        total_count = 0
        below_priority_count = 0

        for task_type in self._QUEUE_TASK_TYPES:
            queued = await self._redis.zrange(self._q(task_type), 0, -1, withscores=True)
            if not queued:
                continue

            for raw_task_id, raw_score in queued:
                task_id = raw_task_id.decode() if isinstance(raw_task_id, bytes) else str(raw_task_id)
                data = await self._redis.hgetall(self._tk(task_id))
                if not data or data.get("status") != STATUS_QUEUED:
                    continue

                task_requirements = self._parse_requirements(data.get("resource_requirements"))
                if self._normalize_requirements(task_requirements) != target:
                    continue

                priority_value = self._parse_int(data.get("priority"), default=int(raw_score))
                total_count += 1
                counts_by_priority[priority_value] = counts_by_priority.get(priority_value, 0) + 1
                if priority_value > priority:
                    below_priority_count += 1

        return {
            "queued_count_total": total_count,
            "queued_count_below_priority": below_priority_count,
            "priority_counts": [
                {"priority": prio, "count": counts_by_priority[prio]}
                for prio in sorted(counts_by_priority)
            ],
        }

    @staticmethod
    def _parse_int(value, default: int = 0) -> int:
        """Parse an integer value with a fallback default."""
        try:
            return int(value)
        except Exception:
            return int(default)

    @staticmethod
    def _parse_requirements(raw: str | None) -> dict[str, dict[str, int]]:
        """Parse serialized resource requirements from Redis hash storage."""
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        if not isinstance(parsed, dict):
            return {}

        out: dict[str, dict[str, int]] = {}
        for resource_id, metrics in parsed.items():
            if not isinstance(metrics, dict):
                continue
            normalized_metrics: dict[str, int] = {}
            for metric_name, amount in metrics.items():
                try:
                    normalized_metrics[str(metric_name)] = int(amount)
                except Exception:
                    continue
            if normalized_metrics:
                out[str(resource_id)] = normalized_metrics
        return out

    @staticmethod
    def _normalize_requirements(requirements: dict[str, dict[str, int]] | None) -> str:
        """Serialize requirements into a stable comparison key."""
        normalized: dict[str, dict[str, int]] = {}
        for resource_id, metrics in (requirements or {}).items():
            if not isinstance(metrics, dict):
                continue
            normalized[str(resource_id)] = {}
            for metric_name, amount in metrics.items():
                try:
                    normalized[str(resource_id)][str(metric_name)] = int(amount)
                except Exception:
                    continue
        return json.dumps(normalized, sort_keys=True, separators=(",", ":"))

    async def _notify_status_change(self, task: Task) -> None:
        """Notify the configured callback after a task status transition."""
        if self._status_change_callback is None:
            return
        try:
            await self._status_change_callback(task)
        except Exception:
            # Status updates must stay best-effort; callback failures are logged upstream.
            return
