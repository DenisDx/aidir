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
from core.task import STATUS_CANCELED, STATUS_COMPLETED, STATUS_FAILED


class Endpoint_openaix(Endpoint_ollama):
    """Endpoint implementing OpenAI-compatible API plus limited /api/chat."""

    api = "openaix"

    def __init__(self, endpoint_cfg: dict) -> None:
        super().__init__(endpoint_cfg)
        # When enabled, responses follow protocol-specific error envelopes
        # (OpenAI format for /v1/* and Ollama format for /api/*).
        self._errors_compatibility_mode = bool(
            endpoint_cfg.get("errors_compatibility_mode", True)
        )
        # Timeout for entire endpoint request (queue + execution)
        self._request_timeout = int(endpoint_cfg.get("request_timeout", 100))

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
        task = self._build_task_for_payload(body, stream)

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
        ollama_payload = self._openai_request_to_ollama(body)
        task = self._build_task_for_payload(ollama_payload, stream)

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
        from core.task_types.task_agent import Task_agent

        task = Task_agent(payload=payload, stream=stream, external=True)
        if "worker" in payload:
            task.worker_id = payload["worker"]
        elif self._cfg.get("worker"):
            task.worker_id = self._cfg["worker"]

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
            "tools",
            "tool_choice",
            "temperature",
            "top_p",
            "max_tokens",
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
        """Collect unique model ids from configured providers."""
        providers = self._core.config.get("models.providers") or {}
        out: list[str] = []
        seen: set[str] = set()

        for provider in providers.values():
            models = provider.get("models", []) if isinstance(provider, dict) else []
            for model in models:
                if not isinstance(model, dict):
                    continue
                model_id = model.get("id") or model.get("name")
                if not model_id:
                    continue
                model_id = str(model_id)
                if model_id in seen:
                    continue
                seen.add(model_id)
                out.append(model_id)

        return out

    def _openai_models_response(self) -> dict:
        """Build OpenAI-compatible models list response."""
        now = int(time.time())
        return {
            "object": "list",
            "data": [
                {
                    "id": model_id,
                    "object": "model",
                    "created": now,
                    "owned_by": "aidir",
                }
                for model_id in self._collect_models()
            ],
        }

    def _ollama_tags_response(self) -> dict:
        """Build limited Ollama-compatible /api/tags response."""
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return {
            "models": [
                {
                    "name": model_id,
                    "model": model_id,
                    "modified_at": now_iso,
                    "size": 0,
                    "digest": "",
                    "details": {},
                }
                for model_id in self._collect_models()
            ]
        }

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
