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
import base64
import hmac
import json
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, AsyncGenerator
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from core.endpoint import BaseEndpoint
from core.error_logging import attach_request_id_middleware, get_or_create_request_id, log_exception
from core.smart_router import SmartRouteError, SmartRouter
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
        self._warn_deprecated_request_timeout(endpoint_cfg)
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
        incoming_bearer_token = self._extract_bearer_token(request)
        route_trace = self._build_incoming_route_trace(request.headers)
        route_trace_error = self._validate_incoming_route_trace(route_trace)
        if route_trace_error is not None:
            code, status_code, message = route_trace_error
            return JSONResponse({"error": {"code": code, "message": message}}, status_code=status_code)

        # Build task
        try:
            task = await self._build_task_for_payload_async(
                body,
                stream,
                incoming_bearer_token=incoming_bearer_token,
                route_trace=route_trace,
            )
        except SmartRouteError as exc:
            return JSONResponse(
                {"error": {"code": exc.code, "message": str(exc)}},
                status_code=exc.status_code,
            )

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
    ) -> Task_agent:
        """Create Task_agent and resolve smart routes before queueing when needed."""
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

    def _create_task_for_payload(
        self,
        payload: dict,
        stream: bool,
        worker_id: str,
        route: dict | None,
    ) -> Task_agent:
        """Finalize Task_agent creation once worker and route are resolved."""
        if route is not None:
            payload = dict(payload or {})
            payload["model"] = route["resolved_model"]

        effective_worker_id = self._resolve_worker_id_for_route(worker_id, route)

        task = Task_agent(
            payload=payload,
            stream=stream,
            external=True,
        )
        if effective_worker_id:
            task.worker_id = effective_worker_id
        if route is not None:
            task.config = dict(task.config or {})
            task.config["route"] = route
        return task

    def _resolve_worker_id_for_route(self, worker_id: str, route: dict | None) -> str:
        """Adjust worker selection when the resolved provider requires a different execution path."""
        if not isinstance(route, dict):
            return worker_id

        resolved_worker = str(route.get("resolved_worker") or "").strip()
        if resolved_worker:
            return resolved_worker

        provider_id = str(route.get("resolved_provider") or "").strip()
        provider_api = self._provider_api(provider_id)
        if provider_api != "openaix":
            return worker_id

        routed_worker_id = self._resolve_worker_id_by_name("openaix")
        return routed_worker_id or worker_id

    def _resolve_worker_id_by_name(self, worker_id: str) -> str:
        """Return a configured worker id when it exists, otherwise an empty string."""
        if not worker_id or self._core is None:
            return ""
        workers_cfg = self._core.config.get("workers.items") or {}
        if isinstance(workers_cfg, dict) and worker_id in workers_cfg:
            return worker_id
        return ""

    async def _resolve_smart_route(
        self,
        route: dict,
        *,
        worker_id: str,
        payload: dict,
        incoming_bearer_token: str = "",
    ) -> dict:
        """Resolve one smart alias route into a concrete provider/model pair."""
        resolution = await self._smart_router(worker_id).resolve_route(
            route,
            request_payload=payload,
            request_priority=SmartRouter.resolve_request_priority(payload),
            incoming_bearer_token=incoming_bearer_token,
        )
        return resolution.route

    def _smart_router(self, worker_id: str) -> SmartRouter:
        """Build the shared smart-router helper bound to this endpoint environment."""
        async def get_local_queue_state(requirements: dict, priority: int) -> dict | None:
            if self._core is None or self._core.queue is None or self._core.resources is None:
                return None
            return await self._core.queue.get_resource_queue_state(requirements, priority=priority)

        def check_resource_available(requirements: dict) -> bool:
            if self._core is None or self._core.resources is None:
                return False
            return bool(self._core.resources.check_available(requirements))

        def check_resource_available_after_unload(requirements: dict) -> bool:
            if self._core is None or self._core.resources is None:
                return False
            return bool(self._core.resources.check_available_after_unload(requirements))

        return SmartRouter(
            endpoint_id=self.id,
            default_worker_id=worker_id,
            find_provider_model_cfg=self._find_provider_model_cfg,
            provider_api=self._provider_api,
            resolve_model_resource_requirements=self._resolve_model_resource_requirements,
            get_local_queue_state=get_local_queue_state,
            check_resource_available=check_resource_available,
            check_resource_available_after_unload=check_resource_available_after_unload,
            probe_remote_model_queue_state=self._probe_remote_model_queue_state,
            probe_ollama_model_availability=self._probe_ollama_model_availability,
            resolve_probe_timeout_ms=self._resolve_probe_timeout_ms,
            resolve_worker_id_for_route=self._resolve_worker_id_for_route,
            on_selection=self._log_smart_route_selection,
            on_failure=self._log_smart_route_failure,
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
        return await self._smart_router("").evaluate_candidate(
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
        return SmartRouter.build_route_result(
            route,
            candidate,
            strategy=strategy,
            reason=reason,
            candidate_probes=candidate_probes,
        )

    @staticmethod
    def _candidate_probe_record(index: int, item: object, *, probe_ok: bool | None = None, reason: str = "", candidate: dict | None = None) -> dict:
        """Build a compact probe record for route observability and task metadata."""
        return SmartRouter.candidate_probe_record(
            index,
            item,
            probe_ok=probe_ok,
            reason=reason,
            candidate=candidate,
        )

    def _log_smart_route_selection(self, route: dict) -> None:
        """Log selected smart route with candidate probe summary."""
        probes = route.get("candidate_probes") if isinstance(route, dict) else []
        log(
            "http",
            "info",
            (
                f"Smart route selected requested={route.get('requested_model')} provider={route.get('resolved_provider')} "
                f"model={route.get('resolved_model')} strategy={route.get('strategy')} "
                f"reason={route.get('selection_reason')} probes={json.dumps(probes, ensure_ascii=False)}"
            ),
            self.id,
        )

    def _log_smart_route_failure(self, route: dict, *, strategy: str, candidate_probes: list[dict]) -> None:
        """Log smart-route failure with candidate probe summary."""
        log(
            "http",
            "warning",
            (
                f"Smart route failed requested={route.get('requested_model') or route.get('resolved_model')} "
                f"strategy={strategy} probes={json.dumps(candidate_probes, ensure_ascii=False)}"
            ),
            self.id,
        )

    @staticmethod
    def _resolve_request_priority(payload: dict | None) -> int:
        """Resolve routing priority from payload, defaulting to the normal task priority."""
        return SmartRouter.resolve_request_priority(payload)

    def _current_instance_id(self) -> str:
        """Return the configured instance id used for inter-aidir route tracing."""
        if self._core is None:
            return "aidir"
        value = self._core.config.get("instance", "aidir")
        return str(value or "aidir").strip() or "aidir"

    def _route_trace_max_hops(self) -> int:
        """Return the configured maximum number of inter-aidir hops."""
        if self._core is None:
            return 8
        try:
            parsed = int(self._core.config.get("routing.max_hops", 8))
        except Exception:
            parsed = 8
        return max(1, parsed)

    @staticmethod
    def _split_visited_instances(raw_value: object) -> list[str]:
        """Parse the visited-instance trace header into a normalized list."""
        if not isinstance(raw_value, str):
            return []
        items: list[str] = []
        seen: set[str] = set()
        for part in raw_value.split(","):
            value = part.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            items.append(value)
        return items

    def _build_incoming_route_trace(self, headers) -> dict:
        """Build internal route-trace metadata from incoming HTTP headers."""
        route_id = str(headers.get("X-Aidir-Route-Id", "") or "").strip() or str(uuid.uuid4())
        visited_instances = self._split_visited_instances(headers.get("X-Aidir-Visited-Instances", ""))
        return {
            "route_id": route_id,
            "visited_instances": visited_instances,
            "current_instance": self._current_instance_id(),
            "max_hops": self._route_trace_max_hops(),
        }

    @staticmethod
    def _validate_incoming_route_trace(route_trace: dict) -> tuple[str, int, str] | None:
        """Reject loops and overlong visited-instance chains on incoming requests."""
        if not isinstance(route_trace, dict):
            return None
        current_instance = str(route_trace.get("current_instance") or "").strip()
        visited_instances = route_trace.get("visited_instances") if isinstance(route_trace.get("visited_instances"), list) else []
        max_hops = int(route_trace.get("max_hops") or 8)

        if current_instance and current_instance in visited_instances:
            return ("ROUTING_LOOP", 409, f"Routing loop detected for instance '{current_instance}'")
        if len(visited_instances) >= max_hops:
            return ("ROUTING_HOPS_EXCEEDED", 409, f"Routing hop limit exceeded ({max_hops})")
        return None

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

    def _resolve_probe_headers(self, provider_id: str, incoming_bearer_token: str = "") -> dict[str, str]:
        """Resolve auth headers for remote smart probes using provider override or pass-through bearer."""
        headers = self._build_auth_headers(self._provider_cfg(provider_id).get("auth") or {})
        if headers:
            return headers
        if isinstance(incoming_bearer_token, str) and incoming_bearer_token.strip():
            return {"Authorization": f"Bearer {incoming_bearer_token.strip()}"}
        return {}

    @staticmethod
    def _build_auth_headers(auth_cfg: dict) -> dict[str, str]:
        """Build HTTP headers from auth config."""
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

    def _resolve_probe_timeout_ms(self, item: dict) -> int:
        """Resolve per-candidate remote probe timeout in milliseconds."""
        try:
            parsed = int(item.get("request_timeout_ms", 1500))
        except Exception:
            parsed = 1500
        return max(1, parsed)

    async def _probe_remote_model_queue_state(
        self,
        provider_id: str,
        model_id: str,
        *,
        priority: int,
        timeout_ms: int,
        incoming_bearer_token: str = "",
    ) -> dict | None:
        """Probe remote OpenAIx model-only queue-state using dual-route compatibility."""
        provider_cfg = self._provider_cfg(provider_id)
        base_url = str(provider_cfg.get("baseUrl") or "").rstrip("/")
        if not base_url:
            return None

        encoded_model = quote(str(model_id), safe="")
        candidate_urls = [
            f"{base_url}/v1/models/{encoded_model}/queue-state",
            f"{base_url}/api/models/{encoded_model}/queue-state",
        ]
        headers = self._resolve_probe_headers(provider_id, incoming_bearer_token)
        timeout_seconds = max(0.001, timeout_ms / 1000.0)

        try:
            async with httpx.AsyncClient(timeout=timeout_seconds, headers=headers) as client:
                for url in candidate_urls:
                    try:
                        response = await client.get(url, params={"priority": priority})
                    except httpx.TimeoutException:
                        return None
                    except httpx.HTTPError:
                        continue

                    if response.status_code < 200 or response.status_code >= 300:
                        continue

                    try:
                        payload = response.json()
                    except Exception:
                        continue
                    if isinstance(payload, dict):
                        return payload
        except httpx.HTTPError:
            return None

        return None

    async def _probe_ollama_model_availability(
        self,
        provider_id: str,
        model_id: str,
        *,
        timeout_ms: int,
        incoming_bearer_token: str = "",
    ) -> bool:
        """Confirm that an Ollama provider responds and reports the requested model in /api/tags."""
        provider_cfg = self._provider_cfg(provider_id)
        base_url = str(provider_cfg.get("baseUrl") or "").rstrip("/")
        if not base_url:
            return False

        headers = self._resolve_probe_headers(provider_id, incoming_bearer_token)
        timeout_seconds = max(0.001, timeout_ms / 1000.0)

        try:
            async with httpx.AsyncClient(timeout=timeout_seconds, headers=headers) as client:
                response = await client.get(f"{base_url}/api/tags")
        except httpx.HTTPError:
            return False

        if response.status_code < 200 or response.status_code >= 300:
            return False

        try:
            payload = response.json()
        except Exception:
            return False

        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            return False

        normalized_model_id = str(model_id or "").strip()
        for model in models:
            if not isinstance(model, dict):
                continue
            candidate_names = {
                str(model.get("name") or "").strip(),
                str(model.get("model") or "").strip(),
                str(model.get("id") or "").strip(),
            }
            if normalized_model_id in candidate_names:
                return True
        return False

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

    def _resolve_model_resource_requirements(self, provider_id: str, model_id: str) -> dict | None:
        """Resolve configured resource requirements for a provider/model pair."""
        provider_cfg = self._provider_cfg(provider_id)
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

    @staticmethod
    def _normalize_resource_requirements(resources: dict | None) -> dict[str, dict[str, int]]:
        """Normalize configured resource requirements into integer dictionaries."""
        if not isinstance(resources, dict):
            return {}

        out: dict[str, dict[str, int]] = {}
        for resource_id, metrics in resources.items():
            if not isinstance(metrics, dict):
                continue
            normalized_metrics: dict[str, int] = {}
            for metric_name, amount in metrics.items():
                try:
                    normalized_metrics[str(metric_name)] = int(amount)
                except Exception:
                    continue
            if normalized_metrics:
                out[str(resource_id)] = normalized_metrics
        return out

    async def _sync_response(self, task: Task_agent) -> JSONResponse:
        """Wait for task completion and return a single JSON response."""
        timeout_phase = await self._wait_for_task_terminal(task)
        if timeout_phase is not None:
            await self._terminate_task_on_timeout(task)
            return JSONResponse(
                {"error": {"code": "TIMEOUT", "message": "Request timed out"}},
                status_code=_HTTP_TIMEOUT,
            )

        asyncio.create_task(self._core.delete_task(task.id))

        if task.status == STATUS_COMPLETED:
            return JSONResponse(task.result or {})
        if task.status == STATUS_FAILED:
            error = task.error or {"message": "Worker error"}
            if error.get("code") in {"TIMEOUT", "QUEUE_TIMEOUT"}:
                return JSONResponse(
                    {"error": {"code": error.get("code"), "message": error.get("message", "Request timed out")}},
                    status_code=_HTTP_TIMEOUT,
                )
            return JSONResponse(
                {"error": error},
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
            while True:
                timeout_phase, remaining = self._task_timeout_phase(task)
                if remaining is not None and remaining <= 0:
                    await self._terminate_task_on_timeout(task)
                    yield (json.dumps({
                        "error": {"code": "TIMEOUT", "message": "Request timed out"},
                        "done": True,
                    }) + "\n").encode()
                    break

                try:
                    wait_timeout = 1.0 if remaining is None else max(0.01, min(remaining, 1.0))
                    chunk = await asyncio.wait_for(task._chunk_queue.get(), timeout=wait_timeout)
                except asyncio.TimeoutError:
                    continue

                if chunk is None:
                    # Sentinel: stream is finished; yield final done marker if needed
                    break

                yield (json.dumps(chunk) + "\n").encode()
        finally:
            asyncio.create_task(self._core.delete_task(task.id))
