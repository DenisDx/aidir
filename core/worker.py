"""
Base worker interface.
All workers must expose a module-level `worker` instance of a BaseWorker subclass.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from core import log
from core.task import Task
from core.task import STATUS_CANCELED, STATUS_COMPLETED, STATUS_FAILED


@dataclass
class WorkerResult:
    """Return value of BaseWorker.execute()."""
    ok: bool
    data: dict | None = None    # final response payload (non-streaming or summary)
    error: dict | None = None   # error dict if ok=False
    usage: dict | None = None   # token usage stats, if available


class BaseWorker:
    """
    Abstract base for all workers.
    Subclasses must set `task_type` and implement `execute()`.
    """

    id: str = ""
    task_type: str = ""
    enabled: bool = True
    is_async: bool = False
    tags: list[str] = field(default_factory=list)
    _core: Any | None = None

    async def initialize(self, config: dict) -> None:
        """Called by core at startup with the worker's config section."""
        self._core = config.get("_core")

    async def execute(
        self,
        task: Task,
        emit_chunk: Callable[[dict], Awaitable[None]] | None = None,
    ) -> WorkerResult:
        """
        Execute a task.
        For stream tasks call emit_chunk(chunk_dict) for each response chunk.
        Raise an exception or return WorkerResult(ok=False) on error.
        """
        raise NotImplementedError(f"Worker {self.id} has no execute() implementation")

    def bind_child_task(
        self,
        child_task: Task,
        parent_task: Task | None = None,
        parent_context: dict[str, Any] | None = None,
        parent_worker_id: str | None = None,
    ) -> Task:
        """Attach the current worker as callback parent and preserve the previous chain in history."""
        frame: dict[str, Any] = {
            "parent_worker": getattr(parent_task, "parent_worker", None),
            "parent_context": dict(getattr(parent_task, "parent_context", {}) or {}),
        }

        context = dict(parent_context or {})
        context["history"] = frame

        if parent_task is not None:
            context.setdefault("task_id", parent_task.id)
            context.setdefault("source_worker", self.id)

        child_task.parent_worker = parent_worker_id or self.id
        child_task.parent_context = context
        return child_task

    async def on_child_task(self, task: Task) -> None:
        """Handle a child task status change and propagate terminal callbacks upward."""
        if task.status not in {STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELED}:
            return None

        parent_worker_id = task.parent_worker
        if not parent_worker_id:
            return None

        core = self._core
        workers = getattr(core, "workers", None) if core is not None else None
        parent_worker = workers.get(parent_worker_id) if isinstance(workers, dict) else None
        if parent_worker is None:
            log("system", "critical", f"parent_worker {parent_worker_id} not found for task {task.id}")
            return None

        history = (task.parent_context or {}).get("history") if isinstance(task.parent_context, dict) else None
        if not isinstance(history, dict):
            return None

        task.parent_worker = history.get("parent_worker") or None
        task.parent_context = history.get("parent_context") or {}

        await parent_worker.on_child_task(task)
        return None


class BaseToolWorker(BaseWorker):
    """Base class for tool workers that can self-describe their MCP contract."""

    task_type: str = "tool"

    def get_tool_description(self) -> list[dict[str, Any]]:
        """
        Return a list of MCP-style tool metadata dicts.
        Each dict: name, description, inputSchema, etc.
        For single-tool workers, return a list with one dict.
        """
        return [{
            "name": self.id,
            "description": f"Tool {self.id}",
            "inputSchema": {"type": "object", "properties": {}},
        }]
