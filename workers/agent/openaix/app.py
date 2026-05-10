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
from core.context import Context
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

    async def initialize(self, config: dict) -> None:
        """Load upstream provider URL and request timeout from config."""
        core = config.get("_core")
        self._core = core
        provider_id = str(config.get("provider", "ollama_local"))
        self._timeout = int(config.get("request_timeout", 100))
        logging_cfg = config.get("logging") if isinstance(config.get("logging"), dict) else {}
        self._save_llm_request_default = bool(logging_cfg.get("save_llm_request", False))

        if core is not None:
            base_url = core.config.get(f"models.providers.{provider_id}.baseUrl")
            if base_url:
                self._base_url = str(base_url).rstrip("/")

        log(
            "worker",
            "info",
            f"openaix initialized; upstream={self._base_url}; save_llm_request_default={self._save_llm_request_default}",
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

        url = f"{self._base_url}/api/chat"
        save_call = self._resolve_save_llm_request(task.payload or {})

        # Limit message history to prevent context overload
        # Keep system + last N user/assistant messages
        payload = dict(task.payload or {})
        messages = list(payload.get("messages") or [])
        if len(messages) > 20:  # Keep last 20 messages max
            system_msgs = [m for m in messages if m.get("role") == "system"]
            other_msgs = [m for m in messages if m.get("role") != "system"]
            kept_messages = system_msgs + other_msgs[-18:]  # system + last 18
            payload["messages"] = kept_messages
            task.payload = payload
            log(
                "worker",
                "info",
                f"Task {task.id}: limited messages from {len(messages)} to {len(kept_messages)}",
                "openaix",
            )

        context_result = await self._apply_context_chain(task)
        if not context_result.ok:
            return context_result

        payload = self._normalize_payload(task.payload or {}, task.stream)

        log("worker", "debug", f"Forwarding task {task.id} to {url} stream={task.stream}", "openaix")

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                # Check if tools are present in payload (injected by context builder)
                has_tools = bool(payload.get("tools"))
                if task.stream and not has_tools:
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

    async def _apply_context_chain(self, task: Task_agent) -> WorkerResult:
        """Run context workers synchronously before model execution."""
        if self._core is None:
            return WorkerResult(ok=False, error={"code": "CORE_NOT_INITIALIZED", "message": "Core is not available"})

        if task.context is None:
            task.context = Context.empty()

        source_worker_id = task.worker_id or self.id
        worker_cfg = self._core.config.get(f"workers.items.{source_worker_id}") or {}
        worker_tools_cfg = worker_cfg.get("tools")
        task.config = dict(task.config or {})
        if isinstance(worker_tools_cfg, dict):
            task.config["tools"] = worker_tools_cfg

        context_builder_cfg = task.payload.get("context_builder") if isinstance(task.payload, dict) else None
        if isinstance(context_builder_cfg, dict) and isinstance(context_builder_cfg.get("context_add_internal_tools"), dict):
            task.config["context_add_internal_tools"] = context_builder_cfg.get("context_add_internal_tools")

        worker_chain = ["context_builder", "context_add_internal_tools", "context_render_openclaw_style"]
        original_worker_id = task.worker_id

        for worker_name in worker_chain:
            worker = self._core.workers.get(worker_name)
            if worker is None:
                continue

            task.worker_id = worker_name
            try:
                result = await worker.execute(task)
            except Exception as exc:
                task.worker_id = original_worker_id
                return WorkerResult(ok=False, error={"code": "WORKER_EXCEPTION", "message": f"{worker_name}: {exc}"})

            if not result.ok:
                task.worker_id = original_worker_id
                return result

        task.worker_id = original_worker_id
        self._apply_context_to_payload(task)
        return WorkerResult(ok=True)

    @staticmethod
    def _apply_context_to_payload(task: Task_agent) -> None:
        """Project context data into payload fields consumed by the model."""
        if task.context is None:
            return

        payload = dict(task.payload or {})
        messages = list(payload.get("messages") or [])

        if task.context.system_rendered:
            rendered = task.context.system_rendered
            system_found = False
            for message in messages:
                if isinstance(message, dict) and message.get("role") == "system":
                    message["content"] = rendered
                    system_found = True
                    break
            if not system_found:
                messages.insert(0, {"role": "system", "content": rendered})

        if task.context.tools:
            out_tools = []
            for tool_name, tool_spec in task.context.tools.items():
                if not isinstance(tool_spec, dict):
                    continue
                out_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "description": tool_spec.get("description", tool_name),
                            "parameters": tool_spec.get("inputSchema", {"type": "object"}),
                        },
                    }
                )
            if out_tools:
                payload["tools"] = out_tools

        payload["messages"] = messages
        task.payload = payload

    async def _run_with_internal_tools(
        self,
        client: httpx.AsyncClient,
        url: str,
        payload: dict,
        parent_task: Task_agent,
        emit_chunk: Callable[[dict], Awaitable[None]] | None,
        save_call: bool = False,
    ) -> WorkerResult:
        """
        Run model loop and intercept tool calls that can be executed locally.
        Tools are pre-injected into payload by context builder.
        If model requests tools that are available as workers, execute them and continue model call.
        Otherwise, return response to client as-is.
        """
        current_payload = dict(payload)
        current_payload["stream"] = False
        messages = list(current_payload.get("messages") or [])
        max_turns = 8
        turn = 0

        # Extract available tool names from payload
        available_tools = self._extract_available_tool_names(payload)

        for _ in range(max_turns):
            turn += 1
            roles = [m.get("role") for m in messages]
            log(
                "worker",
                "debug",
                f"Task {parent_task.id} tool-loop turn={turn} sending {len(messages)} messages: {roles}",
                "openaix",
            )
            current_payload["messages"] = messages
            step = await self._forward_sync(client, url, current_payload, save_call=save_call, task_id=parent_task.id)
            if not step.ok:
                return step

            data = step.data or {}
            assistant_msg = data.get("message") if isinstance(data.get("message"), dict) else {}
            calls = self._extract_tool_calls(assistant_msg)
            if not calls:
                content_preview = str(assistant_msg.get("content", ""))[:300]
                log(
                    "worker",
                    "info",
                    f"Task {parent_task.id} final response turn={turn} role={assistant_msg.get('role')} content={content_preview!r}",
                    "openaix",
                )
                if parent_task.stream and emit_chunk:
                    await emit_chunk(data)
                return step

            # Separate tool calls into executable (local workers) and pass-through (external)
            executable_calls = [c for c in calls if c["name"] in available_tools]
            external_calls = [c for c in calls if c["name"] not in available_tools]

            # If there are external tool calls, pass response to client unchanged
            if external_calls or not executable_calls:
                if parent_task.stream and emit_chunk:
                    await emit_chunk(data)
                return step

            # Continue internally: execute tool calls and append results
            log(
                "worker",
                "info",
                f"Task {parent_task.id} executing tool calls: {[c['name'] for c in executable_calls]}",
                "openaix",
            )
            messages.append(assistant_msg)
            for call in executable_calls:
                tool_result = await self._execute_internal_tool(call, parent_task)
                if not tool_result.ok:
                    return tool_result

                data_preview = json.dumps(tool_result.data or {}, ensure_ascii=False)[:500]
                log(
                    "worker",
                    "info",
                    f"Task {parent_task.id} tool result: name={call['name']} ok={tool_result.ok} data={data_preview}",
                    "openaix",
                )

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
            error={"code": "TOOL_LOOP_LIMIT", "message": "Tool loop exceeded limit"},
        )

    @staticmethod
    def _extract_available_tool_names(payload: dict) -> set[str]:
        """Extract tool names from payload.tools list."""
        tools_list = payload.get("tools")
        if not isinstance(tools_list, list):
            return set()
        
        names = set()
        for tool in tools_list:
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function")
            if isinstance(fn, dict) and "name" in fn:
                names.add(str(fn["name"]))
        return names

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
        tool_context = parent_task.context.tools if parent_task.context and parent_task.context.tools else {}
        meta = tool_context.get(tool_name)
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
