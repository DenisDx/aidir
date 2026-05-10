"""
Ollama-compatible HTTP endpoint.
Implements POST /api/chat (MVP1 scope only).
Creates Task_agent objects, enqueues them, waits for result, returns
an Ollama-compatible JSON or NDJSON streaming response.

TODO: add /api/generate, /api/tags, /api/ps, /api/show endpoints.
TODO: apply middleware chain before task creation.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from core.endpoint import BaseEndpoint
from core.error_logging import attach_request_id_middleware, get_or_create_request_id, log_exception
from core.task_types.task_agent import Task_agent
from core.task import STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELED
from core import log

if TYPE_CHECKING:
    from core.app import Core

# HTTP error codes per MVP1 contract
_HTTP_BUSY    = 503
_HTTP_INVALID = 400
_HTTP_NO_WRKR = 422
_HTTP_UPSTREAM= 502
_HTTP_TIMEOUT = 504


class Endpoint_ollama(BaseEndpoint):
    """Endpoint implementing Ollama REST API (subset for MVP1)."""

    api = "ollama"

    def __init__(self, endpoint_cfg: dict) -> None:
        self.id = endpoint_cfg.get("id", "ollama")
        self._cfg = endpoint_cfg
        self._core: "Core | None" = None
        # Timeout for entire endpoint request (queue + execution)
        self._request_timeout = int(endpoint_cfg.get("request_timeout", 100))

    async def initialize(self, core: "Core") -> None:
        self._core = core
        log("http", "info", f"Endpoint {self.id} initialized", self.id)

    def create_app(self, core: "Core") -> FastAPI:
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
            return await self._handle_chat(request)

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        return app

    # ── Request handling ──────────────────────────────────────────────────────

    async def _handle_chat(self, request: Request) -> StreamingResponse | JSONResponse:
        """Handle POST /api/chat."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"error": {"code": "INVALID_REQUEST", "message": "Invalid JSON body"}},
                status_code=_HTTP_INVALID,
            )

        stream: bool = bool(body.get("stream", False))

        # Build task
        task = Task_agent(
            payload=body,
            stream=stream,
            external=True,
        )
        # Honour explicit worker override from request body (extended syntax)
        if "worker" in body:
            task.worker_id = body["worker"]
        elif self._cfg.get("worker"):
            task.worker_id = self._cfg["worker"]

        # Apply timeouts from config
        cfg_tasks = self._core.config.get("tasks", {}) or {}
        task.queue_timeout = int(cfg_tasks.get("queue_timeout", 300))
        task.run_timeout   = int(cfg_tasks.get("run_timeout", 300))

        log("http", "info",
            f"POST /api/chat task={task.id} model={body.get('model','')} stream={stream}",
            self.id)

        # Enqueue
        try:
            await self._core.on_task_added(task)
        except Exception as exc:
            error_code = getattr(exc, "code", "QUEUE_ERROR")
            log("http", "error", f"Failed to enqueue task {task.id}: {exc}", self.id)
            return JSONResponse(
                {"error": {"code": error_code, "message": str(exc)}},
                status_code=_HTTP_BUSY,
            )

        if stream:
            return StreamingResponse(
                self._stream_response(task),
                media_type="application/x-ndjson",
            )
        else:
            return await self._sync_response(task)

    async def _sync_response(self, task: Task_agent) -> JSONResponse:
        """Wait for task completion and return a single JSON response."""
        # Use timeout from task if set by request, otherwise use endpoint default
        timeout = task.run_timeout if task.run_timeout > 0 else self._request_timeout
        try:
            await asyncio.wait_for(task._done_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            await self._core.queue.mark_canceled(task)
            asyncio.create_task(self._core.delete_task(task.id))
            return JSONResponse(
                {"error": {"code": "TIMEOUT", "message": "Request timed out"}},
                status_code=_HTTP_TIMEOUT,
            )

        asyncio.create_task(self._core.delete_task(task.id))

        if task.status == STATUS_COMPLETED:
            return JSONResponse(task.result or {})
        if task.status == STATUS_FAILED:
            return JSONResponse(
                {"error": task.error or {"message": "Worker error"}},
                status_code=_HTTP_UPSTREAM,
            )
        # canceled
        return JSONResponse(
            {"error": {"code": "CANCELED", "message": "Task was canceled"}},
            status_code=_HTTP_BUSY,
        )

    async def _stream_response(self, task: Task_agent) -> AsyncGenerator[bytes, None]:
        """Read chunks from task queue and yield as NDJSON lines."""
        try:
            # Use timeout from task if set by request, otherwise use endpoint default
            timeout = task.run_timeout if task.run_timeout > 0 else self._request_timeout
            deadline = asyncio.get_event_loop().time() + timeout

            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    # Timeout: cancel task and yield error chunk
                    await self._core.queue.mark_canceled(task)
                    yield (json.dumps({
                        "error": {"code": "TIMEOUT", "message": "Request timed out"},
                        "done": True,
                    }) + "\n").encode()
                    break

                try:
                    chunk = await asyncio.wait_for(
                        task._chunk_queue.get(), timeout=min(remaining, 5.0)
                    )
                except asyncio.TimeoutError:
                    continue

                if chunk is None:
                    # Sentinel: stream is finished; yield final done marker if needed
                    break

                yield (json.dumps(chunk) + "\n").encode()
        finally:
            asyncio.create_task(self._core.delete_task(task.id))
