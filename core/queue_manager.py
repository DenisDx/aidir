"""
Redis-backed priority task queue.
Uses ZSET for priority ordering and HASH for task state persistence.
All ZSET scores are task.priority (lower = higher priority per spec: 0=max, 100=lowest).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

import redis.asyncio as aioredis

from core import log
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
        audit_log=None,
    ) -> None:
        self._redis = redis_client
        self._ns = instance                        # key namespace
        self._tasks: dict[str, Task] = {}          # task_id -> Task
        self._status_change_callback = status_change_callback
        self._audit_log = audit_log

    _QUEUE_TASK_TYPES = ("agent", "tool")

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

    async def recover_startup_tasks(self) -> dict[str, int]:
        """Restore queued tasks and terminalize abandoned running tasks after restart."""
        recovered = 0
        quarantined = 0
        restarted = 0

        for task_type in self._QUEUE_TASK_TYPES:
            task_ids = await self._redis.zrange(self._q(task_type), 0, -1)
            for raw_task_id in task_ids:
                task_id = raw_task_id.decode() if isinstance(raw_task_id, bytes) else str(raw_task_id)
                raw = await self._redis.hgetall(self._tk(task_id))
                try:
                    task = Task.from_redis_hash(raw)
                    if task.status != STATUS_QUEUED or task.type != task_type:
                        raise ValueError("queue member does not match a queued task hash")
                except Exception as exc:
                    await self._redis.zrem(self._q(task_type), task_id)
                    await self._redis.hset(
                        f"{self._ns}:queue:quarantine",
                        task_id,
                        json.dumps({"task_type": task_type, "reason": str(exc)}),
                    )
                    quarantined += 1
                    log("system", "warning", f"Quarantined persisted queue entry {task_id}: {exc}")
                    continue
                self._tasks[task.id] = task
                recovered += 1

        async for raw_key in self._redis.scan_iter(match=f"{self._ns}:task:*", count=200):
            key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
            raw = await self._redis.hgetall(key)
            if raw.get("status") != STATUS_RUNNING:
                continue
            task_id = str(raw.get("id") or key.rsplit(":", 1)[-1])
            now = datetime.now(timezone.utc).isoformat()
            error = {"code": "SERVICE_RESTARTED", "message": "Task interrupted by service restart"}
            pipe = self._redis.pipeline(transaction=True)
            pipe.hset(self._tk(task_id), mapping={
                "status": STATUS_FAILED,
                "updated_at": now,
                "finished_at": now,
                "error": json.dumps(error),
            })
            for task_type in self._QUEUE_TASK_TYPES:
                pipe.zrem(self._q(task_type), task_id)
            await pipe.execute()
            restarted += 1

        return {"recovered": recovered, "quarantined": quarantined, "restarted": restarted}

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
        self._record_terminal_audit(task)
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
            "error_code":  str(error.get("code") or ""),
        })
        await task._chunk_queue.put(None)   # stream sentinel
        task._done_event.set()
        self._record_terminal_audit(task)
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
        self._record_terminal_audit(task)
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

    async def extend_active_timeout(self, task_id: str, seconds: int = 60) -> Optional[Task]:
        """Extend both lifetime deadlines of a queued or running live task."""
        task = self._tasks.get(task_id)
        if task is None or task.status not in {STATUS_QUEUED, STATUS_RUNNING}:
            return None

        extension = int(seconds)
        task.queue_timeout = max(0, int(task.queue_timeout or 0)) + extension
        task.run_timeout = max(0, int(task.run_timeout or 0)) + extension
        await self._redis.hset(
            self._tk(task.id),
            mapping={
                "queue_timeout": str(task.queue_timeout),
                "run_timeout": str(task.run_timeout),
            },
        )
        return task

    async def increment_llm_call_count(self, task: Task) -> int:
        """Increment persisted LLM call count for a task and mirror it in memory."""
        next_value = int(getattr(task, "llm_call_count", 0) or 0) + 1
        task.llm_call_count = next_value

        hincrby = getattr(self._redis, "hincrby", None)
        if callable(hincrby):
            persisted = await hincrby(self._tk(task.id), "llm_call_count", 1)
            try:
                task.llm_call_count = int(persisted)
            except Exception:
                task.llm_call_count = next_value
            return task.llm_call_count

        await self._redis.hset(self._tk(task.id), mapping={"llm_call_count": str(task.llm_call_count)})
        return task.llm_call_count

    async def persist_llm_call_diagnostics(self, task: Task) -> None:
        """Persist bounded LLM summaries without raw request, response, or stream bodies."""
        allowed_fields = {
            "call_index", "started_at", "finished_at", "duration_ms", "worker_id",
            "provider_id", "request_kind", "url_path", "model", "stream",
            "message_count", "last_role", "has_tools", "input_count", "request_text",
            "request_preview", "status", "http_status", "error_code",
            "response_summary", "audit_request_event_id", "audit_response_event_id",
        }
        history = [
            {key: value for key, value in entry.items() if key in allowed_fields}
            for entry in (getattr(task, "llm_call_history", []) or [])
            if isinstance(entry, dict)
        ]
        serialized_history = await asyncio.to_thread(
            json.dumps,
            history,
        )
        await self._redis.hset(
            self._tk(task.id),
            mapping={
                "llm_call_count": str(int(getattr(task, "llm_call_count", 0) or 0)),
                "llm_call_history": serialized_history,
            },
        )

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

    def _record_terminal_audit(self, task: Task) -> None:
        """Write one compact terminal audit record without affecting task completion."""
        if self._audit_log is None:
            return
        try:
            route = task.config.get("route") if isinstance(task.config, dict) else {}
            route = route if isinstance(route, dict) else {}
            events = self._audit_log.confirmed_task_events(task.id)
            references: dict[str, list[str]] = {
                "client_request": [],
                "client_response": [],
                "llm_requests": [],
                "llm_responses": [],
            }
            reference_groups = {
                "client_request": "client_request",
                "client_response": "client_response",
                "llm_request": "llm_requests",
                "llm_response": "llm_responses",
            }
            for event in events:
                group = reference_groups.get(event["type"])
                if group is not None:
                    references[group].append(event["event_id"])
            self._audit_log.record_task_terminal(
                task_id=task.id,
                status=task.status,
                task={
                    "type": task.type, "priority": task.priority, "worker_id": task.worker_id,
                    "created_at": task.created_at.isoformat(),
                    "started_at": task.started_at.isoformat() if task.started_at else None,
                    "finished_at": task.finished_at.isoformat() if task.finished_at else None,
                    "queue_timeout": task.queue_timeout, "run_timeout": task.run_timeout,
                    "route": route, "error": task.error,
                    "result_summary": {"llm_call_count": task.llm_call_count},
                },
                raw_event_refs=references,
            )
        except Exception:
            return

    def record_client_response_reconciliation(self, task: Task, event_id: str) -> None:
        """Append a late client-response reference after terminal stream delivery."""
        if self._audit_log is None or not event_id:
            return
        try:
            self._audit_log.record_task_reconciliation(
                task_id=task.id,
                raw_event_refs={"client_response": [event_id]},
            )
        except Exception:
            return
