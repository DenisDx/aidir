"""
Base worker interface.
All workers must expose a module-level `worker` instance of a BaseWorker subclass.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from core.task import Task


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
    tags: list[str] = field(default_factory=list)

    async def initialize(self, config: dict) -> None:
        """Called by core at startup with the worker's config section."""

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
