"""
Base worker interface.
All workers must expose a module-level `worker` instance of a BaseWorker subclass.
"""
from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

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
    _MAX_LLM_CALL_HISTORY: int = 20

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

    async def _register_llm_call(self, task: Task) -> int:
        """Increment task-local LLM call counter and persist it when queue access is available."""
        core = self._core
        queue = getattr(core, "queue", None) if core is not None else None
        increment = getattr(queue, "increment_llm_call_count", None)
        if callable(increment):
            try:
                return await increment(task)
            except Exception as exc:
                log("worker", "warning", f"Failed to persist llm_call_count for task {task.id}: {exc}", self.id or "worker")

        task.llm_call_count = int(getattr(task, "llm_call_count", 0) or 0) + 1
        return task.llm_call_count

    async def _persist_llm_call_diagnostics(self, task: Task) -> None:
        """Persist task LLM diagnostics when queue access is available."""
        core = self._core
        queue = getattr(core, "queue", None) if core is not None else None
        persist = getattr(queue, "persist_llm_call_diagnostics", None)
        if callable(persist):
            try:
                await persist(task)
            except Exception as exc:
                log("worker", "warning", f"Failed to persist llm_call_history for task {task.id}: {exc}", self.id or "worker")

    async def _begin_llm_call(
        self,
        task: Task,
        *,
        url: str,
        payload: dict | None = None,
        provider_id: str = "",
        save_call: bool = False,
    ) -> dict[str, Any]:
        """Append one compact LLM-call summary entry to the task and persist it."""
        request_payload = payload if isinstance(payload, dict) else {}
        messages = request_payload.get("messages") if isinstance(request_payload.get("messages"), list) else []
        input_value = request_payload.get("input")
        input_count = 0
        if isinstance(input_value, str):
            input_count = 1
        elif isinstance(input_value, list):
            input_count = len(input_value)

        started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        task.llm_call_count = int(getattr(task, "llm_call_count", 0) or 0) + 1

        parsed_url = urlparse(url)
        request_kind = ""
        if isinstance(getattr(task, "config", None), dict):
            request_kind = str(task.config.get("request_kind") or "").strip()

        entry: dict[str, Any] = {
            "call_index": task.llm_call_count,
            "started_at": started_at,
            "worker_id": self.id,
            "provider_id": provider_id,
            "request_kind": request_kind or "chat",
            "url_path": parsed_url.path or url,
            "model": str(request_payload.get("model") or ""),
            "stream": bool(request_payload.get("stream")),
            "message_count": len(messages),
            "last_role": str(messages[-1].get("role") or "") if messages and isinstance(messages[-1], dict) else "",
            "has_tools": bool(request_payload.get("tools")),
            "input_count": input_count,
            "save_llm_request": bool(save_call),
            "status": "started",
        }

        history = list(getattr(task, "llm_call_history", []) or [])
        history.append(entry)
        if len(history) > self._MAX_LLM_CALL_HISTORY:
            history = history[-self._MAX_LLM_CALL_HISTORY :]
        task.llm_call_history = history
        entry = task.llm_call_history[-1]
        await self._persist_llm_call_diagnostics(task)
        return entry

    async def _finalize_llm_call(
        self,
        task: Task,
        entry: dict[str, Any] | None,
        *,
        status: str,
        http_status: int | None = None,
        error_code: str = "",
        response: dict | None = None,
    ) -> None:
        """Finalize one compact LLM-call summary entry and persist it."""
        if not isinstance(entry, dict):
            return

        entry["status"] = status
        finished_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        entry["finished_at"] = finished_at

        started_at_raw = entry.get("started_at")
        try:
            started_at = datetime.fromisoformat(str(started_at_raw))
            finished_dt = datetime.fromisoformat(finished_at)
            entry["duration_ms"] = max(0, int((finished_dt - started_at).total_seconds() * 1000))
        except Exception:
            entry["duration_ms"] = 0

        if http_status is not None:
            entry["http_status"] = int(http_status)
        if error_code:
            entry["error_code"] = error_code

        summary = self._summarize_llm_response(response)
        if summary:
            entry["response_summary"] = summary

        await self._persist_llm_call_diagnostics(task)

    async def _finalize_latest_started_llm_call(
        self,
        task: Task,
        *,
        status: str,
        error_code: str = "",
    ) -> None:
        """Finalize the most recent started LLM-call entry when an outer exception interrupts execution."""
        history = list(getattr(task, "llm_call_history", []) or [])
        for entry in reversed(history):
            if isinstance(entry, dict) and entry.get("status") == "started":
                await self._finalize_llm_call(task, entry, status=status, error_code=error_code)
                return

    @staticmethod
    def _summarize_llm_response(response: dict | None) -> dict[str, Any]:
        """Build a compact response summary safe to persist inside task metadata."""
        if not isinstance(response, dict):
            return {}

        summary: dict[str, Any] = {}
        for key in ("done_reason", "total_duration", "load_duration", "prompt_eval_count", "eval_count"):
            value = response.get(key)
            if value is not None:
                summary[key] = value

        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = response.get(key)
            if value is None:
                value = usage.get(key)
            if value is not None:
                summary[key] = value

        message = response.get("message") if isinstance(response.get("message"), dict) else {}
        tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
        if tool_calls:
            summary["tool_call_count"] = len(tool_calls)

        embeddings = response.get("embeddings") if isinstance(response.get("embeddings"), list) else []
        if embeddings:
            summary["embeddings_count"] = len(embeddings)

        stream_chunks = response.get("stream_chunks") if isinstance(response.get("stream_chunks"), list) else []
        if stream_chunks:
            summary["stream_chunk_count"] = len(stream_chunks)

        return summary


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
