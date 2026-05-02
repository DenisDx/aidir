"""
Base Task class and task status constants.
Each task type is a subclass defined in core/task_types/.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ── Status constants ─────────────────────────────────────────────────────────
STATUS_CREATED   = "created"
STATUS_QUEUED    = "queued"
STATUS_RUNNING   = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED    = "failed"
STATUS_CANCELED  = "canceled"

# ── Priority constants ───────────────────────────────────────────────────────
PRIORITY_URGENT = 5
PRIORITY_NORMAL = 10
PRIORITY_IDLE   = 20


@dataclass
class Task:
    """
    Base task object. Represents one unit of work for a worker.
    All persistent fields are serialized to Redis via to_redis_hash().
    """

    # ── Identity ──────────────────────────────────────────────────────────
    type: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    # ── Routing ───────────────────────────────────────────────────────────
    # Preferred worker id; if None, scheduler selects by task type
    worker_id: str | None = None
    payload: dict = field(default_factory=dict)
    priority: int = PRIORITY_NORMAL

    # ── Lifecycle ─────────────────────────────────────────────────────────
    status: str = STATUS_CREATED
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: Any = None
    error: dict | None = None

    # ── Timeouts (seconds; 0 = no limit) ─────────────────────────────────
    queue_timeout: int = 300
    run_timeout: int = 300

    # ── Retry/fallback policy (spec: "Реакция на отказ") ────────────────
    retry_count: int = 0
    retry_period: int = 0
    retry_attempt: int = 0
    next_retry_at: float = 0.0
    fallbacks: list[str] = field(default_factory=list)
    fallback_index: int = 0
    on_reject: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ── Resource requirements ─────────────────────────────────────────────
    # Map: resource_id -> {metric: amount}
    resource_requirements: dict[str, dict[str, int]] = field(default_factory=dict)

    # ── Origin ────────────────────────────────────────────────────────────
    # True for tasks created by endpoints (from external clients).
    # External tasks are preserved in Redis after completion and cleaned by cron.
    external: bool = False

    # ── Async primitives (not persisted) ──────────────────────────────────
    # Signaled by QueueManager when task reaches a terminal status
    _done_event: asyncio.Event = field(
        default_factory=asyncio.Event, repr=False, compare=False
    )
    # Worker pushes chunks here; sentinel None marks end of stream
    _chunk_queue: asyncio.Queue = field(
        default_factory=asyncio.Queue, repr=False, compare=False
    )

    def to_redis_hash(self) -> dict[str, str]:
        """Serialize task state for Redis HSET (all values must be strings)."""
        return {
            "id":          self.id,
            "type":        self.type,
            "status":      self.status,
            "priority":    str(self.priority),
            "worker_id":   self.worker_id or "",
            "created_at":  self.created_at.isoformat(),
            "started_at":  self.started_at.isoformat() if self.started_at else "",
            "finished_at": self.finished_at.isoformat() if self.finished_at else "",
            "payload":     json.dumps(self.payload),
            "result":      json.dumps(self.result) if self.result is not None else "",
            "error":       json.dumps(self.error) if self.error else "",
            "external":    "1" if self.external else "0",
            "retry_count": str(self.retry_count),
            "retry_period": str(self.retry_period),
            "retry_attempt": str(self.retry_attempt),
            "fallback_index": str(self.fallback_index),
            "resource_requirements": json.dumps(self.resource_requirements),
        }
