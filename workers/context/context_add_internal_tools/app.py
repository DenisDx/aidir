"""
context_add_internal_tools worker.
Adds internal tools to the context.tools dict according to worker config.
Internal tools are always written (replace existing ones).

Input sources (in priority order):
    1. task.config['context_add_internal_tools']['tools'] — per-task request override
    2. task.config['tools'] — per-task tools config prepared by openaix
    3. self._tools — worker default config
    4. legacy payload fallbacks for compatibility
"""
from __future__ import annotations

from typing import Awaitable, Callable

from core import log
from core.config_merger import update_config
from core.context import Context
from core.task import Task
from core.worker import BaseWorker, BaseToolWorker, WorkerResult


class ContextAddInternalToolsWorker(BaseWorker):
    """
    Worker that adds internal tools to task.context.tools.
    
    Config:
      tools: Dict[str, Dict[str, Any]]  — tools to add to context.
    
        Input (task.config):
            tools: Dict[str, Dict] from source worker config (e.g., openaix)
            context_add_internal_tools: Dict with 'tools' key to override config tools
    
    Input: task.context must be initialized.
    Output: Updated task.context.tools with internal tools.
    """

    id = "context_add_internal_tools"
    task_type = "context"

    def __init__(self) -> None:
        super().__init__()
        self._tools: dict = {}
        self._core = None

    async def initialize(self, config: dict) -> None:
        """Store tools config."""
        self._tools = config.get("tools", {})
        self._core = config.get("_core")

    async def execute(
        self,
        task: Task,
        emit_chunk: Callable[[dict], Awaitable[None]] | None = None,
    ) -> WorkerResult:
        """Add internal tools to task.context.tools."""

        # Ensure context exists
        if task.context is None:
            task.context = Context.empty()

        # Build effective tools config (priority order)
        effective_tools = dict(self._tools)

        task_config = task.config or {}
        payload = task.payload or {}

        # Apply worker source tools prepared by openaix.
        worker_tools = task_config.get("tools")
        if isinstance(worker_tools, dict):
            effective_tools = update_config(effective_tools, worker_tools)

        # Apply per-task override from request.
        ctx_override = task_config.get("context_add_internal_tools")
        if isinstance(ctx_override, dict) and isinstance(ctx_override.get("tools"), dict):
            effective_tools = update_config(effective_tools, ctx_override["tools"])

        # Legacy payload fallbacks for older callers.
        if not isinstance(worker_tools, dict):
            legacy_worker_tools = payload.get("worker_tools_config")
            if isinstance(legacy_worker_tools, dict):
                effective_tools = update_config(effective_tools, legacy_worker_tools)

        if not (isinstance(ctx_override, dict) and isinstance(ctx_override.get("tools"), dict)):
            legacy_ctx_override = payload.get("context_add_internal_tools")
            if isinstance(legacy_ctx_override, dict) and isinstance(legacy_ctx_override.get("tools"), dict):
                effective_tools = update_config(effective_tools, legacy_ctx_override["tools"])

        # Auto-include discovered tools from loaded tool workers.
        # Config-defined tools keep priority; discovered ones fill the gaps.
        if self._core and isinstance(getattr(self._core, "workers", None), dict):
            for worker_id, worker in self._core.workers.items():
                if not isinstance(worker, BaseToolWorker):
                    continue

                descs = worker.get_tool_description()
                if isinstance(descs, dict):
                    descs = [descs]
                if not isinstance(descs, list):
                    continue

                for desc in descs:
                    if not isinstance(desc, dict):
                        continue
                    tool_name = desc.get("name")
                    if not isinstance(tool_name, str) or not tool_name:
                        continue
                    effective_tools.setdefault(
                        tool_name,
                        {
                            "worker": worker_id,
                            "description": desc.get("description", tool_name),
                            "inputSchema": desc.get("inputSchema", {"type": "object", "properties": {}}),
                        },
                    )

        resolved_tools = self._resolve_tools_map(effective_tools)

        before_names = set(task.context.tools.keys())

        # Internal tools always overwrite existing ones
        task.context.tools.update(resolved_tools)

        injected_names = sorted(resolved_tools.keys())
        replaced_names = sorted(name for name in injected_names if name in before_names)
        added_names = sorted(name for name in injected_names if name not in before_names)

        log(
            "worker",
            "info",
            (
                f"tools_injection source=context_add_internal_tools task_id={task.id} "
                f"configured={len(resolved_tools)} injected={len(injected_names)} "
                f"added={len(added_names)} replaced={len(replaced_names)} "
                f"injected_names={injected_names}"
            ),
            "context_add_internal_tools",
        )
        
        return WorkerResult(
            ok=True,
            data={"tools_count": len(resolved_tools)},
        )

    def _resolve_tools_map(self, tools_cfg: dict) -> dict:
        """Build context.tools map using metadata from tool workers."""
        resolved: dict = {}
        for tool_name, raw_meta in tools_cfg.items():
            meta = raw_meta if isinstance(raw_meta, dict) else {}
            worker_id = meta.get("worker") if isinstance(meta, dict) else None
            if not worker_id:
                continue

            worker = self._core.workers.get(str(worker_id)) if self._core else None
            tool_descs = []
            if isinstance(worker, BaseToolWorker):
                descs = worker.get_tool_description()
                if isinstance(descs, list):
                    matched_descs = [desc for desc in descs if isinstance(desc, dict) and desc.get("name") == tool_name]
                    tool_descs.extend(matched_descs or descs)
                elif isinstance(descs, dict):
                    tool_descs.append(descs)
            else:
                # Fallback: synthesize minimal spec
                tool_descs.append({
                    "name": tool_name,
                    "description": str(meta.get("description", tool_name)),
                    "inputSchema": meta.get("inputSchema", {"type": "object", "properties": {}}),
                    "worker": str(worker_id),
                })

            for desc in tool_descs:
                # Explicit per-task overrides still win over worker defaults.
                if isinstance(meta.get("description"), str):
                    desc["description"] = meta["description"]
                if isinstance(meta.get("inputSchema"), dict):
                    desc["inputSchema"] = meta["inputSchema"]
                desc["worker"] = str(worker_id)
                resolved[desc["name"]] = desc

        return resolved


# Export worker instance
worker = ContextAddInternalToolsWorker()
