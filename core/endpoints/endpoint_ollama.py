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
import hmac
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
        self._model_resolution_warnings_emitted: set[str] = set()

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

        auth_error = self._authorize_and_apply_envid(request, body)
        if auth_error is not None:
            return auth_error

        stream: bool = bool(body.get("stream", False))

        # Build task
        task = self._build_task_for_payload(body, stream)

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

    def _authorize_and_apply_envid(self, request: Request, body: dict) -> JSONResponse | None:
        """Validate API token, check envid access, and apply user autoassign_envid."""
        token = self._extract_bearer_token(request)
        if not token:
            return None

        user = self._find_api_user_by_token(token)
        if user is None:
            return JSONResponse(
                {"error": {"code": "UNAUTHORIZED", "message": "Invalid API token"}},
                status_code=401,
            )

        requested_envid = body.get("envid")
        if requested_envid in (None, ""):
            autoassign = user.get("autoassign_envid")
            if isinstance(autoassign, str) and autoassign.strip():
                body["envid"] = autoassign.strip()
                requested_envid = body["envid"]

        if requested_envid in (None, ""):
            return None

        requested_envid = str(requested_envid)
        if not self._is_envid_allowed_for_user(requested_envid, user):
            return JSONResponse(
                {"error": {"code": "FORBIDDEN", "message": f"envid '{requested_envid}' is not allowed for this API user"}},
                status_code=403,
            )

        if self._core and self._core.envid_registry and self._core.envid_registry.get(requested_envid) is None:
            return JSONResponse(
                {"error": {"code": "INVALID_ENVID", "message": f"envid '{requested_envid}' does not exist"}},
                status_code=400,
            )

        body["envid"] = requested_envid
        return None

    @staticmethod
    def _extract_bearer_token(request: Request) -> str:
        """Extract Bearer token from Authorization header."""
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return ""
        return auth_header[7:].strip()

    def _find_api_user_by_token(self, token: str) -> dict | None:
        """Find API user from users.items by matching token."""
        users_cfg = self._core.config.get("users", {}) if self._core else {}
        items = users_cfg.get("items") if isinstance(users_cfg, dict) else None

        # Backward compatibility for legacy list format.
        if isinstance(users_cfg, list):
            items = users_cfg

        if not isinstance(items, list):
            return None

        for item in items:
            if not isinstance(item, dict):
                continue
            raw_token = item.get("token")
            if not isinstance(raw_token, str):
                continue
            if hmac.compare_digest(raw_token, token):
                return item
        return None

    @staticmethod
    def _is_envid_allowed_for_user(envid: str, user: dict) -> bool:
        """Return True when user has access to the requested envid."""
        allowed = user.get("envids", []) if isinstance(user, dict) else []
        if not isinstance(allowed, list):
            return False
        normalized = {str(item).strip() for item in allowed if str(item).strip()}
        return envid in normalized

    def _build_task_for_payload(self, payload: dict, stream: bool) -> Task_agent:
        """Create Task_agent and resolve model/provider route using alias-aware rules."""
        worker_id = self._resolve_worker_id(payload)
        route = self._resolve_model_route((payload or {}).get("model"), worker_id)
        if route is not None:
            payload = dict(payload or {})
            payload["model"] = route["resolved_model"]

        task = Task_agent(
            payload=payload,
            stream=stream,
            external=True,
        )
        if worker_id:
            task.worker_id = worker_id
        if route is not None:
            task.config = dict(task.config or {})
            task.config["route"] = route
        return task

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

    def _preferred_provider_for_worker(self, worker_id: str) -> str:
        """Return preferred provider configured for the selected worker."""
        if not worker_id or self._core is None:
            return ""
        provider_id = self._core.config.get(f"workers.items.{worker_id}.provider")
        return str(provider_id).strip() if provider_id is not None else ""

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
