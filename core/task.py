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
from typing import Any, Optional

from core.context import Context

# ── Status constants ─────────────────────────────────────────────────────────
STATUS_CREATED   = "created"
STATUS_QUEUED    = "queued"
STATUS_RUNNING   = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED    = "failed"
STATUS_CANCELED  = "canceled"

# ── Priority constants ───────────────────────────────────────────────────────
PRIORITY_URGENT = 0
PRIORITY_NORMAL = 5
PRIORITY_IDLE   = 10


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
    updated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: Any = None
    error: dict | None = None
    llm_call_count: int = 0
    llm_call_history: list[dict[str, Any]] = field(default_factory=list)

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

    # ── Viewer summary (persisted without decoding full request bodies) ─────
    request_preview: str = ""
    route_provider_id: str = ""
    model_id: str = ""
    envid: str = ""

    # ── Origin ────────────────────────────────────────────────────────────
    # True for tasks created by endpoints (from external clients).
    # External tasks are preserved in Redis after completion and cleaned by cron.
    external: bool = False

    # ── Parent callback chain ─────────────────────────────────────────────
    # Worker id to notify when this task changes status.
    parent_worker: str | None = None
    # JSON-serializable payload passed through the callback chain unchanged.
    parent_context: dict[str, Any] = field(default_factory=dict)

    # ── Context and config ────────────────────────────────────────────────
    # Runtime context for this task (merged from envid context + overrides)
    context: Optional[Context] = None
    # Worker config overrides for this task (merged with worker's base config)
    config: dict[str, Any] = field(default_factory=dict)

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
        context_json = ""
        if self.context:
            context_json = json.dumps(self.context.to_dict())
        
        return {
            "id":          self.id,
            "type":        self.type,
            "status":      self.status,
            "updated_at":  self.updated_at.isoformat(),
            "priority":    str(self.priority),
            "worker_id":   self.worker_id or "",
            "created_at":  self.created_at.isoformat(),
            "started_at":  self.started_at.isoformat() if self.started_at else "",
            "finished_at": self.finished_at.isoformat() if self.finished_at else "",
            "payload":     json.dumps(self.payload),
            "result":      json.dumps(self.result) if self.result is not None else "",
            "error":       json.dumps(self.error) if self.error else "",
            "llm_call_count": str(int(self.llm_call_count or 0)),
            "llm_call_history": json.dumps(self.llm_call_history or []),
            "external":    "1" if self.external else "0",
            "parent_worker": self.parent_worker or "",
            "parent_context": json.dumps(self.parent_context) if self.parent_context else "",
            "retry_count": str(self.retry_count),
            "retry_period": str(self.retry_period),
            "retry_attempt": str(self.retry_attempt),
            "fallback_index": str(self.fallback_index),
            "queue_timeout": str(self.queue_timeout),
            "run_timeout": str(self.run_timeout),
            "resource_requirements": json.dumps(self.resource_requirements),
            "config":      json.dumps(self.config),
            "context":     context_json,
            "stream":      "1" if getattr(self, "stream", False) else "0",
            "request_preview": self.request_preview or self._request_preview(),
            "route_provider_id": self.route_provider_id or str(self.config.get("provider_id") or ""),
            "model_id": self.model_id or str(self.payload.get("model") or ""),
            "envid": self.envid or (self.context.envid if self.context else ""),
        }

    def _request_preview(self) -> str:
        """Build a bounded body-free request description for task search."""
        messages = self.payload.get("messages") if isinstance(self.payload, dict) else None
        message_count = len(messages) if isinstance(messages, list) else 0
        return f"type={self.type} model={self.payload.get('model', '')} messages={message_count}"

    @classmethod
    def from_redis_hash(cls, data: dict[str, str]) -> "Task":
        """Reconstruct a task subtype from its persisted Redis hash."""
        from core.task_types.task_agent import Task_agent
        from core.task_types.task_tool import Task_tool

        task_classes = {
            "agent": Task_agent,
            "tool": Task_tool,
        }
        task_type = str(data.get("type") or "")
        task_class = task_classes.get(task_type)
        if task_class is None:
            raise ValueError(f"unsupported task type: {task_type!r}")

        def parse_json(name: str, default):
            raw = data.get(name) or ""
            if not raw:
                return default
            value = json.loads(raw)
            return value if isinstance(value, type(default)) else default

        def parse_datetime(name: str) -> datetime | None:
            raw = data.get(name) or ""
            return datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None

        context_data = parse_json("context", {})
        task = task_class(
            id=str(data["id"]),
            payload=parse_json("payload", {}),
            worker_id=data.get("worker_id") or None,
            priority=int(data.get("priority") or PRIORITY_NORMAL),
            status=str(data.get("status") or STATUS_CREATED),
            created_at=parse_datetime("created_at") or datetime.now(timezone.utc),
            updated_at=parse_datetime("updated_at") or datetime.now(timezone.utc),
            started_at=parse_datetime("started_at"),
            finished_at=parse_datetime("finished_at"),
            result=parse_json("result", None),
            error=parse_json("error", None),
            llm_call_count=int(data.get("llm_call_count") or 0),
            llm_call_history=parse_json("llm_call_history", []),
            queue_timeout=int(data.get("queue_timeout") or 0),
            run_timeout=int(data.get("run_timeout") or 0),
            retry_count=int(data.get("retry_count") or 0),
            retry_period=int(data.get("retry_period") or 0),
            retry_attempt=int(data.get("retry_attempt") or 0),
            fallback_index=int(data.get("fallback_index") or 0),
            resource_requirements=parse_json("resource_requirements", {}),
            external=str(data.get("external") or "0").lower() in {"1", "true"},
            parent_worker=data.get("parent_worker") or None,
            parent_context=parse_json("parent_context", {}),
            config=parse_json("config", {}),
            context=Context.from_dict(context_data) if context_data else None,
            request_preview=data.get("request_preview") or "",
            route_provider_id=data.get("route_provider_id") or "",
            model_id=data.get("model_id") or "",
            envid=data.get("envid") or "",
        )
        if hasattr(task, "stream"):
            task.stream = str(data.get("stream") or "0").lower() in {"1", "true"}
        return task
