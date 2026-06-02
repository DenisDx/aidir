"""
OpenAIx endpoint.
Supports:
  - limited Ollama-compatible API: POST /api/chat
  - OpenAI-compatible API:      POST /v1/chat/completions

Implementation reuses Task_agent flow from Endpoint_ollama and converts
between OpenAI and Ollama payload/response formats.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from core import log
from core.error_logging import attach_request_id_middleware, get_or_create_request_id, log_exception
from core.endpoints.endpoint_ollama import Endpoint_ollama
from core.smart_router import SmartRouteError
from core.task import STATUS_CANCELED, STATUS_COMPLETED, STATUS_FAILED


class Endpoint_openaix(Endpoint_ollama):
    """Endpoint implementing OpenAI-compatible API plus limited /api/chat."""

    api = "openaix"
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

    def __init__(self, endpoint_cfg: dict) -> None:
        super().__init__(endpoint_cfg)
        # When enabled, responses follow protocol-specific error envelopes
        # (OpenAI format for /v1/* and Ollama format for /api/*).
        self._errors_compatibility_mode = bool(
            endpoint_cfg.get("errors_compatibility_mode", True)
        )
        # Timeout for entire endpoint request (queue + execution)
        self._request_timeout = int(endpoint_cfg.get("request_timeout", 100))
        self._model_resolution_warnings_emitted: set[str] = set()

    def create_app(self, core) -> FastAPI:
        """Create FastAPI app exposing both ollama and openai chat endpoints."""
        self._core = core
        app = FastAPI(title=f"aidir-{self.id}", docs_url=None, redoc_url=None)
        attach_request_id_middleware(app)

        @app.exception_handler(Exception)
        async def unhandled_exception_handler(request: Request, exc: Exception):
            """Log unhandled endpoint exceptions with traceback and request id."""
            request_id = get_or_create_request_id(request)
            log_exception(
                "http",
                self.id,
                f"Unhandled exception method={request.method} path={request.url.path}",
                exc,
                request_id=request_id,
            )
            return JSONResponse(
                status_code=500,
                content={"error": {"code": "INTERNAL_ERROR", "message": "Internal server error", "request_id": request_id}},
                headers={"X-Request-ID": request_id},
            )

        @app.post("/api/chat")
        async def api_chat(request: Request):
            # Limited Ollama compatibility path.
            return await self._handle_chat(request)

        @app.get("/api/tags")
        async def api_tags():
            return self._ollama_tags_response()

        @app.post("/v1/chat/completions")
        async def openai_chat_completions(request: Request):
            return await self._handle_openai_chat(request)

        @app.get("/v1/models")
        async def openai_models():
            return self._openai_models_response()

        @app.get("/v1/providers/{provider_id}/models/{model_id:path}/queue-state")
        async def openai_model_queue_state(provider_id: str, model_id: str, priority: int = 5):
            return await self._model_queue_state_response(
                protocol="openai",
                provider_id=provider_id,
                model_id=model_id,
                priority=priority,
            )

        @app.get("/v1/models/{model_id:path}/queue-state")
        async def openai_model_only_queue_state(model_id: str, priority: int = 5):
            return await self._model_only_queue_state_response(
                protocol="openai",
                model_id=model_id,
                priority=priority,
            )

        @app.get("/api/providers/{provider_id}/models/{model_id:path}/queue-state")
        async def ollama_model_queue_state(provider_id: str, model_id: str, priority: int = 5):
            return await self._model_queue_state_response(
                protocol="ollama",
                provider_id=provider_id,
                model_id=model_id,
                priority=priority,
            )

        @app.get("/api/models/{model_id:path}/queue-state")
        async def ollama_model_only_queue_state(model_id: str, priority: int = 5):
            return await self._model_only_queue_state_response(
                protocol="ollama",
                model_id=model_id,
                priority=priority,
            )

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        return app

    async def _handle_chat(self, request: Request) -> StreamingResponse | JSONResponse:
        """Handle Ollama-compatible /api/chat and queue the openaix worker."""
        try:
            body = await request.json()
        except Exception:
            return self._error_response(
                protocol="ollama",
                status_code=400,
                code="INVALID_REQUEST",
                message="Invalid JSON body",
            )

        auth_error = self._authorize_and_apply_envid(request, body)
        if auth_error is not None:
            return auth_error

        stream = bool(body.get("stream", False))
        incoming_bearer_token = self._extract_bearer_token(request)
        route_trace = self._build_incoming_route_trace(request.headers)
        route_trace_error = self._validate_incoming_route_trace(route_trace)
        if route_trace_error is not None:
            code, status_code, message = route_trace_error
            return self._error_response(
                protocol="ollama",
                status_code=status_code,
                code=code,
                message=message,
            )
        try:
            task = await self._build_task_for_payload_async(
                body,
                stream,
                incoming_bearer_token=incoming_bearer_token,
                route_trace=route_trace,
            )
        except SmartRouteError as exc:
            return self._error_response(
                protocol="ollama",
                status_code=exc.status_code,
                code=exc.code,
                message=str(exc),
            )

        try:
            await self._core.on_task_added(task)
        except Exception as exc:
            return self._error_response(
                protocol="ollama",
                status_code=503,
                code=getattr(exc, "code", "QUEUE_ERROR"),
                message=str(exc),
                task_id=task.id,
            )

        if stream:
            return StreamingResponse(
                self._stream_response(task),
                media_type="application/x-ndjson",
            )
        response = await self._sync_response(task)
        log(
            "http",
            "info",
            (
                f"{self.id} /api/chat sync response: task={task.id} status={task.status} "
                f"http_status={response.status_code} error_code={(task.error or {}).get('code') if isinstance(task.error, dict) else ''}"
            ),
            self.id,
        )
        return response

    async def _handle_openai_chat(self, request: Request) -> StreamingResponse | JSONResponse:
        """Handle OpenAI chat completions request and map it to Task_agent flow."""
        try:
            body = await request.json()
        except Exception:
            return self._error_response(
                protocol="openai",
                status_code=400,
                code="invalid_request_error",
                message="Invalid JSON body",
            )

        auth_error = self._authorize_and_apply_envid(request, body)
        if auth_error is not None:
            return auth_error

        stream = bool(body.get("stream", False))
        incoming_bearer_token = self._extract_bearer_token(request)
        route_trace = self._build_incoming_route_trace(request.headers)
        route_trace_error = self._validate_incoming_route_trace(route_trace)
        if route_trace_error is not None:
            code, status_code, message = route_trace_error
            return self._error_response(
                protocol="openai",
                status_code=status_code,
                code=code,
                message=message,
            )
        ollama_payload = self._openai_request_to_ollama(body)
        try:
            task = await self._build_task_for_payload_async(
                ollama_payload,
                stream,
                incoming_bearer_token=incoming_bearer_token,
                route_trace=route_trace,
            )
        except SmartRouteError as exc:
            return self._error_response(
                protocol="openai",
                status_code=exc.status_code,
                code=exc.code,
                message=str(exc),
            )

        try:
            await self._core.on_task_added(task)
        except Exception as exc:
            return self._error_response(
                protocol="openai",
                status_code=503,
                code=getattr(exc, "code", "server_error"),
                message=str(exc),
                task_id=task.id,
            )

        if stream:
            return StreamingResponse(
                self._openai_stream_response(task, body),
                media_type="text/event-stream",
            )

        return await self._openai_sync_response(task, body)

    def _build_task_for_payload(self, payload: dict, stream: bool):
        """Create Task_agent with standard timeout and worker selection rules."""
        payload = self._apply_generation_defaults(payload)
        worker_id = self._resolve_worker_id(payload)
        route = self._resolve_model_route((payload or {}).get("model"), worker_id)
        if route is not None and self._provider_api(route["resolved_provider"]) == "smart":
            raise SmartRouteError(
                code="SMART_ROUTE_ASYNC_REQUIRED",
                status_code=500,
                message="Smart routes require async task construction",
            )

        return self._create_task_for_payload(payload, stream, worker_id, route)

    async def _build_task_for_payload_async(
        self,
        payload: dict,
        stream: bool,
        *,
        incoming_bearer_token: str = "",
        route_trace: dict | None = None,
    ):
        """Create Task_agent and resolve smart routes before queueing when needed."""
        payload = self._apply_generation_defaults(payload)
        worker_id = self._resolve_worker_id(payload)
        route = self._resolve_model_route((payload or {}).get("model"), worker_id)
        if route is not None and self._provider_api(route["resolved_provider"]) == "smart":
            route = await self._resolve_smart_route(
                route,
                worker_id=worker_id,
                payload=payload,
                incoming_bearer_token=incoming_bearer_token,
            )

        task = self._create_task_for_payload(payload, stream, worker_id, route)
        if incoming_bearer_token:
            task.config = dict(task.config or {})
            task.config["incoming_bearer_token"] = incoming_bearer_token
        if isinstance(route_trace, dict):
            task.config = dict(task.config or {})
            task.config["route_trace"] = dict(route_trace)
        return task

    def _create_task_for_payload(self, payload: dict, stream: bool, worker_id: str, route: dict | None):
        """Finalize Task_agent creation once worker and route are resolved."""
        from core.task_types.task_agent import Task_agent

        if route is not None:
            payload = dict(payload or {})
            payload["model"] = route["resolved_model"]

        effective_worker_id = self._resolve_worker_id_for_route(worker_id, route)

        task = Task_agent(payload=payload, stream=stream, external=True)
        if effective_worker_id:
            task.worker_id = effective_worker_id
        if route is not None:
            task.config = dict(task.config or {})
            task.config["route"] = route

        cfg_tasks = self._core.config.get("tasks", {}) or {}
        # Use timeout from request if specified, otherwise use defaults
        if "timeout" in payload and payload["timeout"] is not None:
            timeout_val = int(payload["timeout"])
            task.queue_timeout = timeout_val
            task.run_timeout = timeout_val
        else:
            task.queue_timeout = int(cfg_tasks.get("queue_timeout", 300))
            task.run_timeout = int(cfg_tasks.get("run_timeout", 300))
        return task

    async def _resolve_smart_route(
        self,
        route: dict,
        *,
        worker_id: str,
        payload: dict,
        incoming_bearer_token: str = "",
    ) -> dict:
        """Resolve one smart alias route into a concrete provider/model pair."""
        return await super()._resolve_smart_route(
            route,
            worker_id=worker_id,
            payload=payload,
            incoming_bearer_token=incoming_bearer_token,
        )

    async def _evaluate_smart_candidate(
        self,
        item: object,
        *,
        request_priority: int,
        index: int,
        incoming_bearer_token: str = "",
    ) -> dict | None:
        """Evaluate one smart routing candidate using locally visible queue/resource state."""
        return await super()._evaluate_smart_candidate(
            item,
            request_priority=request_priority,
            index=index,
            incoming_bearer_token=incoming_bearer_token,
        )

    @staticmethod
    def _build_smart_route_result(
        route: dict,
        candidate: dict,
        *,
        strategy: str,
        reason: str,
        candidate_probes: list[dict],
    ) -> dict:
        """Build final concrete route metadata for a selected smart candidate."""
        return Endpoint_ollama._build_smart_route_result(
            route,
            candidate,
            strategy=strategy,
            reason=reason,
            candidate_probes=candidate_probes,
        )

    @staticmethod
    def _resolve_request_priority(payload: dict | None) -> int:
        """Resolve routing priority from payload, defaulting to the normal task priority."""
        try:
            return max(0, int((payload or {}).get("priority", 5)))
        except Exception:
            return 5

    def _resolve_worker_id(self, payload: dict) -> str:
        """Resolve worker id for a request using payload override, endpoint cfg, or global default."""
        if isinstance(payload, dict) and payload.get("worker"):
            return str(payload["worker"])
        if self._cfg.get("worker"):
            return str(self._cfg["worker"])
        if self._core is not None:
            default_worker = self._core.config.get("workers.default")
            if default_worker:
                return str(default_worker)
        return ""

    def _apply_generation_defaults(self, payload: dict) -> dict:
        """Apply per-worker generation defaults unless request already overrides them."""
        if not isinstance(payload, dict):
            return payload

        worker_id = self._resolve_worker_id(payload)
        if not worker_id or self._core is None:
            return payload

        defaults = self._core.config.get(f"workers.items.{worker_id}.generation_defaults") or {}
        if not isinstance(defaults, dict):
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

            default_value = defaults.get(field_name)
            target_field = field_name
            if default_value is None and option_name != field_name:
                default_value = defaults.get(option_name)
                target_field = option_name
            if default_value is not None:
                out[target_field] = default_value
                applied_options.add(option_name)
        return out

    @classmethod
    def _payload_has_generation_value(cls, payload: dict, option_name: str) -> bool:
        """Return whether the payload already defines any top-level alias for one Ollama option name."""
        if not isinstance(payload, dict):
            return False
        for field_name, mapped_option in cls._GENERATION_OPTION_FIELDS.items():
            if mapped_option != option_name:
                continue
            if payload.get(field_name) is not None:
                return True
        return False

    @staticmethod
    def _openai_request_to_ollama(body: dict) -> dict:
        """Convert OpenAI chat.completions request to Ollama /api/chat payload."""
        payload = {
            "model": body.get("model", ""),
            "messages": body.get("messages", []),
            "stream": bool(body.get("stream", False)),
        }

        # Keep explicit worker override if caller uses aidir extension.
        if "worker" in body:
            payload["worker"] = body["worker"]

        # Pass through selected optional fields when present.
        passthrough = [
            "envid",
            "context_builder",
            "log",
            "options",
            "tools",
            "tool_choice",
            "temperature",
            "top_p",
            "repeat_penalty",
            "repetition_penalty",
            "repeat_last_n",
            "num_predict",
            "max_tokens",
            "seed",
            "presence_penalty",
            "frequency_penalty",
            "top_k",
            "min_p",
            "stop",
        ]
        for key in passthrough:
            if key in body:
                payload[key] = body[key]

        return payload

    async def _openai_sync_response(self, task, request_body: dict) -> JSONResponse:
        """Wait for task completion and return OpenAI chat.completion JSON."""
        # Use timeout from task if set by request, otherwise use endpoint default
        timeout = task.run_timeout if task.run_timeout > 0 else self._request_timeout
        try:
            await asyncio.wait_for(task._done_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            await self._core.queue.mark_canceled(task)
            asyncio.create_task(self._core.delete_task(task.id))
            log("http", "warning", f"{self.id} /v1/chat/completions timeout: task={task.id}", self.id)
            return self._error_response(
                protocol="openai",
                status_code=504,
                code="timeout_error",
                message="Request timed out",
                task_id=task.id,
            )

        asyncio.create_task(self._core.delete_task(task.id))

        if task.status == STATUS_COMPLETED:
            result = task.result or {}
            usage = result.get("usage") if isinstance(result, dict) else {}
            log(
                "http",
                "info",
                (
                    f"{self.id} /v1/chat/completions success: task={task.id} "
                    f"prompt_tokens={(usage or {}).get('prompt_eval_count')} "
                    f"completion_tokens={(usage or {}).get('eval_count')}"
                ),
                self.id,
            )
            return JSONResponse(self._ollama_sync_to_openai(task.result or {}, task.id, request_body))
        if task.status == STATUS_FAILED:
            err = task.error or {}
            log(
                "http",
                "warning",
                (
                    f"{self.id} /v1/chat/completions failed: task={task.id} "
                    f"code={err.get('code', 'server_error')} message={err.get('message', 'Worker error')}"
                ),
                self.id,
            )
            return self._error_response(
                protocol="openai",
                status_code=502,
                code=(task.error or {}).get("code", "server_error"),
                message=(task.error or {}).get("message", "Worker error"),
                task_id=task.id,
            )
        if task.status == STATUS_CANCELED:
            log("http", "warning", f"{self.id} /v1/chat/completions canceled: task={task.id}", self.id)
            return self._error_response(
                protocol="openai",
                status_code=503,
                code="server_error",
                message="Task was canceled",
                task_id=task.id,
            )

        log("http", "warning", f"{self.id} /v1/chat/completions unknown_state: task={task.id} state={task.status}", self.id)
        return self._error_response(
            protocol="openai",
            status_code=500,
            code="server_error",
            message="Unknown task state",
            task_id=task.id,
        )

    async def _openai_stream_response(self, task, request_body: dict) -> AsyncGenerator[bytes, None]:
        """Stream OpenAI-compatible SSE chunks converted from Ollama chunks."""
        # Use timeout from task if set by request, otherwise use endpoint default
        timeout = task.run_timeout if task.run_timeout > 0 else self._request_timeout
        deadline = asyncio.get_event_loop().time() + timeout

        try:
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    await self._core.queue.mark_canceled(task)
                    err = self._openai_error_payload(
                        code="timeout_error",
                        message="Request timed out",
                        task_id=task.id,
                    )
                    yield f"data: {json.dumps(err)}\n\n".encode()
                    break

                try:
                    chunk = await asyncio.wait_for(task._chunk_queue.get(), timeout=min(remaining, 5.0))
                except asyncio.TimeoutError:
                    continue

                if chunk is None:
                    break

                openai_chunk = self._ollama_chunk_to_openai(chunk, task.id, request_body)
                yield f"data: {json.dumps(openai_chunk)}\n\n".encode()

            # OpenAI streaming terminator.
            yield b"data: [DONE]\n\n"
        finally:
            asyncio.create_task(self._core.delete_task(task.id))

    def _collect_models(self) -> list[str]:
        """Collect unique externally visible model ids from configured providers."""
        return [item["id"] for item in self._collect_model_entries()]

    def _collect_model_entries(self) -> list[dict[str, str]]:
        """Collect unique externally visible model ids plus resolved internal ids."""
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        worker_id = self._resolve_worker_id({})

        for _, model in self._iter_provider_models():
            external_id = self._external_model_id(model)
            if not external_id or external_id in seen:
                continue
            seen.add(external_id)

            route = self._resolve_model_route(external_id, worker_id)
            real_id = route["resolved_model"] if isinstance(route, dict) and route.get("resolved_model") else self._model_id(model)
            out.append({"id": external_id, "real_id": real_id or external_id})

        return out

    def _iter_provider_models(self) -> list[tuple[str, dict]]:
        """Return configured provider/model pairs in config order."""
        providers = self._core.config.get("models.providers") or {}
        out: list[tuple[str, dict]] = []
        if not isinstance(providers, dict):
            return out

        for provider_id, provider in providers.items():
            if not isinstance(provider, dict):
                continue
            models = provider.get("models", [])
            if not isinstance(models, list):
                continue
            for model in models:
                if isinstance(model, dict):
                    out.append((str(provider_id), model))
        return out

    @staticmethod
    def _model_alias(model_cfg: dict) -> str:
        """Return explicit external alias for a model or empty string when missing."""
        alias = model_cfg.get("alias") if isinstance(model_cfg, dict) else None
        return str(alias).strip() if alias is not None else ""

    @staticmethod
    def _model_id(model_cfg: dict) -> str:
        """Return internal model id, falling back to configured name when needed."""
        if not isinstance(model_cfg, dict):
            return ""
        return str(model_cfg.get("id") or model_cfg.get("name") or "").strip()

    def _external_model_id(self, model_cfg: dict) -> str:
        """Return externally visible model name used by clients."""
        return self._model_alias(model_cfg) or self._model_id(model_cfg)

    def _preferred_provider_for_worker(self, worker_id: str) -> str:
        """Return preferred provider configured for the selected worker."""
        if not worker_id or self._core is None:
            return ""
        provider_id = self._core.config.get(f"workers.items.{worker_id}.provider")
        return str(provider_id).strip() if provider_id is not None else ""

    def _provider_cfg(self, provider_id: str) -> dict:
        """Return provider config dictionary or an empty dict when missing."""
        providers = self._core.config.get("models.providers") or {} if self._core is not None else {}
        if not isinstance(providers, dict):
            return {}
        provider_cfg = providers.get(provider_id)
        return provider_cfg if isinstance(provider_cfg, dict) else {}

    def _provider_api(self, provider_id: str) -> str:
        """Return provider api type string for a configured provider."""
        return str(self._provider_cfg(provider_id).get("api") or "").strip()

    def _find_provider_model_cfg(self, provider_id: str, model_id: str) -> dict | None:
        """Return provider model config matched by id, name, or alias."""
        provider_cfg = self._provider_cfg(provider_id)
        models = provider_cfg.get("models") or []
        if not isinstance(models, list):
            return None

        normalized_model_id = str(model_id).strip()
        for model in models:
            if not isinstance(model, dict):
                continue
            if normalized_model_id not in {self._model_id(model), str(model.get("name") or "").strip(), self._model_alias(model)}:
                continue
            return model
        return None

    def _warn_model_resolution(self, warning_key: str, message: str) -> None:
        """Emit a model-resolution warning once per endpoint instance."""
        if warning_key in self._model_resolution_warnings_emitted:
            return
        self._model_resolution_warnings_emitted.add(warning_key)
        log("http", "warning", message, self.id)

    def _resolve_model_route(self, requested_model: object, worker_id: str) -> dict | None:
        """Resolve external model id to provider/model using alias-aware precedence."""
        requested = str(requested_model or "").strip()
        if not requested:
            return None

        preferred_provider = self._preferred_provider_for_worker(worker_id)
        matches: list[dict] = []

        for order, (provider_id, model_cfg) in enumerate(self._iter_provider_models()):
            alias = self._model_alias(model_cfg)
            model_id = self._model_id(model_cfg)
            if not model_id:
                continue

            rank: int | None = None
            if alias and requested == alias:
                rank = 0
            elif not alias and requested == model_id:
                rank = 1
            elif alias and requested == model_id:
                rank = 2

            if rank is None:
                continue

            matches.append(
                {
                    "rank": rank,
                    "order": order,
                    "provider": provider_id,
                    "model_id": model_id,
                }
            )

        if not matches:
            return None

        alias_matches = [item for item in matches if item["rank"] == 0]
        if len(alias_matches) > 1:
            self._warn_model_resolution(
                f"duplicate-alias:{requested}",
                (
                    f"Duplicate model alias '{requested}' in config; using preferred provider "
                    f"'{preferred_provider or 'first-configured'}' and config order fallback"
                ),
            )

        unaliased_id_matches = [item for item in matches if item["rank"] == 1]
        if len(unaliased_id_matches) > 1:
            self._warn_model_resolution(
                f"duplicate-unaliased-id:{requested}",
                (
                    f"Duplicate unaliased model id '{requested}' in config; using preferred provider "
                    f"'{preferred_provider or 'first-configured'}' and config order fallback"
                ),
            )

        def _sort_key(item: dict) -> tuple[int, int, int]:
            preferred_penalty = 0 if preferred_provider and item["provider"] == preferred_provider else 1
            return int(item["rank"]), preferred_penalty, int(item["order"])

        selected = min(matches, key=_sort_key)
        return {
            "requested_model": requested,
            "requested_alias": requested,
            "resolved_provider": selected["provider"],
            "resolved_model": selected["model_id"],
            "selection": "alias" if selected["rank"] == 0 else "model_id",
            "worker_preferred_provider": preferred_provider,
        }

    def _openai_models_response(self) -> dict:
        """Build OpenAI-compatible models list response."""
        now = int(time.time())
        return {
            "object": "list",
            "data": [
                {
                    "id": model_info["id"],
                    "real_id": model_info["real_id"],
                    "object": "model",
                    "created": now,
                    "owned_by": "aidir",
                }
                for model_info in self._collect_model_entries()
            ],
        }

    def _ollama_tags_response(self) -> dict:
        """Build limited Ollama-compatible /api/tags response."""
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return {
            "models": [
                {
                    "name": model_info["id"],
                    "model": model_info["id"],
                    "real_id": model_info["real_id"],
                    "modified_at": now_iso,
                    "size": 0,
                    "digest": "",
                    "details": {},
                }
                for model_info in self._collect_model_entries()
            ]
        }

    async def _model_queue_state_response(
        self,
        *,
        protocol: str,
        provider_id: str,
        model_id: str,
        priority: int,
    ) -> JSONResponse:
        """Return queue state for a provider/model pair."""
        if priority < 0:
            return self._error_response(
                protocol=protocol,
                status_code=400,
                code="INVALID_REQUEST",
                message="priority must be greater than or equal to 0",
            )

        resolved = self._resolve_model_resource_requirements(provider_id, model_id)
        if resolved is None:
            return self._error_response(
                protocol=protocol,
                status_code=404,
                code="INVALID_MODEL",
                message=f"Unknown provider/model pair: {provider_id}/{model_id}",
            )

        if self._core.queue is None:
            return self._error_response(
                protocol=protocol,
                status_code=503,
                code="QUEUE_UNAVAILABLE",
                message="Queue is not available",
            )

        queue_state = await self._core.queue.get_resource_queue_state(resolved, priority=priority)
        resource_ready = bool(self._core.resources and self._core.resources.check_available(resolved))
        blocked_by_same_or_higher = queue_state["queued_count_total"] - queue_state["queued_count_below_priority"]

        payload = {
            "provider": provider_id,
            "model": model_id,
            "priority": priority,
            "can_run_now": resource_ready and blocked_by_same_or_higher == 0,
            "queued_count_below_priority": queue_state["queued_count_below_priority"],
            "queued_count_total": queue_state["queued_count_total"],
            "priority_counts": queue_state["priority_counts"],
        }
        return JSONResponse(payload)

    async def _model_only_queue_state_response(
        self,
        *,
        protocol: str,
        model_id: str,
        priority: int,
    ) -> JSONResponse:
        """Resolve external model id using inference routing rules and return queue state."""
        worker_id = self._resolve_worker_id({})
        route = self._resolve_model_route(model_id, worker_id)
        if route is None:
            return self._error_response(
                protocol=protocol,
                status_code=404,
                code="INVALID_MODEL",
                message=f"Unknown model: {model_id}",
            )

        response = await self._model_queue_state_response(
            protocol=protocol,
            provider_id=route["resolved_provider"],
            model_id=route["resolved_model"],
            priority=priority,
        )
        try:
            payload = json.loads(response.body)
        except Exception:
            return response

        payload["requested_model"] = str(model_id)
        return JSONResponse(payload, status_code=response.status_code)

    def _resolve_model_resource_requirements(self, provider_id: str, model_id: str) -> dict | None:
        """Resolve configured resource requirements for a provider/model pair."""
        providers = self._config_get("models.providers") or {}
        if not providers:
            models_cfg = self._config_get("models") or {}
            if isinstance(models_cfg, dict):
                providers = models_cfg.get("providers") or {}
        if not isinstance(providers, dict):
            return None

        provider_cfg = providers.get(provider_id)
        if not isinstance(provider_cfg, dict):
            return None

        models = provider_cfg.get("models") or []
        if not isinstance(models, list):
            return None

        normalized_model_id = str(model_id).strip()
        for model in models:
            if not isinstance(model, dict):
                continue
            candidate_id = str(model.get("id") or "").strip()
            candidate_name = str(model.get("name") or "").strip()
            candidate_alias = self._model_alias(model)
            if normalized_model_id not in {candidate_id, candidate_name, candidate_alias}:
                continue
            resources = model.get("resources") or {}
            if not isinstance(resources, dict):
                return {}
            return self._normalize_resource_requirements(resources)

        return None

    def _config_get(self, key: str, default=None):
        """Read config keys from either Config objects or nested dicts."""
        cfg = getattr(self, "_core", None)
        cfg = getattr(cfg, "config", None)
        if cfg is None:
            return default
        getter = getattr(cfg, "get", None)
        if callable(getter):
            try:
                value = getter(key)
            except Exception:
                value = default
            else:
                if value is not None:
                    return value
        if isinstance(cfg, dict):
            current = cfg
            for part in key.split("."):
                if not isinstance(current, dict) or part not in current:
                    return default
                current = current[part]
            return current
        return default

    @staticmethod
    def _normalize_resource_requirements(resources: dict | None) -> dict[str, dict[str, int]]:
        """Normalize resource requirements to integer values."""
        normalized: dict[str, dict[str, int]] = {}
        for resource_id, metrics in (resources or {}).items():
            if not isinstance(metrics, dict):
                continue
            normalized_metrics: dict[str, int] = {}
            for metric_name, amount in metrics.items():
                try:
                    normalized_metrics[str(metric_name)] = int(amount)
                except Exception:
                    continue
            if normalized_metrics:
                normalized[str(resource_id)] = normalized_metrics
        return normalized

    def _openai_error_payload(self, code: str, message: str, task_id: str | None = None) -> dict:
        """OpenAI-style error payload."""
        payload = {
            "error": {
                "message": message,
                "type": code,
                "code": code,
            }
        }
        if task_id:
            payload["error"]["task_id"] = task_id
        return payload

    def _ollama_error_payload(self, code: str, message: str, task_id: str | None = None) -> dict:
        """Ollama-style error payload used in this project."""
        payload = {"error": {"code": code, "message": message}}
        if task_id:
            payload["error"]["task_id"] = task_id
        return payload

    def _error_response(
        self,
        *,
        protocol: str,
        status_code: int,
        code: str,
        message: str,
        task_id: str | None = None,
    ) -> JSONResponse:
        """Protocol-aware error response. Supports compatibility mode toggle."""
        if self._errors_compatibility_mode:
            payload = (
                self._openai_error_payload(code, message, task_id)
                if protocol == "openai"
                else self._ollama_error_payload(code, message, task_id)
            )
        else:
            payload = {"error": {"code": code, "message": message}}
            if task_id:
                payload["error"]["task_id"] = task_id
        return JSONResponse(payload, status_code=status_code)

    @staticmethod
    def _usage_from_ollama(data: dict) -> dict | None:
        """Map ollama usage-like counters to OpenAI usage when present."""
        prompt_tokens = data.get("prompt_eval_count")
        completion_tokens = data.get("eval_count")
        if prompt_tokens is None and completion_tokens is None:
            return None
        prompt_tokens = int(prompt_tokens or 0)
        completion_tokens = int(completion_tokens or 0)
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

    def _ollama_sync_to_openai(self, data: dict, task_id: str, request_body: dict) -> dict:
        """Convert non-stream ollama chat response to OpenAI chat.completion shape."""
        msg = data.get("message") or {}
        content = msg.get("content", "")
        model = data.get("model") or request_body.get("model") or ""

        resp = {
            "id": f"chatcmpl-{task_id}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        }

        usage = self._usage_from_ollama(data)
        if usage is not None:
            resp["usage"] = usage
        return resp

    def _ollama_chunk_to_openai(self, chunk: dict, task_id: str, request_body: dict) -> dict:
        """Convert one ollama stream chunk to OpenAI chat.completion.chunk shape."""
        msg = chunk.get("message") or {}
        content = msg.get("content", "")
        done = bool(chunk.get("done", False))

        delta = {"content": content}
        if not content:
            delta = {}

        finish_reason = "stop" if done else None

        return {
            "id": f"chatcmpl-{task_id}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": chunk.get("model") or request_body.get("model") or "",
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }
            ],
        }
