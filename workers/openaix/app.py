"""
openaix worker.
Accepts extended OpenAIx payload and forwards it to an upstream Ollama-compatible chat endpoint.
"""
from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable

import httpx

from core import log
from core.call_log import save_llm_call
from core.task import Task, STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELED
from core.task_types.task_agent import Task_agent
from core.task_types.task_tool import Task_tool
from core.worker import BaseWorker, WorkerResult


class OpenAIxWorker(BaseWorker):
    """Proxy worker for the extended OpenAIx syntax."""

    task_type = "agent"

    def __init__(self) -> None:
        self._base_url: str = "http://127.0.0.1:11434"
        self._timeout: int = 300
        self._save_llm_request_default: bool = False
        self._core = None
        self._internal_tools: dict[str, dict] = {}

    async def initialize(self, config: dict) -> None:
        """Load upstream provider URL and timeout from config."""
        core = config.get("_core")
        self._core = core
        provider_id = str(config.get("provider", "ollama_local"))
        self._timeout = int(config.get("timeoutSeconds", 300))
        logging_cfg = config.get("logging") if isinstance(config.get("logging"), dict) else {}
        self._save_llm_request_default = bool(logging_cfg.get("save_llm_request", False))

        if core is not None:
            base_url = core.config.get(f"models.providers.{provider_id}.baseUrl")
            if base_url:
                self._base_url = str(base_url).rstrip("/")

            # Internal tools configured specifically for this worker.
            self._internal_tools = self._load_internal_tools(config)

        log(
            "worker",
            "info",
            f"openaix initialized; upstream={self._base_url}; save_llm_request_default={self._save_llm_request_default}; internal_tools={list(self._internal_tools.keys())}",
            "openaix",
        )

    async def execute(
        self,
        task: Task,
        emit_chunk: Callable[[dict], Awaitable[None]] | None = None,
    ) -> WorkerResult:
        """Normalize OpenAIx payload and proxy it to upstream chat endpoint."""
        if not isinstance(task, Task_agent):
            return WorkerResult(
                ok=False,
                error={"code": "WRONG_TASK_TYPE", "message": f"Expected Task_agent, got {type(task).__name__}"},
            )

        payload = self._normalize_payload(task.payload or {}, task.stream)
        payload = self._inject_internal_tools(payload)
        url = f"{self._base_url}/api/chat"
        save_call = self._resolve_save_llm_request(task.payload or {})

        log("worker", "debug", f"Forwarding task {task.id} to {url} stream={task.stream}", "openaix")

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                # If no internal tools are configured, keep direct streaming behavior.
                if task.stream and not self._internal_tools:
                    return await self._forward_stream(client, url, payload, emit_chunk, save_call=save_call, task_id=task.id)
                return await self._run_with_internal_tools(client, url, payload, task, emit_chunk, save_call=save_call)
        except httpx.ConnectError as exc:
            log("worker", "error", f"Upstream unreachable: {exc}", "openaix")
            return WorkerResult(ok=False, error={"code": "UPSTREAM_UNREACHABLE", "message": str(exc)})
        except httpx.TimeoutException as exc:
            log("worker", "error", f"Upstream timeout: {exc}", "openaix")
            return WorkerResult(ok=False, error={"code": "UPSTREAM_TIMEOUT", "message": str(exc)})
        except Exception as exc:
            log("worker", "error", f"Unexpected error: {exc}", "openaix")
            return WorkerResult(ok=False, error={"code": "EXCEPTION", "message": str(exc)})

    def _resolve_save_llm_request(self, request_payload: dict) -> bool:
        """Resolve save_llm_request flag with per-request value overriding worker default."""
        log_field = request_payload.get("log") if isinstance(request_payload, dict) else None
        if isinstance(log_field, dict):
            options = log_field.get("options")
            if isinstance(options, dict) and "save_llm_request" in options:
                return bool(options.get("save_llm_request"))
        return self._save_llm_request_default

    @staticmethod
    def _normalize_payload(payload: dict, stream: bool) -> dict:
        """Convert extended OpenAIx payload into upstream Ollama-compatible chat payload."""
        out = {
            "model": payload.get("model", ""),
            "messages": payload.get("messages", []),
            "stream": bool(stream),
        }

        passthrough = [
            "worker",
            "tools",
            "tool_choice",
            "temperature",
            "top_p",
            "max_tokens",
            "stop",
            "response_format",
            "seed",
        ]
        for key in passthrough:
            if key in payload:
                out[key] = payload[key]

        options = payload.get("options")
        if isinstance(options, dict):
            out["options"] = dict(options)

        return out

    def _load_internal_tools(self, worker_cfg: dict) -> dict[str, dict]:
        """Read worker-local internal tool mapping from config.
        Supported format:
        tools: {
          "search": {"worker": "web_search", "description": "...", "inputSchema": {...}},
          "fetch":  {"worker": "web_fetch"}
        }
        """
        tools_cfg = worker_cfg.get("tools") or {}
        if not isinstance(tools_cfg, dict):
            return {}

        loaded: dict[str, dict] = {}
        for tool_name, meta in tools_cfg.items():
            if not isinstance(meta, dict):
                continue
            worker_id = str(meta.get("worker", "")).strip()
            if not worker_id:
                continue

            wk = self._core.workers.get(worker_id) if self._core is not None else None
            if wk is None or getattr(wk, "task_type", "") != "tool":
                log("worker", "warn", f"openaix internal tool '{tool_name}' skipped: worker '{worker_id}' is not tool", "openaix")
                continue

            loaded[str(tool_name)] = {
                "worker": worker_id,
                "description": str(meta.get("description", f"Internal tool {tool_name}")),
                "inputSchema": meta.get("inputSchema") or {"type": "object"},
            }
        return loaded

    def _inject_internal_tools(self, payload: dict) -> dict:
        """Add configured internal tools to payload.tools if they are missing."""
        if not self._internal_tools:
            return payload

        out = dict(payload)
        existing = out.get("tools")
        if not isinstance(existing, list):
            existing = []

        existing_names: set[str] = set()
        for tool in existing:
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function") if isinstance(tool.get("function"), dict) else {}
            name = fn.get("name") or tool.get("name")
            if name:
                existing_names.add(str(name))

        merged = list(existing)
        for name, meta in self._internal_tools.items():
            if name in existing_names:
                continue
            merged.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": meta["description"],
                        "parameters": meta["inputSchema"],
                    },
                }
            )

        out["tools"] = merged
        return out

    async def _run_with_internal_tools(
        self,
        client: httpx.AsyncClient,
        url: str,
        payload: dict,
        parent_task: Task_agent,
        emit_chunk: Callable[[dict], Awaitable[None]] | None,
        save_call: bool = False,
    ) -> WorkerResult:
        """Run model loop and intercept internal tool calls.
        If model requests only internal tools, execute them via Task_tool and continue model call.
        External tools are returned to client as-is.
        """
        current_payload = dict(payload)
        current_payload["stream"] = False
        messages = list(current_payload.get("messages") or [])
        max_turns = 8

        for _ in range(max_turns):
            current_payload["messages"] = messages
            step = await self._forward_sync(client, url, current_payload, save_call=save_call, task_id=parent_task.id)
            if not step.ok:
                return step

            data = step.data or {}
            assistant_msg = data.get("message") if isinstance(data.get("message"), dict) else {}
            calls = self._extract_tool_calls(assistant_msg)
            if not calls:
                if parent_task.stream and emit_chunk:
                    await emit_chunk(data)
                return step

            internal_calls = [c for c in calls if c["name"] in self._internal_tools]
            external_calls = [c for c in calls if c["name"] not in self._internal_tools]

            # If there is any external tool call, pass response to client unchanged.
            if external_calls or not internal_calls:
                if parent_task.stream and emit_chunk:
                    await emit_chunk(data)
                return step

            # Continue internally: append assistant tool call message, then tool results.
            log(
                "worker",
                "info",
                f"Task {parent_task.id} intercepted internal tool calls: {[c['name'] for c in internal_calls]}",
                "openaix",
            )
            messages.append(assistant_msg)
            for call in internal_calls:
                tool_result = await self._execute_internal_tool(call, parent_task)
                if not tool_result.ok:
                    return tool_result

                tool_message = {
                    "role": "tool",
                    "tool_name": call["name"],
                    "content": json.dumps(tool_result.data or {}, ensure_ascii=False),
                }
                if call.get("id"):
                    tool_message["tool_call_id"] = call["id"]
                messages.append(tool_message)

        return WorkerResult(
            ok=False,
            error={"code": "TOOL_LOOP_LIMIT", "message": "Internal tool loop exceeded limit"},
        )

    @staticmethod
    def _extract_tool_calls(message: dict) -> list[dict]:
        """Normalize tool call list from assistant message."""
        out: list[dict] = []
        raw_calls = message.get("tool_calls") if isinstance(message, dict) else None
        if not isinstance(raw_calls, list):
            return out

        for call in raw_calls:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = fn.get("name") or call.get("name")
            if not name:
                continue

            args = fn.get("arguments") or call.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            if not isinstance(args, dict):
                args = {}

            out.append(
                {
                    "id": call.get("id") or "",
                    "name": str(name),
                    "arguments": args,
                }
            )
        return out

    async def _execute_internal_tool(self, call: dict, parent_task: Task_agent) -> WorkerResult:
        """Run an internal tool via Task_tool and wait for completion."""
        tool_name = call["name"]
        meta = self._internal_tools.get(tool_name)
        if meta is None:
            return WorkerResult(ok=False, error={"code": "TOOL_NOT_FOUND", "message": f"Unknown internal tool: {tool_name}"})

        task = Task_tool(payload={"tool": tool_name, "arguments": call.get("arguments") or {}})
        task.worker_id = meta["worker"]
        task.queue_timeout = int(parent_task.queue_timeout or 300)
        task.run_timeout = int(parent_task.run_timeout or 300)
        log("worker", "info", f"Task {parent_task.id} -> internal tool {tool_name} via worker {task.worker_id}", "openaix")

        try:
            await self._core.on_task_added(task)
        except Exception as exc:
            return WorkerResult(ok=False, error={"code": "TOOL_ENQUEUE_FAILED", "message": str(exc)})

        timeout = task.queue_timeout + task.run_timeout
        try:
            await asyncio.wait_for(task._done_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            await self._core.queue.mark_canceled(task)
            await self._core.delete_task(task.id)
            return WorkerResult(ok=False, error={"code": "TOOL_TIMEOUT", "message": f"Internal tool timed out: {tool_name}"})

        await self._core.delete_task(task.id)

        if task.status == STATUS_COMPLETED:
            return WorkerResult(ok=True, data=task.result or {})
        if task.status == STATUS_FAILED:
            return WorkerResult(ok=False, error=task.error or {"code": "TOOL_FAILED", "message": f"Internal tool failed: {tool_name}"})
        if task.status == STATUS_CANCELED:
            return WorkerResult(ok=False, error={"code": "TOOL_CANCELED", "message": f"Internal tool canceled: {tool_name}"})

        return WorkerResult(ok=False, error={"code": "TOOL_UNKNOWN_STATE", "message": f"Internal tool unknown state: {task.status}"})

    async def _forward_sync(self, client: httpx.AsyncClient, url: str, payload: dict, *, save_call: bool = False, task_id: str = "") -> WorkerResult:
        """Send a non-streaming request and return the full JSON body."""
        upstream_payload = {**payload, "stream": False}
        resp = await client.post(url, json=upstream_payload)
        if resp.status_code != 200:
            return WorkerResult(
                ok=False,
                error={
                    "code": "UPSTREAM_ERROR",
                    "message": f"Upstream returned HTTP {resp.status_code}",
                    "body": resp.text[:512],
                },
            )
        data = resp.json()
        if save_call:
            save_llm_call(self.id, task_id, upstream_payload, data)
        return WorkerResult(ok=True, data=data, usage=data.get("usage"))

    async def _forward_stream(
        self,
        client: httpx.AsyncClient,
        url: str,
        payload: dict,
        emit_chunk: Callable[[dict], Awaitable[None]] | None,
        *,
        save_call: bool = False,
        task_id: str = "",
    ) -> WorkerResult:
        """Forward streaming chunks to the endpoint emitter."""
        upstream_payload = {**payload, "stream": True}
        final_data: dict | None = None
        chunks: list[dict] = [] if save_call else []
        async with client.stream("POST", url, json=upstream_payload) as resp:
            if resp.status_code != 200:
                return WorkerResult(
                    ok=False,
                    error={
                        "code": "UPSTREAM_ERROR",
                        "message": f"Upstream returned HTTP {resp.status_code}",
                    },
                )

            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if emit_chunk:
                    await emit_chunk(chunk)
                if save_call:
                    chunks.append(chunk)
                if chunk.get("done"):
                    final_data = chunk

        if save_call:
            save_llm_call(self.id, task_id, upstream_payload, {"stream_chunks": chunks})
        return WorkerResult(ok=True, data=final_data, usage=(final_data or {}).get("usage"))


worker = OpenAIxWorker()
