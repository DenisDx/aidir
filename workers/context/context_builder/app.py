"""
context_builder worker.
Loads context from Redis (if envid specified) and merges it with task context.
Merges saved envid context with task-specific context overrides (top-level only).
"""
from __future__ import annotations

from typing import Awaitable, Callable

from core.config_merger import merge_contexts_top_level
from core.context import Context
from core.task import Task
from core.worker import BaseWorker, WorkerResult


class ContextBuilderWorker(BaseWorker):
    """
    Worker that loads and merges context for a task.
    
    Input (task.payload):
      - envid: str | None  — environment ID to load saved context from
    
    Input (task.context):
      - Can be None (will create empty) or pre-populated with overrides
    
    Output:
      - Updated task.context with merged saved + task context
    """

    id = "context_builder"
    task_type = "context"

    def __init__(self) -> None:
        super().__init__()
        self._core = None

    async def initialize(self, config: dict) -> None:
        """Store core reference."""
        self._core = config.get("_core")

    async def execute(
        self,
        task: Task,
        emit_chunk: Callable[[dict], Awaitable[None]] | None = None,
    ) -> WorkerResult:
        """Load and merge context."""
        
        payload = task.payload or {}
        envid_id: str | None = payload.get("envid")
        
        # Ensure context exists
        if task.context is None:
            task.context = Context.empty()
        
        # Load saved context from Redis (if envid known)
        if envid_id and self._core and self._core.envid_registry:
            try:
                saved_ctx_dict = await self._core.envid_registry.load_context(envid_id)
                if saved_ctx_dict:
                    saved_ctx = Context.from_dict(saved_ctx_dict)
                    # Merge: saved is base, task context overrides top-level
                    merged_dict = merge_contexts_top_level(
                        saved_ctx.to_dict(),
                        task.context.to_dict()
                    )
                    task.context = Context.from_dict(merged_dict)
                
                # Mark envid as used
                await self._core.envid_registry.mark_used(envid_id)
            except Exception as e:
                # Log but don't fail - context will be empty or task-provided
                pass
        
        # Set envid if not already set
        if task.context.envid is None:
            task.context.envid = envid_id
        
        return WorkerResult(
            ok=True,
            data={"envid": envid_id, "context_keys": list(task.context.to_dict().keys())},
        )


# Export worker instance
worker = ContextBuilderWorker()
