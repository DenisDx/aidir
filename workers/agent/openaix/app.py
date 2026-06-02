"""
openaix worker.
Accepts extended OpenAIx payload and forwards it to an upstream Ollama-compatible chat endpoint.
"""
from __future__ import annotations

import ast
import asyncio
import base64
import codecs
import json
from typing import Awaitable, Callable

import httpx

from core import log
from core.call_log import save_llm_call, save_llm_raw_call
from core.context import Context
from core.task import Task, STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELED
from core.task_types.task_agent import Task_agent
from core.task_types.task_tool import Task_tool
from core.worker import BaseWorker, WorkerResult


class OpenAIxWorker(BaseWorker):
    """Proxy worker for the extended OpenAIx syntax."""

    task_type = "agent"
    _GENERATION_OPTION_FIELDS = {
        "temperature": "temperature",
        "top_p": "top_p",
        "repeat_penalty": "repeat_penalty",
        "repetition_penalty": "repeat_penalty",
        "repeat_last_n": "repeat_last_n",
        "num_predict": "num_predict",
        "max_tokens": "num_predict",
        "seed": "seed",
        "presence_penalty": "presence_penalty",
        "frequency_penalty": "frequency_penalty",
        "top_k": "top_k",
        "min_p": "min_p",
    }

    def __init__(self) -> None:
        self._base_url: str = "http://127.0.0.1:11434"
        self._timeout: int = 300
        self._save_llm_request_default: bool = False
        self._tools_max_turns: int = 50
        self._provider_id: str = "ollama_local"
        self._generation_defaults: dict[str, object] = {}
        self._core = None

    async def initialize(self, config: dict) -> None:
        """Load upstream provider URL and request timeout from config."""
        core = config.get("_core")
        self._core = core
        provider_id = str(config.get("provider", "ollama_local"))
        self._provider_id = provider_id
        self._timeout = int(config.get("request_timeout", 100))
        logging_cfg = config.get("logging") if isinstance(config.get("logging"), dict) else {}
        self._save_llm_request_default = bool(logging_cfg.get("save_llm_request", False))

        global_tools_max_turns = 50
        if core is not None:
            raw_global_tools_max_turns = core.config.get("workers.tools_max_turns", 50)
            try:
                global_tools_max_turns = max(1, int(raw_global_tools_max_turns))
            except (TypeError, ValueError):
                global_tools_max_turns = 50

        raw_tools_max_turns = config.get("tools_max_turns", global_tools_max_turns)
        try:
            self._tools_max_turns = max(1, int(raw_tools_max_turns))
        except (TypeError, ValueError):
            self._tools_max_turns = global_tools_max_turns

        raw_generation_defaults = config.get("generation_defaults")
        self._generation_defaults = dict(raw_generation_defaults) if isinstance(raw_generation_defaults, dict) else {}

        if core is not None:
            base_url = core.config.get(f"models.providers.{provider_id}.baseUrl")
            if base_url:
                self._base_url = str(base_url).rstrip("/")

        log(
            "worker",
            "info",
            (
                f"openaix initialized; upstream={self._base_url}; "
                f"save_llm_request_default={self._save_llm_request_default}; "
                f"tools_max_turns={self._tools_max_turns}"
            ),
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

        payload = self._apply_generation_defaults(dict(task.payload or {}))
        task.payload = payload
        provider_id = self._resolve_task_provider_id(task)
        base_url = self._resolve_base_url(provider_id)

        log(
            "worker",
            "info",
            f"Task {task.id}: received request payload={json.dumps(task.payload, ensure_ascii=False, default=str)}",
            "openaix",
        )

        url = f"{base_url}/api/chat"
        save_call = self._resolve_save_llm_request(task.payload or {})
        request_headers = self._resolve_request_headers(task, provider_id)

        # Limit message history to prevent context overload
        # Keep system + last N user/assistant messages
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

        payload_with_model_defaults = self._apply_model_generation_defaults(dict(task.payload or {}), provider_id=provider_id)
        payload_with_ctx = self._apply_model_context_window(payload_with_model_defaults, provider_id=provider_id)
        payload = self._normalize_payload(payload_with_ctx, task.stream)
        upstream_timeout = self._resolve_upstream_timeout(task)

        log(
            "worker",
            "debug",
            f"Forwarding task {task.id} to {url} provider={provider_id} stream={task.stream}",
            "openaix",
        )

        try:
            async with httpx.AsyncClient(timeout=upstream_timeout, headers=request_headers) as client:
                # Check if tools are present in payload (injected by context builder)
                has_tools = bool(payload.get("tools"))
                if task.stream and not has_tools:
                    return await self._forward_stream(client, url, payload, emit_chunk, save_call=save_call, task_id=task.id)
                return await self._run_with_internal_tools(client, url, payload, task, emit_chunk, save_call=save_call)
        except httpx.ConnectError as exc:
            log("worker", "warning", f"Upstream unreachable: {exc}", "openaix")
            return WorkerResult(ok=False, error={"code": "UPSTREAM_UNREACHABLE", "message": str(exc)})
        except httpx.TimeoutException as exc:
            timeout_message = self._build_timeout_message(exc)
            log("worker", "warning", f"Upstream timeout: {timeout_message}", "openaix")
            return WorkerResult(ok=False, error={"code": "UPSTREAM_TIMEOUT", "message": timeout_message})
        except Exception as exc:
            log("worker", "error", f"Unexpected error: {exc}", "openaix")
            return WorkerResult(ok=False, error={"code": "EXCEPTION", "message": str(exc)})

    def _resolve_upstream_timeout(self, task: Task) -> int:
        """Use task timeout for upstream request when available to avoid premature failures."""
        try:
            configured_timeout = int(self._timeout)
        except (TypeError, ValueError):
            configured_timeout = 100
        configured_timeout = max(1, configured_timeout)

        task_timeout = getattr(task, "run_timeout", 0)
        try:
            task_timeout_int = int(task_timeout)
        except (TypeError, ValueError):
            task_timeout_int = 0

        if task_timeout_int > 0:
            return task_timeout_int
        return configured_timeout

    @staticmethod
    def _build_timeout_message(exc: httpx.TimeoutException) -> str:
        """Build a non-empty timeout message for diagnostics."""
        detail = str(exc).strip()
        if detail:
            return detail

        request = getattr(exc, "request", None)
        if request is not None:
            method = getattr(request, "method", "") or ""
            url = getattr(request, "url", None)
            if method and url is not None:
                return f"{exc.__class__.__name__}: {method} {url}"

        return exc.__class__.__name__

    def _resolve_save_llm_request(self, request_payload: dict) -> bool:
        """Resolve save_llm_request flag with per-request value overriding worker default."""
        log_field = request_payload.get("log") if isinstance(request_payload, dict) else None
        if isinstance(log_field, dict):
            options = log_field.get("options")
            if isinstance(options, dict) and "save_llm_request" in options:
                return bool(options.get("save_llm_request"))
        return self._save_llm_request_default

    def _apply_generation_defaults(self, payload: dict) -> dict:
        """Apply worker-configured generation defaults to payload when request omits them."""
        if not isinstance(payload, dict) or not self._generation_defaults:
            return payload

        out = dict(payload)
        options = out.get("options") if isinstance(out.get("options"), dict) else {}
        for field_name, option_name in self._GENERATION_OPTION_FIELDS.items():
            if out.get(field_name) is not None:
                continue
            if options.get(option_name) is not None:
                continue

            default_value = self._generation_defaults.get(field_name)
            if default_value is None and option_name != field_name:
                default_value = self._generation_defaults.get(option_name)
            if default_value is not None:
                out[field_name] = default_value
        return out

    @classmethod
    def _normalize_payload(cls, payload: dict, stream: bool) -> dict:
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
            "stop",
            "think",
            "response_format",
        ]
        for key in passthrough:
            if key in payload:
                out[key] = payload[key]

        options = payload.get("options")
        out_options = dict(options) if isinstance(options, dict) else {}
        for field_name, option_name in cls._GENERATION_OPTION_FIELDS.items():
            field_value = payload.get(field_name)
            if field_value is None or out_options.get(option_name) is not None:
                continue
            out_options[option_name] = field_value

        if out_options:
            out["options"] = out_options

        return out

    def _apply_model_generation_defaults(self, payload: dict, provider_id: str | None = None) -> dict:
        """Apply per-model generation defaults unless request already overrides them."""
        if not isinstance(payload, dict):
            return payload

        model_name = payload.get("model")
        if not isinstance(model_name, str) or not model_name.strip():
            return payload

        model_cfg = self._resolve_model_cfg(model_name, provider_id=provider_id)
        if not isinstance(model_cfg, dict):
            return payload

        out = dict(payload)
        options = out.get("options") if isinstance(out.get("options"), dict) else {}
        applied_options: set[str] = set()
        for field_name, option_name in self._GENERATION_OPTION_FIELDS.items():
            if option_name in applied_options:
                continue
            if self._payload_has_generation_value(out, option_name) or options.get(option_name) is not None:
                applied_options.add(option_name)
                continue

            default_value = model_cfg.get(field_name)
            target_field = field_name
            if default_value is None and option_name != field_name:
                default_value = model_cfg.get(option_name)
                target_field = option_name
            if default_value is not None:
                out[target_field] = default_value
                applied_options.add(option_name)
        return out

    @classmethod
    def _payload_has_generation_value(cls, payload: dict, option_name: str) -> bool:
        """Return whether payload already defines any top-level alias for one Ollama option name."""
        if not isinstance(payload, dict):
            return False
        for field_name, mapped_option in cls._GENERATION_OPTION_FIELDS.items():
            if mapped_option != option_name:
                continue
            if payload.get(field_name) is not None:
                return True
        return False

    def _apply_model_context_window(self, payload: dict, provider_id: str | None = None) -> dict:
        """Set options.num_ctx from model config contextWindow when not explicitly provided."""
        if not isinstance(payload, dict):
            return payload

        model_name = payload.get("model")
        if not isinstance(model_name, str) or not model_name.strip():
            return payload

        options = payload.get("options")
        if not isinstance(options, dict):
            options = {}

        # Explicit request value always wins.
        if options.get("num_ctx") is not None:
            return payload

        cfg_window = self._resolve_model_context_window(model_name, provider_id=provider_id)
        if cfg_window is None:
            return payload

        options["num_ctx"] = cfg_window
        payload["options"] = options
        return payload

    def _resolve_model_context_window(self, model_name: str, provider_id: str | None = None) -> int | None:
        """Return configured contextWindow for model from effective provider, if available."""
        model_cfg = self._resolve_model_cfg(model_name, provider_id=provider_id)
        if not isinstance(model_cfg, dict):
            return None

        raw_ctx = model_cfg.get("contextWindow")
        if raw_ctx is None:
            return None
        try:
            parsed = int(raw_ctx)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    def _resolve_model_cfg(self, model_name: str, provider_id: str | None = None) -> dict | None:
        """Return configured model dictionary for the effective provider, if available."""
        if self._core is None:
            return None

        effective_provider_id = str(provider_id or self._provider_id or "").strip()
        provider_models = self._core.config.get(f"models.providers.{effective_provider_id}.models") or []
        if not isinstance(provider_models, list):
            return None

        for model_cfg in provider_models:
            if not isinstance(model_cfg, dict):
                continue
            cfg_id = model_cfg.get("id")
            cfg_name = model_cfg.get("name")
            if model_name not in {cfg_id, cfg_name}:
                continue

            return model_cfg

        return None

    def _resolve_task_provider_id(self, task: Task) -> str:
        """Return effective provider id for a task using route metadata when present."""
        route_cfg = (task.config or {}).get("route") if isinstance(task.config, dict) else None
        if isinstance(route_cfg, dict):
            provider_id = str(route_cfg.get("resolved_provider") or "").strip()
            if provider_id:
                return provider_id
        return self._provider_id

    def _resolve_base_url(self, provider_id: str) -> str:
        """Resolve provider base URL for the effective task provider."""
        if self._core is None:
            return self._base_url
        base_url = self._core.config.get(f"models.providers.{provider_id}.baseUrl")
        if not base_url:
            return self._base_url
        return str(base_url).rstrip("/")

    def _resolve_request_headers(self, task: Task, provider_id: str) -> dict[str, str]:
        """Resolve auth headers with provider override, incoming bearer fallback, or no auth."""
        provider_auth = None
        if self._core is not None:
            provider_auth = self._core.config.get(f"models.providers.{provider_id}.auth")
        headers = self._build_auth_headers(provider_auth if isinstance(provider_auth, dict) else {})
        if not headers:
            task_cfg = task.config if isinstance(task.config, dict) else {}
            incoming_bearer_token = task_cfg.get("incoming_bearer_token")
            if isinstance(incoming_bearer_token, str) and incoming_bearer_token.strip():
                headers["Authorization"] = f"Bearer {incoming_bearer_token.strip()}"

        if self._provider_api(provider_id) == "openaix":
            headers.update(self._build_route_trace_headers(task))
        return headers

    def _provider_api(self, provider_id: str) -> str:
        """Return provider api type for the effective upstream provider."""
        if self._core is None:
            return ""
        value = self._core.config.get(f"models.providers.{provider_id}.api")
        return str(value or "").strip()

    def _build_route_trace_headers(self, task: Task) -> dict[str, str]:
        """Build outgoing inter-aidir route-trace headers for remote OpenAIx hops."""
        task_cfg = task.config if isinstance(task.config, dict) else {}
        route_trace = task_cfg.get("route_trace") if isinstance(task_cfg.get("route_trace"), dict) else {}
        route_id = str(route_trace.get("route_id") or "").strip()
        visited_instances = route_trace.get("visited_instances") if isinstance(route_trace.get("visited_instances"), list) else []
        outgoing_visited = [str(item).strip() for item in visited_instances if isinstance(item, str) and item.strip()]

        current_instance = ""
        if self._core is not None:
            current_instance = str(self._core.config.get("instance", "aidir") or "aidir").strip() or "aidir"
        if current_instance and current_instance not in outgoing_visited:
            outgoing_visited.append(current_instance)

        headers: dict[str, str] = {}
        if route_id:
            headers["X-Aidir-Route-Id"] = route_id
        if outgoing_visited:
            headers["X-Aidir-Visited-Instances"] = ", ".join(outgoing_visited)
        return headers

    @staticmethod
    def _build_auth_headers(auth_cfg: dict) -> dict[str, str]:
        """Build HTTP headers from provider auth config."""
        if not isinstance(auth_cfg, dict):
            return {}

        headers: dict[str, str] = {}
        raw_headers = auth_cfg.get("headers")
        if isinstance(raw_headers, dict):
            for key, value in raw_headers.items():
                if isinstance(key, str) and isinstance(value, str):
                    headers[key] = value

        raw_authorization = auth_cfg.get("authorization")
        if isinstance(raw_authorization, str) and raw_authorization.strip():
            headers["Authorization"] = raw_authorization.strip()

        auth_type = str(auth_cfg.get("type", "bearer")).strip().lower()
        token = auth_cfg.get("token")
        if isinstance(token, str) and token.strip() and auth_type in {"bearer", "token", ""}:
            headers["Authorization"] = f"Bearer {token.strip()}"

        if auth_type == "basic":
            username = auth_cfg.get("username")
            password = auth_cfg.get("password")
            if isinstance(username, str) and isinstance(password, str):
                raw = f"{username}:{password}".encode("utf-8")
                headers["Authorization"] = f"Basic {base64.b64encode(raw).decode('ascii')}"

        return headers

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

        request_payload = task.payload if isinstance(task.payload, dict) else {}
        request_has_tools_field = "tools" in request_payload
        request_tools_value = request_payload.get("tools") if request_has_tools_field else None
        requested_internal_tool_names = self._extract_requested_internal_tool_names(request_tools_value)
        disable_internal_tools_injection = request_has_tools_field and (
            request_tools_value is None
            or (isinstance(request_tools_value, list) and len(request_tools_value) == 0)
        )

        if disable_internal_tools_injection:
            log(
                "worker",
                "info",
                f"Task {task.id}: explicit tools={request_tools_value!r}; internal tools injection disabled",
                "openaix",
            )

        worker_chain = ["context_builder", "context_add_internal_tools", "context_render_openclaw_style"]
        original_worker_id = task.worker_id

        for worker_name in worker_chain:
            if worker_name == "context_add_internal_tools" and disable_internal_tools_injection:
                continue

            if worker_name == "context_render_openclaw_style" and task.context is not None and request_has_tools_field:
                # When caller controls the tools list, advertise only explicitly requested internal tools.
                if disable_internal_tools_injection:
                    task.context.tools = {}
                else:
                    task.context.tools = {
                        name: spec
                        for name, spec in task.context.tools.items()
                        if name in requested_internal_tool_names
                    }

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
        task.config = dict(task.config or {})
        messages = list(payload.get("messages") or [])
        request_has_tools_field = "tools" in payload
        injected_tool_names: list[str] = []

        if task.context.system_rendered:
            rendered = task.context.system_rendered
            system_found = False
            for message in messages:
                if isinstance(message, dict) and message.get("role") == "system":
                    current_content = message.get("content", "")
                    if not isinstance(current_content, str):
                        current_content = str(current_content)

                    # Keep caller-provided system prompt and append injected context.
                    if current_content.strip() and rendered.strip() and rendered not in current_content:
                        message["content"] = f"{current_content}\n\n{rendered}"
                    elif rendered.strip() and not current_content.strip():
                        message["content"] = rendered
                    system_found = True
                    break
            if not system_found:
                messages.insert(0, {"role": "system", "content": rendered})

        if request_has_tools_field:
            request_tools = payload.get("tools")
            if request_tools is None:
                payload["tools"] = []
            elif isinstance(request_tools, list):
                out_tools = []
                for raw_tool in request_tools:
                    if isinstance(raw_tool, dict):
                        out_tools.append(raw_tool)
                        continue
                    if not isinstance(raw_tool, str):
                        continue

                    tool_name = raw_tool.strip()
                    if not tool_name:
                        continue
                    tool_spec = task.context.tools.get(tool_name)
                    if not isinstance(tool_spec, dict):
                        continue

                    out_tools.append(OpenAIxWorker._tool_payload_entry(tool_name, tool_spec))
                    injected_tool_names.append(tool_name)

                payload["tools"] = out_tools
        elif task.context.tools:
            out_tools = []
            for tool_name, tool_spec in task.context.tools.items():
                if not isinstance(tool_spec, dict):
                    continue
                out_tools.append(OpenAIxWorker._tool_payload_entry(tool_name, tool_spec))
                injected_tool_names.append(tool_name)
            if out_tools:
                payload["tools"] = out_tools

        task.config["injected_tool_names"] = injected_tool_names
        payload["messages"] = messages
        task.payload = payload

    @staticmethod
    def _tool_payload_entry(tool_name: str, tool_spec: dict) -> dict:
        """Build one OpenAI/Ollama-compatible tool descriptor from internal tool metadata."""
        return {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": tool_spec.get("description", tool_name),
                "parameters": tool_spec.get("inputSchema", {"type": "object"}),
            },
        }

    @staticmethod
    def _extract_requested_internal_tool_names(request_tools_value: object) -> set[str]:
        """Return internal-tool names explicitly requested via string items in payload.tools."""
        if not isinstance(request_tools_value, list):
            return set()
        return {
            item.strip()
            for item in request_tools_value
            if isinstance(item, str) and item.strip()
        }

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
        max_turns = self._tools_max_turns
        turn = 0
        last_step_data: dict | None = None

        # Execute only tools that were actually injected by aidir for this request.
        available_tools = self._extract_injected_tool_names(parent_task)

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
            last_step_data = data if isinstance(data, dict) else None
            assistant_msg = data.get("message") if isinstance(data.get("message"), dict) else {}
            calls = self._extract_tool_calls(assistant_msg)
            if not calls:
                content = assistant_msg.get("content") or ""
                thinking = assistant_msg.get("thinking") or ""
                # Thinking-mode models (e.g. qwen3) sometimes output the answer
                # only in 'thinking' and leave 'content' empty.  Fall back to
                # 'thinking' so the client receives a non-empty response.
                if not content.strip() and thinking.strip():
                    log(
                        "worker",
                        "warning",
                        f"Task {parent_task.id} final response has empty content but non-empty thinking; using thinking as content",
                        "openaix",
                    )
                    data = dict(data)
                    patched_msg = dict(assistant_msg)
                    patched_msg["content"] = thinking
                    data["message"] = patched_msg
                    content = thinking

                content_preview = content[:300]
                log(
                    "worker",
                    "info",
                    f"Task {parent_task.id} final response turn={turn} role={assistant_msg.get('role')} content={content_preview!r}",
                    "openaix",
                )
                if parent_task.stream and emit_chunk:
                    await emit_chunk(data)
                return WorkerResult(ok=True, data=data, usage=data.get("usage"))

            # Separate tool calls into executable (local workers) and pass-through (external)
            executable_calls, external_calls = self._split_tool_calls(calls, available_tools)

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
            messages.append(self._normalize_assistant_message_for_history(assistant_msg))
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
                    "name": call["name"],
                    "content": self._extract_tool_content_text(tool_result.data),
                }
                if call.get("id"):
                    tool_message["tool_call_id"] = call["id"]
                messages.append(tool_message)

        # Fallback: request one final answer without tools instead of hard failing.
        log(
            "worker",
            "warning",
            f"Task {parent_task.id} tool-loop reached limit ({max_turns}); requesting final answer without tools",
            "openaix",
        )

        fallback_payload = dict(current_payload)
        fallback_payload.pop("tools", None)
        fallback_payload.pop("tool_choice", None)
        fallback_messages = list(messages)
        fallback_messages.append(
            {
                "role": "system",
                "content": (
                    "Tool loop limit reached. Provide a final user-facing answer based on the conversation "
                    "and tool outputs already available. Do not call tools."
                ),
            }
        )
        fallback_payload["messages"] = fallback_messages

        fallback_step = await self._forward_sync(
            client,
            url,
            fallback_payload,
            save_call=save_call,
            task_id=parent_task.id,
        )
        if fallback_step.ok:
            fd = fallback_step.data or {}
            fb_msg = fd.get("message") if isinstance(fd.get("message"), dict) else {}
            fb_content = fb_msg.get("content") or ""
            fb_thinking = fb_msg.get("thinking") or ""
            if not fb_content.strip() and fb_thinking.strip():
                fd = dict(fd)
                fd["message"] = dict(fb_msg)
                fd["message"]["content"] = fb_thinking
                fallback_step = WorkerResult(ok=True, data=fd, usage=fd.get("usage"))
            if parent_task.stream and emit_chunk and isinstance(fd, dict):
                await emit_chunk(fd)
            return fallback_step

        if last_step_data is not None:
            return WorkerResult(ok=True, data=last_step_data, usage=last_step_data.get("usage"))

        return WorkerResult(ok=False, error={"code": "TOOL_LOOP_LIMIT", "message": "Tool loop exceeded limit"})

    @staticmethod
    def _extract_injected_tool_names(task: Task) -> set[str]:
        """Return the names of tools that were injected by aidir for this task."""
        task_cfg = task.config if isinstance(task.config, dict) else {}
        raw_names = task_cfg.get("injected_tool_names")
        if not isinstance(raw_names, list):
            return set()
        return {
            name.strip()
            for name in raw_names
            if isinstance(name, str) and name.strip()
        }

    @staticmethod
    def _split_tool_calls(calls: list[dict], available_tools: set[str]) -> tuple[list[dict], list[dict]]:
        """Split model tool calls into aidir-executable and pass-through groups."""
        executable_calls = [call for call in calls if call["name"] in available_tools]
        external_calls = [call for call in calls if call["name"] not in available_tools]
        return executable_calls, external_calls

    @staticmethod
    def _parse_tool_arguments(raw_args: object) -> dict:
        """Parse tool arguments from dict/string formats into a dict."""
        if isinstance(raw_args, dict):
            return raw_args

        if not isinstance(raw_args, str):
            return {}

        text = raw_args.strip()
        if not text:
            return {}

        # 1) Standard JSON object in string form.
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
            # 2) Some models return a JSON string that itself contains a JSON object.
            if isinstance(parsed, str):
                nested = json.loads(parsed)
                if isinstance(nested, dict):
                    return nested
        except Exception:
            pass

        # 3) Best-effort fallback for python-literal style objects with quotes.
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

        return {}

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

            raw_args = fn.get("arguments") or call.get("arguments") or {}
            args = OpenAIxWorker._parse_tool_arguments(raw_args)

            out.append(
                {
                    "id": call.get("id") or "",
                    "name": str(name),
                    "arguments": args,
                }
            )
        return out

    @staticmethod
    def _normalize_assistant_message_for_history(message: dict) -> dict:
        """Keep only Ollama-required fields for follow-up tool turns."""
        normalized: dict = {"role": "assistant", "content": ""}

        content = message.get("content") if isinstance(message, dict) else ""
        if isinstance(content, str):
            normalized["content"] = content
        elif content is None:
            normalized["content"] = ""
        else:
            normalized["content"] = str(content)

        calls = message.get("tool_calls") if isinstance(message, dict) else None
        if not isinstance(calls, list):
            return normalized

        normalized_calls: list[dict] = []
        for raw_call in calls:
            if not isinstance(raw_call, dict):
                continue

            fn = raw_call.get("function") if isinstance(raw_call.get("function"), dict) else {}
            name = fn.get("name") or raw_call.get("name")
            if not name:
                continue

            # Ollama /api/chat expects arguments as a dict (object), not a JSON string.
            # OpenAI-style (string) would cause Ollama to fail with JSON parse error.
            raw_args = fn.get("arguments") or raw_call.get("arguments") or {}
            args = OpenAIxWorker._parse_tool_arguments(raw_args)

            call_entry: dict = {
                "function": {
                    "name": str(name),
                    "arguments": args,
                },
            }
            call_id = raw_call.get("id")
            if call_id:
                call_entry["id"] = str(call_id)
            normalized_calls.append(call_entry)

        if normalized_calls:
            normalized["tool_calls"] = normalized_calls
        return normalized

    @staticmethod
    def _extract_tool_content_text(data: dict | None) -> str:
        """Extract plain text from MCP tool result for Ollama tool message content."""
        if not isinstance(data, dict):
            return str(data or "")
        # MCP standard: content array with text items
        content_arr = data.get("content")
        if isinstance(content_arr, list):
            texts = [
                item.get("text", "")
                for item in content_arr
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            if texts:
                return "\n".join(texts)
        # Fallback: serialize the whole thing
        return json.dumps(data, ensure_ascii=False)

    async def _execute_internal_tool(self, call: dict, parent_task: Task_agent) -> WorkerResult:
        """Run an internal tool via Task_tool and wait for completion."""
        tool_name = call["name"]
        tool_context = parent_task.context.tools if parent_task.context and parent_task.context.tools else {}
        meta = tool_context.get(tool_name)
        if meta is None:
            return WorkerResult(ok=False, error={"code": "TOOL_NOT_FOUND", "message": f"Unknown internal tool: {tool_name}"})

        task = Task_tool(payload={"tool": tool_name, "arguments": call.get("arguments") or {}})
        self.bind_child_task(
            task,
            parent_task=parent_task,
            parent_context={"tool": tool_name},
        )
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
        messages = upstream_payload.get("messages")
        msg_count = len(messages) if isinstance(messages, list) else 0
        last_role = ""
        if isinstance(messages, list) and messages:
            last_msg = messages[-1]
            if isinstance(last_msg, dict):
                last_role = str(last_msg.get("role", ""))

        log(
            "worker",
            "debug",
            (
                f"Task {task_id or '-'} upstream sync request: "
                f"messages={msg_count} last_role={last_role!r} "
                f"tools={bool(upstream_payload.get('tools'))}"
            ),
            "openaix",
        )

        request = self._build_json_request(client, url, upstream_payload)
        if save_call:
            save_llm_raw_call(self.id, request.content)

        resp = await self._send_json_request(client, request, upstream_payload)
        raw_response = await self._read_response_body(resp)
        if save_call:
            save_llm_raw_call(self.id, raw_response)

        if resp.status_code != 200:
            body_preview = resp.text[:512]
            log(
                "worker",
                "warning",
                (
                    f"Task {task_id or '-'} upstream sync error: status={resp.status_code} "
                    f"messages={msg_count} last_role={last_role!r} body={body_preview!r}"
                ),
                "openaix",
            )
            return WorkerResult(
                ok=False,
                error={
                    "code": "UPSTREAM_ERROR",
                    "message": f"Upstream returned HTTP {resp.status_code}",
                    "body": body_preview,
                },
            )

        try:
            data = json.loads(raw_response)
        except json.JSONDecodeError:
            body_preview = resp.text[:512]
            log(
                "worker",
                "warning",
                (
                    f"Task {task_id or '-'} upstream sync invalid json: "
                    f"messages={msg_count} last_role={last_role!r} body={body_preview!r}"
                ),
                "openaix",
            )
            return WorkerResult(
                ok=False,
                error={
                    "code": "UPSTREAM_INVALID_JSON",
                    "message": "Upstream returned invalid JSON",
                    "body": body_preview,
                },
            )

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
        request = self._build_json_request(client, url, upstream_payload)
        if save_call:
            save_llm_raw_call(self.id, request.content)

        stream_context = await self._open_stream_request(client, request, upstream_payload)
        raw_parts: list[bytes] = [] if save_call else []
        async with stream_context as resp:
            if resp.status_code != 200:
                raw_error = await self._read_response_body(resp)
                if save_call:
                    save_llm_raw_call(self.id, raw_error)
                log(
                    "worker",
                    "warning",
                    f"Task {task_id or '-'} upstream stream error: status={resp.status_code}",
                    "openaix",
                )
                return WorkerResult(
                    ok=False,
                    error={
                        "code": "UPSTREAM_ERROR",
                        "message": f"Upstream returned HTTP {resp.status_code}",
                    },
                )

            final_data = await self._consume_stream_response(resp, emit_chunk, chunks, raw_parts, save_call)

        if save_call:
            save_llm_raw_call(self.id, b"".join(raw_parts))

        if save_call:
            save_llm_call(self.id, task_id, upstream_payload, {"stream_chunks": chunks})
        return WorkerResult(ok=True, data=final_data, usage=(final_data or {}).get("usage"))

    @staticmethod
    def _build_json_request(client: httpx.AsyncClient, url: str, payload: dict) -> httpx.Request:
        """Build the exact JSON request object when supported by the client."""
        build_request = getattr(client, "build_request", None)
        if callable(build_request):
            return build_request("POST", url, json=payload)
        return httpx.Request("POST", url, json=payload)

    @staticmethod
    async def _send_json_request(client: httpx.AsyncClient, request: httpx.Request, payload: dict):
        """Send one non-streaming request using either real httpx or a test double."""
        send = getattr(client, "send", None)
        if callable(send):
            return await send(request)
        return await client.post(str(request.url), json=payload)

    @staticmethod
    async def _open_stream_request(client: httpx.AsyncClient, request: httpx.Request, payload: dict):
        """Open one streaming request using either real httpx or a test double."""
        send = getattr(client, "send", None)
        if callable(send):
            return await send(request, stream=True)
        return client.stream(request.method, str(request.url), json=payload)

    @staticmethod
    async def _read_response_body(resp) -> bytes | str:
        """Read a full response body without altering it when bytes are available."""
        aread = getattr(resp, "aread", None)
        if callable(aread):
            return await aread()

        text = getattr(resp, "text", None)
        if isinstance(text, str):
            return text

        content = getattr(resp, "content", b"")
        if isinstance(content, (bytes, str)):
            return content
        return str(content)

    @staticmethod
    async def _consume_stream_response(resp, emit_chunk, chunks: list[dict], raw_parts: list[bytes], save_call: bool) -> dict | None:
        """Parse Ollama NDJSON stream while preserving raw bytes for logging."""
        final_data: dict | None = None

        aiter_raw = getattr(resp, "aiter_raw", None)
        if callable(aiter_raw):
            decoder = codecs.getincrementaldecoder("utf-8")()
            text_buffer = ""
            async for raw_chunk in aiter_raw():
                if save_call:
                    raw_parts.append(raw_chunk)
                decoded = decoder.decode(raw_chunk)
                text_buffer += decoded
                while "\n" in text_buffer:
                    line, text_buffer = text_buffer.split("\n", 1)
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

            tail = decoder.decode(b"", final=True)
            if tail:
                text_buffer += tail
            if text_buffer.strip():
                try:
                    chunk = json.loads(text_buffer)
                except json.JSONDecodeError:
                    chunk = None
                if isinstance(chunk, dict):
                    if emit_chunk:
                        await emit_chunk(chunk)
                    if save_call:
                        chunks.append(chunk)
                    if chunk.get("done"):
                        final_data = chunk
            return final_data

        aiter_lines = getattr(resp, "aiter_lines", None)
        if callable(aiter_lines):
            async for line in aiter_lines():
                if save_call:
                    raw_parts.append(line.encode("utf-8") + b"\n")
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

        return final_data


worker = OpenAIxWorker()
