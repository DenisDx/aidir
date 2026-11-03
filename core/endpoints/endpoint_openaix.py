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
import base64
import json
import re
import socket
import struct
import time
from datetime import datetime, timezone
from typing import AsyncGenerator
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from core import log
from core.error_logging import attach_request_id_middleware, get_or_create_request_id, log_exception
from core.endpoints.endpoint_ollama import Endpoint_ollama
from core.generation_options import GENERATION_OPTION_FIELDS
from core.request_limits import RequestBodyLimitMiddleware
from core.smart_router import SmartRouteError, SmartRouter
from core.task import STATUS_CANCELED, STATUS_COMPLETED, STATUS_FAILED, STATUS_RUNNING
from core.upstream_errors import upstream_error_payload, upstream_error_response
from core.upstream_response import UpstreamChunk, original_chunk, original_payload, original_response


class Endpoint_openaix(Endpoint_ollama):
    """Endpoint implementing OpenAI-compatible API plus limited /api/chat."""

    api = "openaix"
    _GENERATION_OPTION_FIELDS = GENERATION_OPTION_FIELDS

    def __init__(self, endpoint_cfg: dict) -> None:
        super().__init__(endpoint_cfg)
        # When enabled, responses follow protocol-specific error envelopes
        # (OpenAI format for /v1/* and Ollama format for /api/*).
        self._errors_compatibility_mode = bool(
            endpoint_cfg.get("errors_compatibility_mode", True)
        )
        self._peer_resources_auth_mode = self._peer_auth_mode(endpoint_cfg.get("peer_resources"))
        self._peer_tasks_auth_mode = self._peer_auth_mode(endpoint_cfg.get("peer_tasks"))
        self._model_resolution_warnings_emitted: set[str] = set()

    def create_app(self, core) -> FastAPI:
        """Create FastAPI app exposing both ollama and openai chat endpoints."""
        self._core = core
        default_auth_mode = self._default_peer_auth_mode()
        self._peer_resources_auth_mode = self._peer_auth_mode(
            self._cfg.get("peer_resources"), default_auth_mode
        )
        self._peer_tasks_auth_mode = self._peer_auth_mode(
            self._cfg.get("peer_tasks"), default_auth_mode
        )
        app = FastAPI(title=f"aidir-{self.id}", docs_url=None, redoc_url=None)
        app.add_middleware(
            RequestBodyLimitMiddleware,
            max_request_size=core.config.get("http.max_request_size", 104857600),
            audit_log=getattr(core, "audit_log", None),
        )
        attach_request_id_middleware(app)
        self._attach_ingress_logging(app)

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

        @app.post("/api/embed")
        async def api_embed(request: Request):
            return await self._handle_embed(request, protocol="ollama")

        @app.get("/api/tags")
        async def api_tags():
            return self._ollama_tags_response()

        @app.post("/api/show")
        async def api_show(request: Request):
            return await self._handle_ollama_show(request)

        @app.post("/v1/chat/completions")
        async def openai_chat_completions(request: Request):
            return await self._handle_openai_chat(request)

        @app.post("/v1/embeddings")
        async def openai_embeddings(request: Request):
            return await self._handle_embed(request, protocol="openai")

        @app.get("/v1/models")
        async def openai_models():
            return self._openai_models_response()

        @app.get("/v1/resources")
        async def openai_resources(request: Request):
            return self._peer_resources_response(request)

        @app.get("/v1/resources/{resource_id}")
        async def openai_resource(resource_id: str, request: Request):
            return self._peer_resources_response(request, resource_id)

        @app.get("/api/resources")
        async def ollama_resources(request: Request):
            return self._peer_resources_response(request)

        @app.get("/api/resources/{resource_id}")
        async def ollama_resource(resource_id: str, request: Request):
            return self._peer_resources_response(request, resource_id)

        @app.get("/v1/tasks")
        async def openai_tasks(request: Request):
            return self._peer_tasks_response(request)

        @app.post("/v1/tasks/{task_id}/terminate")
        async def peer_terminate_task(task_id: str, request: Request):
            auth_error = self._peer_access_error(request, self._peer_tasks_auth_mode, "peer tasks")
            if auth_error is not None:
                return auth_error
            task_hash = await self._core.terminate_task(task_id) if self._core is not None else None
            if task_hash is None:
                return JSONResponse({"error": "task not found"}, status_code=404)
            log("http", "warn", f"{self.id} peer termination task={task_id}", self.id)
            return JSONResponse({"ok": True, "task_id": task_id})

        @app.get("/v1/providers/{provider_id}/models/{model_id:path}/queue-state")
        async def openai_model_queue_state(provider_id: str, model_id: str, priority: int = 5, request: Request = None):
            return await self._safe_queue_state(
                "openai",
                self._model_queue_state_response(
                    protocol="openai",
                    provider_id=provider_id,
                    model_id=model_id,
                    priority=priority,
                    request=request,
                ),
            )

        @app.get("/v1/models/{model_id:path}/queue-state")
        async def openai_model_only_queue_state(model_id: str, priority: int = 5, request: Request = None):
            return await self._safe_queue_state(
                "openai",
                self._model_only_queue_state_response(
                    protocol="openai",
                    model_id=model_id,
                    priority=priority,
                    request=request,
                ),
            )

        @app.get("/api/providers/{provider_id}/models/{model_id:path}/queue-state")
        async def ollama_model_queue_state(provider_id: str, model_id: str, priority: int = 5, request: Request = None):
            return await self._safe_queue_state(
                "ollama",
                self._model_queue_state_response(
                    protocol="ollama",
                    provider_id=provider_id,
                    model_id=model_id,
                    priority=priority,
                    request=request,
                ),
            )

        @app.get("/api/models/{model_id:path}/queue-state")
        async def ollama_model_only_queue_state(model_id: str, priority: int = 5, request: Request = None):
            return await self._safe_queue_state(
                "ollama",
                self._model_only_queue_state_response(
                    protocol="ollama",
                    model_id=model_id,
                    priority=priority,
                    request=request,
                ),
            )

        @app.get("/v1/models/{model_id:path}")
        async def openai_model(model_id: str):
            # Declared after the queue-state routes so that ".../queue-state"
            # is never swallowed by the path converter.
            try:
                return self._openai_model_response(model_id)
            except Exception as exc:
                log("http", "error", f"Model state request failed: {exc}", self.id)
                return self._error_response(
                    protocol="openai",
                    status_code=503,
                    code="MODEL_STATE_UNAVAILABLE",
                    message="Model state is temporarily unavailable",
                )

        @app.get("/health")
        async def health():
            try:
                return self._health_payload()
            except Exception as exc:
                log("http", "error", f"Health request failed: {exc}", self.id)
                return self._error_response(
                    protocol="openai",
                    status_code=503,
                    code="HEALTH_UNAVAILABLE",
                    message="Health snapshot is temporarily unavailable",
                )

        @app.get("/stats")
        async def stats():
            # Compatibility route for clients that probe service statistics.
            return self._service_stats()

        @app.get("/metrics")
        async def metrics():
            # Prometheus-compatible scrape target.
            return Response(
                content=self._prometheus_metrics(),
                media_type="text/plain; version=0.0.4; charset=utf-8",
            )

        return app

    # ── Health, model state and queue state ───────────────────────────────────

    @staticmethod
    def _local_host() -> str:
        """Return this host's LAN address, falling back to loopback."""
        try:
            addresses = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        except OSError:
            addresses = []
        for address in addresses:
            ip = str(address[4][0])
            if not ip.startswith("127."):
                return ip
        # A hostname that resolves only to loopback (the common /etc/hosts
        # short-name case); ask the routing table instead.
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("8.8.8.8", 80))
                return str(sock.getsockname()[0])
        except OSError:
            return "127.0.0.1"

    def _provider_host(self, provider_id: str) -> str:
        """Return the host that serves one provider."""
        provider = self._provider_cfg(provider_id)
        base_url = str(provider.get("baseUrl") or "").strip()
        if base_url:
            host = (urlparse(base_url).hostname or "").strip()
            if host and host not in {"127.0.0.1", "localhost", "0.0.0.0", "::1"}:
                return host
        return self._local_host()

    def _resource_provider(self, resource_id: str) -> str:
        """Return the provider configured for one resource, if any."""
        resources = getattr(self._core, "resources", None)
        getter = getattr(resources, "get", None)
        if not callable(getter):
            return ""
        resource = getter(resource_id)
        return str(getattr(resource, "provider", "") or "")

    def _resource_snapshots(self) -> list[dict]:
        """Return resource snapshots without triggering any probe."""
        resources = getattr(self._core, "resources", None)
        if resources is None or not hasattr(resources, "snapshot"):
            return []
        return [snap for snap in resources.snapshot() if isinstance(snap, dict)]

    def _model_config_info(self, provider_id: str, model_id: str) -> dict:
        """Return configured model metadata used by the health and model routes."""
        model_cfg = self._find_provider_model_cfg(provider_id, model_id) or {}
        context_tokens = (
            model_cfg.get("contextWindow")
            or model_cfg.get("context_tokens")
            or model_cfg.get("max_context_tokens")
        )
        quantization = model_cfg.get("quantization") or model_cfg.get("quantization_level")
        return {
            "context_tokens": context_tokens,
            "quantization": str(quantization) if quantization else "",
        }

    @staticmethod
    def _task_route_info(task: object) -> tuple[str, str, str]:
        """Return the provider, model and requested model of one live task."""
        config = getattr(task, "config", None)
        config = config if isinstance(config, dict) else {}
        route = config.get("route") if isinstance(config.get("route"), dict) else {}
        payload = getattr(task, "payload", None)
        payload = payload if isinstance(payload, dict) else {}
        provider_id = str(
            getattr(task, "route_provider_id", "")
            or route.get("resolved_provider")
            or ""
        )
        model_id = str(
            getattr(task, "model_id", "")
            or route.get("resolved_model")
            or payload.get("model")
            or ""
        )
        requested = str(
            route.get("requested_alias")
            or route.get("requested_model")
            or payload.get("model")
            or ""
        )
        return provider_id, model_id, requested

    def _task_queue_summary(
        self,
        provider_id: str | None = None,
        model_id: str | None = None,
        requested_model: str | None = None,
    ) -> dict:
        """Summarize live tasks for one model or the whole service from in-memory state."""
        queue = getattr(self._core, "queue", None)
        tasks = queue.list_tasks() if queue is not None and hasattr(queue, "list_tasks") else []
        by_status: dict[str, int] = {}
        by_priority: dict[str, int] = {}
        for task in tasks:
            task_provider, task_model, task_requested = self._task_route_info(task)
            if provider_id is not None and task_provider != provider_id:
                continue
            if model_id is not None and task_model != model_id:
                continue
            if requested_model is not None and not (
                task_requested == requested_model
                or (task_provider == provider_id and task_model == model_id)
            ):
                continue
            status = str(getattr(task, "status", "unknown"))
            by_status[status] = by_status.get(status, 0) + 1
            priority = str(getattr(task, "priority", ""))
            by_priority[priority] = by_priority.get(priority, 0) + 1
        active = by_status.get("running", 0)
        queued = by_status.get("queued", 0) + by_status.get("created", 0)
        live = sum(
            count for status, count in by_status.items()
            if status in {"created", "queued", "running"}
        )
        return {
            "active": active,
            "queued": queued,
            "total": live,
            "by_status": by_status,
            "by_priority": by_priority,
        }

    def _health_tasks(self) -> list[dict]:
        """Return the active task list for the health snapshot without request bodies."""
        queue = getattr(self._core, "queue", None)
        tasks = queue.list_tasks() if queue is not None and hasattr(queue, "list_tasks") else []
        now = datetime.now(timezone.utc)

        def _aware(value):
            """Return an aware UTC datetime for a task timestamp."""
            if not isinstance(value, datetime):
                return None
            return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

        def _ms_between(start, end) -> int | None:
            """Return a non-negative millisecond difference, or None."""
            if not isinstance(start, datetime) or not isinstance(end, datetime):
                return None
            return max(0, int((end - start).total_seconds() * 1000))

        def _remaining_ms(limit_seconds, elapsed_ms) -> int | None:
            """Return the remaining budget in milliseconds, or None for no limit."""
            if int(limit_seconds or 0) <= 0 or elapsed_ms is None:
                return None
            return max(0, int(limit_seconds) * 1000 - int(elapsed_ms))

        payload: list[dict] = []
        for task in tasks:
            status = str(getattr(task, "status", "") or "")
            if status not in {"created", "queued", "running"}:
                continue
            provider_id, model_id, requested_model = self._task_route_info(task)
            created_at = _aware(getattr(task, "created_at", None))
            updated_at = _aware(getattr(task, "updated_at", None))
            started_at = _aware(getattr(task, "started_at", None))
            client = (getattr(task, "config", None) or {}).get("client") if isinstance(getattr(task, "config", None), dict) else None
            client = client if isinstance(client, dict) else {}
            external = bool(getattr(task, "external", False) or client.get("external"))
            wait_ms = _ms_between(created_at, started_at or now)
            run_ms = _ms_between(started_at, now) if status == "running" else None
            queue_timeout = int(getattr(task, "queue_timeout", 0) or 0)
            run_timeout = int(getattr(task, "run_timeout", 0) or 0)

            def _iso(value):
                """Return an ISO timestamp or an empty string."""
                return value.isoformat() if hasattr(value, "isoformat") else ""

            payload.append({
                "id": str(getattr(task, "id", "") or ""),
                "type": str(getattr(task, "type", "") or ""),
                "status": status,
                "worker_id": str(getattr(task, "worker_id", "") or ""),
                "priority": int(getattr(task, "priority", 0) or 0),
                "provider": provider_id,
                "model": model_id,
                "requested_model": requested_model,
                "client": str(client.get("login") or ("internal" if not external else "anonymous")),
                "client_source": str(client.get("source") or ("internal" if not external else "anonymous")),
                "client_host": str(client.get("host") or ""),
                "external": external,
                "envid": str(getattr(task, "envid", "") or client.get("envid") or ""),
                "created_at": _iso(created_at),
                "updated_at": _iso(updated_at),
                "started_at": _iso(started_at),
                "wait_ms": wait_ms,
                "run_ms": run_ms,
                "queue_timeout": queue_timeout,
                "run_timeout": run_timeout,
                "queue_remaining_ms": _remaining_ms(queue_timeout, wait_ms),
                "run_remaining_ms": _remaining_ms(run_timeout, run_ms),
            })
        payload.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
        return payload

    @staticmethod
    def _merge_usage(target: dict, usage: object) -> None:
        """Add one usage mapping into a metric accumulator."""
        if not isinstance(usage, dict):
            return
        for metric, value in usage.items():
            try:
                amount = int(value)
            except (TypeError, ValueError):
                continue
            if amount:
                target[str(metric)] = target.get(str(metric), 0) + amount

    def _active_task_consumers(self) -> dict[str, tuple[str, str]]:
        """Map active consumer ids to their provider/model pair."""
        queue = getattr(self._core, "queue", None)
        tasks = queue.list_tasks() if queue is not None and hasattr(queue, "list_tasks") else []
        out: dict[str, tuple[str, str]] = {}
        for task in tasks:
            if str(getattr(task, "status", "")) != "running":
                continue
            provider_id, model_id, _requested = self._task_route_info(task)
            if not provider_id or not model_id:
                continue
            worker_id = str(getattr(task, "worker_id", "") or "")
            out[f"{getattr(task, 'id', '')}:{worker_id}"] = (provider_id, model_id)
        return out

    def _model_used_on_resource(self, snapshot: dict, provider_id: str, model_id: str) -> dict:
        """Return the amount of one resource currently held by one model."""
        used: dict[str, int] = {}
        base_id = f"base:{provider_id}:{model_id}"
        active = self._active_task_consumers()
        for consumer in snapshot.get("consumers") or []:
            if not isinstance(consumer, dict):
                continue
            consumer_id = str(consumer.get("id") or "")
            if consumer_id == base_id or active.get(consumer_id) == (provider_id, model_id):
                self._merge_usage(used, consumer.get("usage"))
        for entry in snapshot.get("soft_consumers") or []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("model_id") or "") != model_id:
                continue
            entry_provider = str(entry.get("provider_id") or "")
            if entry_provider and entry_provider != provider_id:
                continue
            self._merge_usage(used, entry.get("resources"))
        return used

    def _resource_usage_entries(
        self,
        provider_id: str,
        model_id: str,
        requirements: dict[str, dict[str, int]],
        snapshots: list[dict] | None = None,
    ) -> list[dict]:
        """Return per-resource total/free/used-by-model/needed figures."""
        snapshots = snapshots if snapshots is not None else self._resource_snapshots()
        by_id = {str(snap.get("id") or ""): snap for snap in snapshots}
        entries: list[dict] = []
        for resource_id, needed in (requirements or {}).items():
            snapshot = by_id.get(resource_id)
            if not isinstance(snapshot, dict):
                entries.append({
                    "id": resource_id,
                    "total": {},
                    "free": {},
                    "used_by_model": {},
                    "needed": dict(needed or {}),
                    "units": {},
                    "model_loaded": False,
                })
                continue
            limits = dict(snapshot.get("limits") or {})
            used = dict(snapshot.get("used") or {})
            soft = dict(snapshot.get("soft_used") or {})
            availability = snapshot.get("availability") or {}
            free = dict(availability.get("available") or {})
            if not free:
                free = {
                    metric: max(0, int(limit) - int(used.get(metric, 0)) - int(soft.get(metric, 0)))
                    for metric, limit in limits.items()
                }
            used_by_model = self._model_used_on_resource(snapshot, provider_id, model_id)
            entries.append({
                "id": resource_id,
                "total": limits,
                "free": free,
                "used": used,
                "soft_used": soft,
                "used_by_model": used_by_model,
                "needed": dict(needed or {}),
                "units": dict(snapshot.get("units") or {}),
                "model_loaded": bool(used_by_model),
                "availability": {
                    "source": availability.get("source"),
                    "status": availability.get("status"),
                    "observed_at": availability.get("observed_at"),
                    "error": availability.get("error"),
                },
            })
        return entries

    def _concrete_model_state(
        self,
        provider_id: str,
        model_id: str,
        *,
        requested_model: str | None = None,
        requirements: dict[str, dict[str, int]] | None = None,
        snapshots: list[dict] | None = None,
    ) -> dict:
        """Build the aidir extension payload for one concrete provider/model pair."""
        if requirements is None:
            requirements = self._resolve_model_resource_requirements(provider_id, model_id) or {}
        resources = getattr(self._core, "resources", None)
        evaluator = getattr(resources, "evaluate_model_admission", None)
        admission = (
            evaluator(requirements, model_id, provider_id)
            if callable(evaluator)
            else None
        )
        if not isinstance(admission, dict):
            admission = {
                "admission": "unknown",
                "admissible_now": False,
                "admissible_after_unload": False,
                "model_loaded": False,
                "resource_ready": False,
                "resource_ready_after_unload": False,
                "active_count": 0,
                "max_concurrent_requests": 1,
                "session_resources": {},
            }
        queue = self._task_queue_summary(provider_id, model_id)
        usage = self._resource_usage_entries(provider_id, model_id, requirements, snapshots)
        model_loaded = bool(admission.get("model_loaded")) or any(
            entry.get("model_loaded") for entry in usage
        )
        active_count = int(admission.get("active_count") or 0)
        max_concurrent = int(admission.get("max_concurrent_requests") or 1)
        admission_value = admission.get("admission")
        if model_loaded:
            # A loaded model already owns its base memory. Admission can still
            # report ``base_memory`` when its soft-consumer fingerprint does not
            # match, but the model is present and can serve another request
            # unless the concurrency limit is reached.
            resource_ready = True
            resource_ready_after_unload = True
            can_run_now = active_count < max_concurrent
            admission_value = "ready" if can_run_now else "concurrency"
        else:
            resource_ready = bool(admission.get("resource_ready"))
            resource_ready_after_unload = bool(admission.get("resource_ready_after_unload"))
            can_run_now = bool(admission.get("admissible_now")) and not queue["queued"]
        return {
            "schema_version": 1,
            "requested_model": requested_model or model_id,
            "provider": provider_id,
            "model": model_id,
            "resources": usage,
            "state": {
                "can_run_now": can_run_now,
                "resource_ready": resource_ready,
                "resource_ready_after_unload": resource_ready_after_unload,
                "model_loaded": model_loaded,
                "admission": admission_value,
                "active_count": int(admission.get("active_count") or 0),
                "max_concurrent_requests": int(admission.get("max_concurrent_requests") or 1),
                "session_resources": admission.get("session_resources") or {},
                "queue": queue,
            },
        }

    def _context_identification_enabled(self) -> bool:
        """Return whether smart models use context-based candidate identification."""
        config = getattr(self._core, "config", None)
        getter = getattr(config, "get", None)
        affinity = getter("routing.kv_affinity") if callable(getter) else None
        return bool(isinstance(affinity, dict) and affinity.get("enabled"))

    def _smart_model_state(self, requested_model: str, smart_cfg: dict) -> dict:
        """Build the aidir extension payload for one smart model without probing."""
        items = smart_cfg.get("items") if isinstance(smart_cfg, dict) else None
        items = [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        snapshots = self._resource_snapshots()
        candidates: list[dict] = []
        aggregate_queue = {"active": 0, "queued": 0, "total": 0, "by_status": {}, "by_priority": {}}
        for index, item in enumerate(items):
            provider_id = str(item.get("provider") or "").strip()
            model_id = str(item.get("model") or "").strip()
            if not provider_id or not model_id:
                continue
            try:
                fallback_prio = int(item.get("fallback_prio", index))
            except (TypeError, ValueError):
                fallback_prio = index
            candidate = self._concrete_model_state(
                provider_id,
                model_id,
                requested_model=requested_model,
                snapshots=snapshots,
            )
            candidate["fallback_prio"] = fallback_prio
            candidate["index"] = index
            candidates.append(candidate)
            for key in ("active", "queued", "total"):
                aggregate_queue[key] += int(candidate["state"]["queue"].get(key, 0))
            for status, count in candidate["state"]["queue"].get("by_status", {}).items():
                aggregate_queue["by_status"][status] = (
                    aggregate_queue["by_status"].get(status, 0) + int(count)
                )
            for priority, count in candidate["state"]["queue"].get("by_priority", {}).items():
                aggregate_queue["by_priority"][priority] = (
                    aggregate_queue["by_priority"].get(priority, 0) + int(count)
                )

        candidates.sort(key=lambda item: (item["fallback_prio"], item["index"]))
        fallback = candidates[0] if candidates else {}
        fallback_state = fallback.get("state") or {}
        state = {
            "can_run_now": any(item["state"].get("can_run_now") for item in candidates),
            "resource_ready": any(item["state"].get("resource_ready") for item in candidates),
            "resource_ready_after_unload": any(
                item["state"].get("resource_ready_after_unload") for item in candidates
            ),
            "model_loaded": any(item["state"].get("model_loaded") for item in candidates),
            "admission": fallback_state.get("admission"),
            "active_count": sum(int(item["state"].get("active_count") or 0) for item in candidates),
            "max_concurrent_requests": fallback_state.get("max_concurrent_requests", 1),
            "session_resources": fallback_state.get("session_resources") or {},
            "queue": aggregate_queue,
        }
        return {
            "schema_version": 1,
            "requested_model": requested_model,
            "provider": "smart",
            "model": requested_model,
            "resolved_provider": fallback.get("provider"),
            "resolved_model": fallback.get("model"),
            "resources": fallback.get("resources") or [],
            "state": state,
            "smart": {
                "context_identification": self._context_identification_enabled(),
                "fallback_route": {
                    "provider": fallback.get("provider"),
                    "model": fallback.get("model"),
                },
                "candidates": [
                    {
                        "provider": item.get("provider"),
                        "model": item.get("model"),
                        "fallback_prio": item.get("fallback_prio"),
                        "can_run_now": item["state"].get("can_run_now"),
                        "resource_ready": item["state"].get("resource_ready"),
                        "model_loaded": item["state"].get("model_loaded"),
                        "resources": item.get("resources") or [],
                        "queue": item["state"].get("queue"),
                    }
                    for item in candidates
                ],
            },
        }

    def _model_extension(self, requested_model: str) -> dict | None:
        """Return the aidir extension payload for an externally visible model id."""
        route = self._resolve_model_route(requested_model, self._resolve_worker_id({}))
        if not isinstance(route, dict):
            return None
        provider_id = str(route.get("resolved_provider") or "")
        model_id = str(route.get("resolved_model") or "")
        if not provider_id or not model_id:
            return None
        if self._provider_api(provider_id) == "smart":
            smart_cfg = self._find_provider_model_cfg(provider_id, model_id)
            if not isinstance(smart_cfg, dict):
                smart_cfg = self._find_provider_model_cfg(provider_id, requested_model) or {}
            payload = self._smart_model_state(requested_model, smart_cfg)
            payload["real_id"] = model_id
            payload["resolved_provider"] = payload.get("resolved_provider") or provider_id
            payload["resolved_model"] = payload.get("resolved_model") or model_id
            return payload
        payload = self._concrete_model_state(provider_id, model_id, requested_model=requested_model)
        payload["real_id"] = model_id
        payload["resolved_provider"] = provider_id
        payload["resolved_model"] = model_id
        payload["route"] = {
            "selection": route.get("selection"),
            "protocol": route.get("resolved_protocol"),
            "requested_alias": route.get("requested_alias"),
            "worker_preferred_provider": route.get("worker_preferred_provider"),
        }
        return payload

    def _openai_model_response(self, model_id: str) -> Response:
        """Return one model per the OpenAI models shape plus an aidir extension block."""
        extension = self._model_extension(model_id)
        if extension is None:
            return JSONResponse(
                self._openai_error_payload_with_type(
                    "model_not_found",
                    f"Unknown model: {model_id}",
                    error_type="invalid_request_error",
                    error_param="model",
                ),
                status_code=404,
            )
        return JSONResponse({
            "id": model_id,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "aidir",
            "schema_version": 1,
            "aidir": extension,
        })

    def _snapshot_loaded_models(self, snapshot: dict) -> list[dict]:
        """Return loaded models known for one resource, local and remote."""
        entries: dict[tuple[str, str], dict] = {}
        for item in snapshot.get("soft_consumers") or []:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("model_id") or "").strip()
            if not model_id:
                continue
            provider_id = str(item.get("provider_id") or "").strip()
            key = (provider_id, model_id)
            info = self._model_config_info(provider_id, model_id)
            entries[key] = {
                "id": model_id,
                "provider": provider_id,
                "resources": dict(item.get("resources") or {}),
                "persistent": bool(item.get("persistent")),
                "state": "loaded" if item.get("persistent") else "idle",
                "context_tokens": info["context_tokens"],
                "quantization": info["quantization"],
            }
        active = self._active_task_consumers()
        for consumer in snapshot.get("consumers") or []:
            if not isinstance(consumer, dict):
                continue
            consumer_id = str(consumer.get("id") or "")
            if consumer_id.startswith("base:"):
                parts = consumer_id.split(":", 2)
                if len(parts) != 3 or not parts[2]:
                    continue
                provider_id, model_id = parts[1], parts[2]
                source = active.get(consumer_id)
                state = "loaded"
                persistent = True
            else:
                source = active.get(consumer_id)
                if source is None:
                    continue
                provider_id, model_id = source
                state = "running"
                persistent = False
            info = self._model_config_info(provider_id, model_id)
            entries[(provider_id, model_id)] = {
                "id": model_id,
                "provider": provider_id,
                "resources": dict(consumer.get("usage") or {}),
                "persistent": persistent,
                "state": state,
                "context_tokens": info["context_tokens"],
                "quantization": info["quantization"],
            }
        for item in snapshot.get("peer_loaded_models") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            provider_id = str(item.get("provider") or "").strip()
            model_id = str(item.get("id") or "").strip()
            info = self._model_config_info(provider_id, model_id)
            entries[(provider_id, model_id)] = {
                "id": model_id,
                "provider": provider_id,
                "resources": dict(item.get("resources") or {}),
                "persistent": bool(item.get("persistent")),
                "state": str(item.get("state") or "loaded"),
                "context_tokens": item.get("context_tokens") or info["context_tokens"],
                "quantization": info["quantization"],
            }
        return list(entries.values())

    def _all_sensor_readings(self, snapshot: dict) -> list[dict]:
        """Merge monitoring and telemetry readings, preferring telemetry entries."""
        readings: dict[str, dict] = {}
        monitoring = snapshot.get("monitoring")
        if isinstance(monitoring, dict):
            for item in monitoring.get("sensors") or []:
                if isinstance(item, dict) and item.get("id"):
                    readings[str(item["id"])] = dict(item)
        telemetry = snapshot.get("telemetry")
        if isinstance(telemetry, dict):
            for item in telemetry.get("sensors") or []:
                if isinstance(item, dict) and item.get("id"):
                    readings[str(item["id"])] = dict(item)
        return list(readings.values())

    def _health_resource(self, snapshot: dict) -> dict:
        """Build one health resource entry from a runtime snapshot."""
        provider_id = self._resource_provider(str(snapshot.get("id") or ""))
        active_used = dict(snapshot.get("used") or {})
        soft_used = dict(snapshot.get("soft_used") or {})
        total_used = {
            metric: int(active_used.get(metric, 0)) + int(soft_used.get(metric, 0))
            for metric in set(active_used) | set(soft_used)
        }
        return {
            "id": str(snapshot.get("id") or ""),
            "type": str(snapshot.get("type") or ""),
            "provider": provider_id,
            "host": self._provider_host(provider_id) if provider_id else self._local_host(),
            "devices": list(snapshot.get("devices") or []),
            "limits": dict(snapshot.get("limits") or {}),
            "units": dict(snapshot.get("units") or {}),
            "used": total_used,
            "active_used": active_used,
            "soft_used": soft_used,
            "availability": dict(snapshot.get("availability") or {}),
            "telemetry": {"sensors": self._all_sensor_readings(snapshot)},
            "loaded_models": self._snapshot_loaded_models(snapshot),
        }

    def _health_models(self, resources: list[dict]) -> list[dict]:
        """Return configured models that declare resources, with their current state."""
        loaded = {
            (str(item.get("provider") or ""), str(item.get("id") or ""))
            for resource in resources
            for item in resource.get("loaded_models") or []
        }
        snapshots = self._resource_snapshots()
        models: list[dict] = []
        for provider_id, model_cfg in self._iter_provider_models():
            requirements = model_cfg.get("resources")
            if not isinstance(requirements, dict) or not requirements:
                continue
            model_id = self._model_id(model_cfg)
            if not model_id:
                continue
            info = self._model_config_info(provider_id, model_id)
            state = self._concrete_model_state(
                provider_id,
                model_id,
                requirements=self._normalize_resource_requirements(requirements),
                snapshots=snapshots,
            )
            models.append({
                "id": self._external_model_id(model_cfg),
                "model": model_id,
                "provider": provider_id,
                "resources": dict(requirements),
                "context_tokens": info["context_tokens"],
                "quantization": info["quantization"],
                "loaded": (provider_id, model_id) in loaded,
                "state": state["state"],
            })

        providers = self._config_get("models.providers") or {}
        if isinstance(providers, dict):
            for provider_id, provider in providers.items():
                if not isinstance(provider, dict) or str(provider.get("api") or "") != "smart":
                    continue
                for model_cfg in provider.get("models") or []:
                    if not isinstance(model_cfg, dict):
                        continue
                    model_id = self._model_id(model_cfg)
                    if not model_id:
                        continue
                    candidate_resources: dict[str, dict[str, int]] = {}
                    items = model_cfg.get("items") if isinstance(model_cfg.get("items"), list) else []
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        item_provider = str(item.get("provider") or "")
                        item_model = str(item.get("model") or "")
                        requirement = self._resolve_model_resource_requirements(
                            item_provider,
                            item_model,
                        ) or {}
                        for resource_id, metrics in requirement.items():
                            bucket = candidate_resources.setdefault(resource_id, {})
                            for metric, amount in metrics.items():
                                bucket[metric] = max(bucket.get(metric, 0), int(amount))
                    if not candidate_resources:
                        continue
                    state = self._smart_model_state(model_id, model_cfg)
                    info = self._model_config_info(provider_id, model_id)
                    models.append({
                        "id": self._external_model_id(model_cfg),
                        "model": model_id,
                        "provider": provider_id,
                        "resolved_provider": state.get("resolved_provider"),
                        "resolved_model": state.get("resolved_model"),
                        "resources": candidate_resources,
                        "context_tokens": info["context_tokens"],
                        "quantization": info["quantization"],
                        "loaded": bool(state["state"].get("model_loaded")),
                        "state": state["state"],
                        "smart": state.get("smart"),
                    })
        return models

    def _external_provider_statuses(self, resources: list[dict]) -> list[dict]:
        """Return configured external providers with their last-known reachability."""
        providers = self._config_get("models.providers") or {}
        if not isinstance(providers, dict):
            return []
        statuses = []
        for provider_id, provider in providers.items():
            if not isinstance(provider, dict) or provider_id == "smart":
                continue
            api = str(provider.get("api") or "")
            host = self._provider_host(str(provider_id))
            local = host == self._local_host() and api in {"ollama", "llama-cpp"}
            if local:
                continue
            related = [resource for resource in resources if resource.get("provider") == provider_id]
            availability = [resource.get("availability") or {} for resource in related]
            ok = any(item.get("status") == "ok" for item in availability)
            failed = bool(availability) and not ok
            auth_error = any(
                "authoriz" in str(item.get("error") or "").lower()
                or "401" in str(item.get("error") or "")
                or "403" in str(item.get("error") or "")
                for item in availability
            )
            statuses.append({
                "provider": provider_id,
                "api": api,
                "host": host,
                "kind": "peer" if api == "openaix" else "external",
                "reachable": True if ok else (False if failed else None),
                "auth": "ok" if ok else ("error" if auth_error else None),
                "resources": [resource.get("id") for resource in related],
                "models": len(provider.get("models") or []) if isinstance(provider.get("models"), list) else 0,
            })
        return statuses

    def _health_payload(self) -> dict:
        """Return the full service health snapshot with resources, models and telemetry."""
        snapshots = self._resource_snapshots()
        resources = [self._health_resource(snapshot) for snapshot in snapshots]
        runtime = {}
        reader = getattr(self._core, "get_runtime_status", None)
        if callable(reader):
            runtime = reader()
        tasks_cfg = self._config_get("tasks") or {}
        if isinstance(tasks_cfg, dict):
            runtime = dict(runtime)
            runtime["timeouts"] = {
                "queue_timeout_seconds": int(tasks_cfg.get("queue_timeout", 300) or 300),
                "run_timeout_seconds": int(tasks_cfg.get("run_timeout", 300) or 300),
                "restart_wait_timeout_seconds": int(tasks_cfg.get("restart_wait_timeout", 120) or 120),
            }
        return {
            "status": "ok",
            "schema_version": 1,
            "instance": str(self._core.config.get("instance", "aidir") or "aidir"),
            "resources": resources,
            "models": self._health_models(resources),
            "telemetry": [
                {
                    "resource": resource.get("id"),
                    "provider": resource.get("provider"),
                    "host": resource.get("host"),
                    "sensors": (resource.get("telemetry") or {}).get("sensors", []),
                }
                for resource in resources
            ],
            "external_providers": self._external_provider_statuses(resources),
            "queue": self._task_queue_summary(),
            "tasks": self._health_tasks(),
            "runtime": runtime,
        }

    def _service_stats(self) -> dict:
        """Return a JSON snapshot of service counters for the stats route."""
        core = self._core
        tasks = list(core.queue.list_tasks()) if getattr(core, "queue", None) else []
        by_status: dict[str, int] = {}
        for task in tasks:
            status = str(getattr(task, "status", "unknown"))
            by_status[status] = by_status.get(status, 0) + 1

        scheduler = getattr(core, "scheduler", None)
        active = (
            scheduler.active_task_count()
            if scheduler is not None
            else by_status.get(STATUS_RUNNING, 0)
        )
        workers = getattr(core, "workers", {}) or {}
        resources = core.resources.snapshot() if getattr(core, "resources", None) else []
        started_at = getattr(core, "started_at", None)
        uptime = max(0.0, time.time() - started_at) if started_at else 0.0

        return {
            "object": "aidir.stats",
            "schema_version": 1,
            "instance": str(core.config.get("instance", "aidir") or "aidir"),
            "uptime_seconds": round(uptime, 3),
            "tasks": {
                "total": len(tasks),
                "active": active,
                "by_status": by_status,
            },
            "workers": {
                "total": len(workers),
                "enabled": sum(1 for worker in workers.values() if getattr(worker, "enabled", False)),
            },
            "resources": {"total": len(resources)},
        }

    @staticmethod
    def _prometheus_escape(value: str) -> str:
        """Escape a Prometheus label value."""
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    def _prometheus_metrics(self) -> str:
        """Render the service counters in Prometheus text exposition format."""
        stats = self._service_stats()
        tasks = stats["tasks"]
        lines = [
            "# HELP aidir_schema_version Version of the aidir extension schema.",
            "# TYPE aidir_schema_version gauge",
            "aidir_schema_version 1",
            "# HELP aidir_up Whether the aidir endpoint is serving requests.",
            "# TYPE aidir_up gauge",
            "aidir_up 1",
            "# HELP aidir_uptime_seconds Service uptime in seconds.",
            "# TYPE aidir_uptime_seconds gauge",
            f"aidir_uptime_seconds {stats['uptime_seconds']}",
            "# HELP aidir_tasks_total Live tasks by status.",
            "# TYPE aidir_tasks_total gauge",
        ]
        for status, count in sorted(tasks["by_status"].items()):
            lines.append(
                f'aidir_tasks_total{{status="{self._prometheus_escape(status)}"}} {count}'
            )
        lines.extend([
            "# HELP aidir_tasks_active Tasks currently executing.",
            "# TYPE aidir_tasks_active gauge",
            f"aidir_tasks_active {tasks['active']}",
            "# HELP aidir_workers_total Configured workers.",
            "# TYPE aidir_workers_total gauge",
            f"aidir_workers_total {stats['workers']['total']}",
            "# HELP aidir_workers_enabled Enabled workers.",
            "# TYPE aidir_workers_enabled gauge",
            f"aidir_workers_enabled {stats['workers']['enabled']}",
            "# HELP aidir_resources_total Configured resources.",
            "# TYPE aidir_resources_total gauge",
            f"aidir_resources_total {stats['resources']['total']}",
        ])
        return "\n".join(lines) + "\n"

    def _peer_resources_response(self, request: Request, resource_id: str | None = None) -> JSONResponse:
        """Return sanitized peer resource state after applying configured access policy."""
        auth_error = self._peer_access_error(request, self._peer_resources_auth_mode, "peer resources")
        if auth_error is not None:
            return auth_error

        resources = getattr(self._core, "resources", None)
        if resources is None:
            return self._error_response(
                protocol="openai",
                status_code=503,
                code="RESOURCES_UNAVAILABLE",
                message="Resources are not available",
            )

        if resource_id is not None:
            resource = resources.get(resource_id)
            if resource is None:
                return self._error_response(
                    protocol="openai",
                    status_code=404,
                    code="RESOURCE_NOT_FOUND",
                    message=f"Unknown resource: {resource_id}",
                )
            return JSONResponse(self._peer_resource_payload(resource.snapshot()))

        return JSONResponse({
            "object": "list",
            "protocol_version": 1,
            "data": [self._peer_resource_payload(resource.snapshot()) for resource in resources.all()],
        })

    def _peer_tasks_response(self, request: Request) -> JSONResponse:
        """Return active tasks for read-only cross-instance dashboard visibility."""
        auth_error = self._peer_access_error(request, self._peer_tasks_auth_mode, "peer tasks")
        if auth_error is not None:
            return auth_error

        queue = getattr(self._core, "queue", None)
        tasks = queue.list_tasks() if queue is not None and hasattr(queue, "list_tasks") else []
        active = {"created", "queued", "running"}
        payload = [
            self._peer_task_payload(task)
            for task in tasks
            if str(getattr(task, "status", "")).lower() in active
        ]
        payload.sort(
            key=lambda item: item.get("updated_at") or item.get("created_at") or "",
            reverse=True,
        )
        return JSONResponse({
            "object": "list",
            "protocol_version": 1,
            "instance": str(self._core.config.get("instance", "aidir") or "aidir"),
            "data": payload[:1000],
        })

    def _peer_access_error(
        self,
        request: Request,
        auth_mode: str,
        subject: str,
    ) -> JSONResponse | None:
        """Apply the optional endpoint auth policy shared by peer read-only APIs."""
        if auth_mode != "endpoint":
            return None
        if not self._extract_bearer_token(request):
            return self._error_response(
                protocol="openai",
                status_code=401,
                code="UNAUTHORIZED",
                message=f"Bearer authentication is required for {subject}",
            )
        return self._authorize_and_apply_envid(request, {})

    @staticmethod
    def _peer_task_payload(task) -> dict:
        """Build one minimal read-only task row without request or response bodies."""
        config = getattr(task, "config", None)
        config = config if isinstance(config, dict) else {}
        route = config.get("route") if isinstance(config.get("route"), dict) else {}
        trace = config.get("route_trace") if isinstance(config.get("route_trace"), dict) else {}
        payload = getattr(task, "payload", None)
        payload = payload if isinstance(payload, dict) else {}

        def _iso(value):
            """Return an ISO timestamp or an empty string."""
            return value.isoformat() if hasattr(value, "isoformat") else ""

        return {
            "id": str(getattr(task, "id", "") or ""),
            "type": str(getattr(task, "type", "") or ""),
            "status": str(getattr(task, "status", "") or ""),
            "worker_id": str(getattr(task, "worker_id", "") or ""),
            "priority": int(getattr(task, "priority", 0) or 0),
            "created_at": _iso(getattr(task, "created_at", None)),
            "updated_at": _iso(getattr(task, "updated_at", None)),
            "started_at": _iso(getattr(task, "started_at", None)),
            "finished_at": _iso(getattr(task, "finished_at", None)),
            "route_provider_id": str(
                getattr(task, "route_provider_id", "")
                or route.get("resolved_provider")
                or ""
            ),
            "model_id": str(
                getattr(task, "model_id", "")
                or route.get("resolved_model")
                or payload.get("model")
                or ""
            ),
            "requested_model": str(
                route.get("requested_alias")
                or route.get("requested_model")
                or ""
            ),
            "route_id": str(trace.get("route_id") or ""),
            "llm_call_count": int(getattr(task, "llm_call_count", 0) or 0),
            "envid": str(getattr(task, "envid", "") or ""),
            "parent_worker": str(getattr(task, "parent_worker", "") or ""),
            "request_preview": str(getattr(task, "request_preview", "") or "")[:300],
        }

    def _peer_resource_payload(self, snapshot: dict) -> dict:
        """Build one minimal peer-safe resource response from a runtime snapshot."""
        telemetry = snapshot.get("telemetry")
        readings = telemetry.get("sensors") if isinstance(telemetry, dict) else None
        loaded_models = []
        for entry in snapshot.get("soft_consumers") or []:
            if not isinstance(entry, dict):
                continue
            model_id = str(entry.get("model_id") or "").strip()
            if not model_id:
                continue
            loaded_models.append({
                "id": model_id,
                "provider": str(entry.get("provider_id") or ""),
                "resources": dict(entry.get("resources") or {}),
                "persistent": bool(entry.get("persistent")),
                "state": "loaded",
            })
        active = self._active_task_consumers()
        for consumer in snapshot.get("consumers") or []:
            if not isinstance(consumer, dict):
                continue
            consumer_id = str(consumer.get("id") or "")
            if consumer_id.startswith("base:"):
                parts = consumer_id.split(":", 2)
                if len(parts) != 3 or not parts[2]:
                    continue
                provider_id, model_id = parts[1], parts[2]
                state = "loaded"
                persistent = True
            else:
                source = active.get(consumer_id)
                if source is None:
                    continue
                provider_id, model_id = source
                state = "running"
                persistent = False
            loaded_models.append({
                "id": model_id,
                "provider": provider_id,
                "resources": dict(consumer.get("usage") or {}),
                "persistent": persistent,
                "state": state,
            })
        return {
            "id": str(snapshot.get("id") or ""),
            "type": str(snapshot.get("type") or ""),
            "limits": dict(snapshot.get("limits") or {}),
            "units": dict(snapshot.get("units") or {}),
            "availability": dict(snapshot.get("availability") or {}),
            "loaded_models": loaded_models,
            "telemetry": {
                "sensors": [
                    {
                        key: reading.get(key)
                        for key in ("id", "label", "unit", "status", "value", "updated_at", "error")
                    } | (
                        {"devices": reading["devices"]}
                        if isinstance(reading.get("devices"), list)
                        else {}
                    )
                    for reading in readings
                    if isinstance(reading, dict)
                ]
            } if isinstance(readings, list) else {},
        }

    async def _handle_chat(self, request: Request) -> Response:
        """Handle Ollama-compatible /api/chat and queue the openaix worker."""
        try:
            body, raw_body = await self._read_json_body(request)
        except Exception:
            self._audit_pre_task_rejection(
                request,
                protocol="ollama",
                status_code=400,
                error_code="INVALID_REQUEST",
                reason="Invalid JSON body",
            )
            return self._error_response(
                protocol="ollama",
                status_code=400,
                code="INVALID_REQUEST",
                message="Invalid JSON body",
            )

        auth_error = self._authorize_and_apply_envid(request, body)
        if auth_error is not None:
            self._audit_rejection_response(request, "ollama", auth_error, "Authorization or envid validation failed")
            return auth_error

        stream = bool(body.get("stream", False))
        incoming_bearer_token = self._extract_bearer_token(request)
        route_trace = self._build_incoming_route_trace(request.headers)
        route_trace_error = self._validate_incoming_route_trace(route_trace)
        if route_trace_error is not None:
            code, status_code, message = route_trace_error
            self._audit_pre_task_rejection(request, protocol="ollama", status_code=status_code, error_code=code, reason=message)
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
                request=request,
            )
        except SmartRouteError as exc:
            self._audit_pre_task_rejection(request, protocol="ollama", status_code=exc.status_code, error_code=exc.code, reason=str(exc))
            return self._error_response(
                protocol="ollama",
                status_code=exc.status_code,
                code=exc.code,
                message=str(exc),
            )

        self._audit_client_request(task, request, body, protocol="ollama", raw_body=raw_body)

        try:
            await self._core.on_task_added(task)
        except Exception as exc:
            self._audit_pre_task_rejection(
                request,
                protocol="ollama",
                status_code=503,
                error_code=getattr(exc, "code", "QUEUE_ERROR"),
                reason=str(exc),
            )
            return self._error_response(
                protocol="ollama",
                status_code=503,
                code=getattr(exc, "code", "QUEUE_ERROR"),
                message=str(exc),
                task_id=task.id,
            )

        if stream:
            return await self._streaming_response(task)
        response = await self._sync_response(task)
        self._audit_client_response(task, response)
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

    async def _handle_openai_chat(self, request: Request) -> Response:
        """Handle OpenAI chat completions request and map it to Task_agent flow."""
        try:
            body, raw_body = await self._read_json_body(request)
        except Exception:
            self._audit_pre_task_rejection(
                request,
                protocol="openai",
                status_code=400,
                error_code="invalid_request_error",
                reason="Invalid JSON body",
            )
            return self._error_response(
                protocol="openai",
                status_code=400,
                code="invalid_request_error",
                message="Invalid JSON body",
            )

        auth_error = self._authorize_and_apply_envid(request, body)
        if auth_error is not None:
            self._audit_rejection_response(request, "openai", auth_error, "Authorization or envid validation failed")
            return auth_error

        stream = bool(body.get("stream", False))
        incoming_bearer_token = self._extract_bearer_token(request)
        route_trace = self._build_incoming_route_trace(request.headers)
        route_trace_error = self._validate_incoming_route_trace(route_trace)
        if route_trace_error is not None:
            code, status_code, message = route_trace_error
            self._audit_pre_task_rejection(request, protocol="openai", status_code=status_code, error_code=code, reason=message)
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
                request=request,
            )
        except SmartRouteError as exc:
            self._audit_pre_task_rejection(request, protocol="openai", status_code=exc.status_code, error_code=exc.code, reason=str(exc))
            return self._error_response(
                protocol="openai",
                status_code=exc.status_code,
                code=exc.code,
                message=str(exc),
            )

        self._audit_client_request(task, request, body, protocol="openai", raw_body=raw_body)

        try:
            await self._core.on_task_added(task)
        except Exception as exc:
            self._audit_pre_task_rejection(
                request,
                protocol="openai",
                status_code=503,
                error_code=getattr(exc, "code", "QUEUE_ERROR"),
                reason=str(exc),
            )
            return self._error_response(
                protocol="openai",
                status_code=503,
                code=getattr(exc, "code", "server_error"),
                message=str(exc),
                task_id=task.id,
            )

        if stream:
            return await self._openai_streaming_response(task, body)

        response = await self._openai_sync_response(task, body)
        self._audit_client_response(task, response)
        return response

    async def _handle_embed(self, request: Request, *, protocol: str) -> Response:
        """Handle one non-streaming Ollama or OpenAI embedding request."""
        try:
            body, raw_body = await self._read_json_body(request)
        except Exception:
            self._audit_pre_task_rejection(
                request,
                protocol=protocol,
                status_code=400,
                error_code="INVALID_REQUEST",
                reason="Invalid JSON body",
            )
            return self._error_response(
                protocol=protocol,
                status_code=400,
                code="INVALID_REQUEST",
                message="Invalid JSON body",
            )

        if not isinstance(body, dict):
            self._audit_pre_task_rejection(
                request,
                protocol=protocol,
                status_code=400,
                error_code="INVALID_REQUEST",
                reason="Request body must be an object",
            )
            return self._error_response(
                protocol=protocol,
                status_code=400,
                code="INVALID_REQUEST",
                message="Request body must be an object",
            )

        validation_error = self._validate_embed_request(body, protocol=protocol)
        if validation_error is not None:
            self._audit_rejection_response(request, protocol, validation_error, "Embedding request validation failed")
            return validation_error

        auth_error = self._authorize_and_apply_envid(request, body)
        if auth_error is not None:
            self._audit_rejection_response(request, protocol, auth_error, "Authorization or envid validation failed")
            return auth_error

        incoming_bearer_token = self._extract_bearer_token(request)
        route_trace = self._build_incoming_route_trace(request.headers)
        route_trace_error = self._validate_incoming_route_trace(route_trace)
        if route_trace_error is not None:
            code, status_code, message = route_trace_error
            self._audit_pre_task_rejection(request, protocol=protocol, status_code=status_code, error_code=code, reason=message)
            return self._error_response(protocol=protocol, status_code=status_code, code=code, message=message)

        payload = self._openai_embed_to_ollama(body) if protocol == "openai" else dict(body)
        payload["stream"] = False
        try:
            task = await self._build_task_for_payload_async(
                payload,
                False,
                request_kind="embed",
                incoming_bearer_token=incoming_bearer_token,
                route_trace=route_trace,
                request=request,
            )
        except SmartRouteError as exc:
            self._audit_pre_task_rejection(request, protocol=protocol, status_code=exc.status_code, error_code=exc.code, reason=str(exc))
            return self._error_response(
                protocol=protocol,
                status_code=exc.status_code,
                code=exc.code,
                message=str(exc),
            )
        except HTTPException as exc:
            self._audit_pre_task_rejection(
                request,
                protocol=protocol,
                status_code=exc.status_code,
                error_code="INVALID_REQUEST",
                reason=str(exc.detail),
            )
            return self._error_response(
                protocol=protocol,
                status_code=exc.status_code,
                code="INVALID_REQUEST",
                message=str(exc.detail),
            )

        if protocol == "openai" and "user" in body:
            task.config = dict(task.config or {})
            task.config["embedding_user"] = body["user"]

        self._audit_client_request(task, request, body, protocol=protocol, raw_body=raw_body)

        try:
            await self._core.on_task_added(task)
        except Exception as exc:
            self._audit_pre_task_rejection(
                request,
                protocol=protocol,
                status_code=503,
                error_code=getattr(exc, "code", "QUEUE_ERROR"),
                reason=str(exc),
            )
            return self._error_response(
                protocol=protocol,
                status_code=503,
                code=getattr(exc, "code", "QUEUE_ERROR"),
                message=str(exc),
                task_id=task.id,
            )

        if protocol == "ollama":
            response = await self._sync_response(task)
        else:
            response = await self._openai_embed_sync_response(task, body)
        self._audit_client_response(task, response)
        return response

    def _validate_embed_request(self, body: dict, *, protocol: str) -> JSONResponse | None:
        """Validate the shared text-only, non-streaming embedding request contract."""
        model = body.get("model")
        if not isinstance(model, str) or not model.strip():
            return self._error_response(protocol=protocol, status_code=400, code="INVALID_REQUEST", message="Field 'model' is required")

        value = body.get("input")
        valid_input = isinstance(value, str) or (
            isinstance(value, list) and all(isinstance(item, str) for item in value)
        )
        if not valid_input:
            return self._error_response(
                protocol=protocol,
                status_code=400,
                code="INVALID_REQUEST",
                message="Field 'input' must be a string or an array of strings",
            )

        if body.get("stream"):
            return self._error_response(protocol=protocol, status_code=400, code="INVALID_REQUEST", message="Embedding requests do not support streaming")

        for field in ("tools", "tool_choice", "context_builder"):
            if field in body:
                return self._error_response(
                    protocol=protocol,
                    status_code=400,
                    code="INVALID_REQUEST",
                    message=f"Field '{field}' is not supported for embedding requests",
                )
        return None

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
        request_kind: str = "chat",
        incoming_bearer_token: str = "",
        route_trace: dict | None = None,
        request: Request | None = None,
    ):
        """Create Task_agent and resolve smart routes before queueing when needed."""
        payload = self._apply_generation_defaults(payload)
        worker_id = self._resolve_worker_id(payload)
        client_key = str(request.headers.get("x-aidir-kv-key") or "").strip() if request is not None else ""
        explicit_client = ""
        if request is not None:
            raw_client = request.headers.get("x-aidir-client")
            if not raw_client and isinstance(payload, dict):
                raw_client = payload.get("client")
            explicit_client = self._normalize_client_label(raw_client)
        route = self._resolve_model_route((payload or {}).get("model"), worker_id)
        if request_kind == "embed":
            self._ensure_embedding_route(route)
        if route is not None and self._provider_api(route["resolved_provider"]) == "smart":
            routing_payload = dict(payload)
            routing_payload["request_kind"] = request_kind
            if client_key:
                routing_payload["_aidir_kv_key"] = client_key
            route = await self._resolve_smart_route(
                route,
                worker_id=worker_id,
                payload=routing_payload,
                incoming_bearer_token=incoming_bearer_token,
            )

        if request_kind == "embed":
            self._ensure_embedding_route(route)

        task = self._create_task_for_payload(payload, stream, worker_id, route, request_kind=request_kind)
        if isinstance(task.payload, dict) and "client" in task.payload:
            # The label is aidir metadata, not an upstream request field.
            task.payload = {key: value for key, value in task.payload.items() if key != "client"}
        if client_key:
            task.config = dict(task.config or {})
            task.config["kv_client_key"] = client_key
        if incoming_bearer_token:
            task.config = dict(task.config or {})
            task.config["incoming_bearer_token"] = incoming_bearer_token
        if isinstance(route_trace, dict):
            task.config = dict(task.config or {})
            task.config["route_trace"] = dict(route_trace)
        if request is not None:
            token = self._extract_incoming_token(request)
            user = self._find_api_user_by_token(token) if token else None
            task.config = dict(task.config or {})
            login = explicit_client or str((user or {}).get("login") or ("anonymous" if not token else "api-user"))
            if explicit_client:
                source = "header" if request.headers.get("x-aidir-client") else "body"
            elif user:
                source = "api_user"
            else:
                source = "anonymous"
            task.config["client"] = {
                "login": login,
                "source": source,
                "host": request.client.host if request.client is not None else "",
                "envid": str((payload or {}).get("envid") or ""),
                "external": True,
            }
            await self._attach_opencode_session_metadata(task, request)
        else:
            task.config = dict(task.config or {})
            task.config.setdefault(
                "client",
                {"login": "internal", "source": "internal", "host": "", "envid": "", "external": False},
            )
        return task

    def _create_task_for_payload(
        self,
        payload: dict,
        stream: bool,
        worker_id: str,
        route: dict | None,
        *,
        request_kind: str = "chat",
    ):
        """Finalize Task_agent creation once worker and route are resolved."""
        from core.task_types.task_agent import Task_agent

        payload = dict(payload or {})
        requested_timeout = payload.pop("timeout", None)
        requested_queue_timeout = payload.pop("queue_timeout", None)
        if route is not None:
            payload["model"] = route["resolved_model"]

        effective_worker_id = self._resolve_worker_id_for_route(worker_id, route)

        task = Task_agent(payload=payload, stream=stream, external=True)
        if effective_worker_id:
            task.worker_id = effective_worker_id
        if route is not None:
            task.config = dict(task.config or {})
            task.config["route"] = route
            task.route_provider_id = str(route.get("resolved_provider") or "")
            task.model_id = str(route.get("resolved_model") or "")
        if request_kind != "chat":
            task.config = dict(task.config or {})
            task.config["request_kind"] = request_kind

        cfg_tasks = self._core.config.get("tasks", {}) or {}
        default_queue_timeout = int(cfg_tasks.get("queue_timeout", 300))
        default_run_timeout = int(cfg_tasks.get("run_timeout", 300))
        task.run_timeout = self._resolve_timeout(
            requested_timeout,
            default_run_timeout,
        )
        if requested_queue_timeout is not None:
            task.queue_timeout = self._resolve_queue_timeout(
                requested_queue_timeout,
                default_queue_timeout,
            )
        elif requested_timeout is not None:
            queue_headroom = max(0, default_queue_timeout - default_run_timeout)
            task.queue_timeout = task.run_timeout + queue_headroom
        else:
            task.queue_timeout = default_queue_timeout
        if requested_timeout is not None:
            task.config = dict(task.config or {})
            task.config["request_timeout"] = task.run_timeout
        return task

    def _ensure_embedding_route(self, route: dict | None) -> None:
        """Require that a requested embedding route resolves to an embedding-capable model."""
        if not isinstance(route, dict):
            raise SmartRouteError(code="INVALID_MODEL", status_code=422, message="Unknown embedding model")

        provider_id = str(route.get("resolved_provider") or "").strip()
        model_id = str(route.get("resolved_model") or "").strip()
        model_cfg = self._find_provider_model_cfg(provider_id, model_id)
        if not isinstance(model_cfg, dict) or model_cfg.get("embedding") is not True:
            raise SmartRouteError(
                code="EMBEDDING_NOT_SUPPORTED",
                status_code=422,
                message=f"Model '{route.get('requested_model') or model_id}' does not support embeddings",
            )

    @staticmethod
    def _resolve_queue_timeout(value: object, default: int) -> int:
        """Return a request queue timeout or the configured default when omitted."""
        return Endpoint_openaix._resolve_timeout(value, default, "queue_timeout")

    @staticmethod
    def _resolve_timeout(value: object, default: int, field_name: str = "timeout") -> int:
        """Return a positive request timeout or its configured default.

        A request may not remove the bound: zero would mean "no limit" in the
        task model, and a task without a queue timeout would wait forever.
        Zero therefore falls back to the configured default.
        """
        if value is None:
            return default
        if isinstance(value, bool):
            raise HTTPException(status_code=400, detail=f"{field_name} must be a non-negative integer")
        try:
            timeout = int(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"{field_name} must be a non-negative integer") from exc
        if timeout < 0 or str(value).strip() not in {str(timeout), f"+{timeout}"}:
            raise HTTPException(status_code=400, detail=f"{field_name} must be a non-negative integer")
        if timeout == 0:
            return default
        return timeout

    async def _resolve_smart_route(
        self,
        route: dict,
        *,
        worker_id: str,
        payload: dict,
        incoming_bearer_token: str = "",
    ) -> dict:
        """Resolve one smart alias route into a concrete provider/model pair."""
        request_kind = str((payload or {}).get("request_kind") or "chat").strip().lower()
        router = self._smart_router(
            worker_id,
            is_candidate_allowed=self._is_embedding_candidate if request_kind == "embed" else None,
        )
        resolution = await router.resolve_route(
            route,
            request_payload=payload,
            request_priority=SmartRouter.resolve_request_priority(payload),
            incoming_bearer_token=incoming_bearer_token,
        )
        return resolution.route

    def _is_embedding_candidate(self, provider_id: str, model_id: str) -> bool:
        """Return whether one concrete smart-routing candidate supports embeddings."""
        model_cfg = self._find_provider_model_cfg(provider_id, model_id)
        return isinstance(model_cfg, dict) and model_cfg.get("embedding") is True

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
        payload = dict(body)
        payload["model"] = body.get("model", "")
        payload["messages"] = body.get("messages", [])
        payload["stream"] = bool(body.get("stream", False))

        return payload

    @staticmethod
    def _openai_embed_to_ollama(body: dict) -> dict:
        """Convert an OpenAI embedding request into the shared Ollama-shaped task payload."""
        payload = dict(body)
        payload["model"] = body.get("model", "")
        payload["input"] = body.get("input")
        payload["stream"] = False
        return payload

    async def _openai_embed_sync_response(self, task, request_body: dict) -> Response:
        """Wait for an embedding task and return an OpenAI-compatible embedding response."""
        timeout_phase = await self._wait_for_task_terminal(task)
        if timeout_phase is not None:
            await self._terminate_task_on_timeout(task)
            return self._error_response(
                protocol="openai",
                status_code=504,
                code="timeout_error",
                message="Request timed out",
                task_id=task.id,
            )

        asyncio.create_task(self._core.delete_task(task.id))
        if task.status == STATUS_COMPLETED:
            return JSONResponse(self._ollama_embed_to_openai(task.result or {}, task.id, request_body))
        if task.status == STATUS_FAILED:
            err = task.error or {}
            upstream_response = upstream_error_response(err)
            if upstream_response is not None:
                return upstream_response
            if err.get("code") in {"TIMEOUT", "QUEUE_TIMEOUT"}:
                return self._error_response(
                    protocol="openai",
                    status_code=504,
                    code="timeout_error",
                    message=err.get("message", "Request timed out"),
                    task_id=task.id,
                )
            mapped_error = self._map_openai_failed_task_error(err)
            return self._error_response(
                protocol="openai",
                status_code=mapped_error["status_code"],
                code=mapped_error["code"],
                message=mapped_error["message"],
                task_id=task.id,
                error_type=mapped_error.get("error_type"),
                error_param=mapped_error.get("error_param"),
            )
        if task.status == STATUS_CANCELED:
            return self._error_response(
                protocol="openai",
                status_code=503,
                code="server_error",
                message="Task was canceled",
                task_id=task.id,
            )
        return self._error_response(
            protocol="openai",
            status_code=500,
            code="server_error",
            message="Unknown task state",
            task_id=task.id,
        )

    @staticmethod
    def _ollama_embed_to_openai(result: dict, task_id: str, request_body: dict) -> dict:
        """Convert an Ollama `/api/embed` response into an OpenAI embedding list."""
        raw_embeddings = result.get("embeddings") if isinstance(result, dict) else None
        if not isinstance(raw_embeddings, list):
            raw_embeddings = []
        encoding_format = str((request_body or {}).get("encoding_format") or "float").lower()
        data: list[dict] = []
        for index, vector in enumerate(raw_embeddings):
            if not isinstance(vector, list):
                continue
            embedding: list[float] | str
            normalized_vector = [float(value) for value in vector]
            if encoding_format == "base64":
                raw_vector = struct.pack(f"<{len(normalized_vector)}f", *normalized_vector)
                embedding = base64.b64encode(raw_vector).decode("ascii")
            else:
                embedding = normalized_vector
            data.append({"object": "embedding", "embedding": embedding, "index": index})

        upstream_usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        prompt_tokens = result.get("prompt_eval_count") or upstream_usage.get("prompt_eval_count") or upstream_usage.get("prompt_tokens")
        total_tokens = result.get("total_tokens") or upstream_usage.get("total_tokens") or prompt_tokens
        response = {
            "object": "list",
            "data": data,
            "model": str((request_body or {}).get("model") or result.get("model") or ""),
        }
        if prompt_tokens is not None:
            response["usage"] = {"prompt_tokens": int(prompt_tokens), "total_tokens": int(total_tokens)}
        return response

    async def _openai_sync_response(self, task, request_body: dict) -> Response:
        """Wait for task completion and return OpenAI chat.completion JSON."""
        timeout_phase = await self._wait_for_task_terminal(task)
        if timeout_phase is not None:
            await self._terminate_task_on_timeout(task)
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
            upstream = original_response(task.upstream_response, "openai")
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
            if upstream is not None:
                return upstream
            result = original_payload(task.upstream_response, "ollama", result)
            return JSONResponse(self._ollama_sync_to_openai(result, task.id, request_body))
        if task.status == STATUS_FAILED:
            err = task.error or {}
            upstream_response = None if self._is_anthropic_route(task) else upstream_error_response(err)
            if upstream_response is not None:
                log("http", "warning", f"{self.id} upstream error: task={task.id} status={upstream_response.status_code} message={err.get('message')}", self.id)
                return upstream_response
            if err.get("code") in {"TIMEOUT", "QUEUE_TIMEOUT"}:
                log(
                    "http",
                    "warning",
                    (
                        f"{self.id} /v1/chat/completions timeout_failed: task={task.id} "
                        f"code={err.get('code')} message={err.get('message', 'Request timed out')}"
                    ),
                    self.id,
                )
                return self._error_response(
                    protocol="openai",
                    status_code=504,
                    code="timeout_error",
                    message=err.get("message", "Request timed out"),
                    task_id=task.id,
                )
            mapped_error = self._map_openai_failed_task_error(err)
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
                status_code=mapped_error["status_code"],
                code=mapped_error["code"],
                message=mapped_error["message"],
                task_id=task.id,
                error_type=mapped_error.get("error_type"),
                error_param=mapped_error.get("error_param"),
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

    async def _openai_stream_response(
        self,
        task,
        request_body: dict,
        first_chunk: dict | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """Stream OpenAI-compatible SSE chunks converted from Ollama chunks."""
        audit_log = getattr(self._core, "audit_log", None)
        audit_context = getattr(task, "_audit_client_context", None)
        spool = audit_log.open_body_spool() if audit_log is not None and isinstance(audit_context, dict) else None
        passthrough = False
        try:
            if first_chunk is not None:
                passthrough = original_chunk(first_chunk, "openai") is not None
                encoded = self._encode_openai_chunk(first_chunk, task.id, request_body)
                if encoded is not None:
                    yield encoded
                    if spool is not None:
                        spool.write(encoded)
            while True:
                if task.status == STATUS_COMPLETED and task._chunk_queue.empty():
                    break
                timeout_phase, remaining = self._task_timeout_phase(task)
                if remaining is not None and remaining <= 0:
                    await self._terminate_task_on_timeout(task)
                    err = self._openai_error_payload(
                        code="timeout_error",
                        message="Request timed out",
                        task_id=task.id,
                    )
                    encoded = f"data: {json.dumps(err)}\n\n".encode()
                    yield encoded
                    if spool is not None:
                        spool.write(encoded)
                    break

                try:
                    wait_timeout = 1.0 if remaining is None else max(0.01, min(remaining, 1.0))
                    chunk = await asyncio.wait_for(task._chunk_queue.get(), timeout=wait_timeout)
                except asyncio.TimeoutError:
                    continue

                if chunk is None:
                    if task.status == STATUS_FAILED:
                        error_payload = task.error or {}
                        if self._is_anthropic_route(task):
                            err = self._openai_error_payload(
                                str(error_payload.get("code") or "UPSTREAM_ERROR"),
                                str(error_payload.get("message") or "Upstream error"),
                            )
                        else:
                            err = upstream_error_payload(error_payload)
                        encoded = f"data: {json.dumps(err)}\n\n".encode()
                        yield encoded
                        if spool is not None:
                            spool.write(encoded)
                    break

                passthrough = passthrough or original_chunk(chunk, "openai") is not None
                encoded = self._encode_openai_chunk(chunk, task.id, request_body)
                if encoded is not None:
                    yield encoded
                    if spool is not None:
                        spool.write(encoded)

            if not passthrough:
                encoded = b"data: [DONE]\n\n"
                yield encoded
                if spool is not None:
                    spool.write(encoded)
        finally:
            if spool is not None:
                try:
                    event = audit_log.finalize_body_spool(
                        "client_response", spool, content_type="text/event-stream",
                        task_id=task.id, request_id=audit_context["request_id"], protocol=audit_context["protocol"],
                        endpoint=audit_context["endpoint"], worker_id=task.worker_id,
                        http={"status_code": 200, "content_type": "text/event-stream"},
                        terminal_status=task.status,
                    )
                    queue = getattr(self._core, "queue", None)
                    reconcile = getattr(queue, "record_client_response_reconciliation", None)
                    if callable(reconcile):
                        reconcile(task, event["event_id"])
                except Exception as exc:
                    log("audit", "error", f"Failed to audit OpenAI stream response task={task.id}: {exc}", self.id)
            asyncio.create_task(self._core.delete_task(task.id))

    async def _openai_streaming_response(self, task, request_body: dict) -> Response:
        """Open SSE only after the first chunk, preserving HTTP errors before output starts."""
        while True:
            _, remaining = self._task_timeout_phase(task)
            if remaining is not None and remaining <= 0:
                await self._terminate_task_on_timeout(task)
                return self._error_response(
                    protocol="openai",
                    status_code=504,
                    code="timeout_error",
                    message="Request timed out",
                    task_id=task.id,
                )

            wait_timeout = 1.0 if remaining is None else max(0.01, min(remaining, 1.0))
            try:
                chunk = await asyncio.wait_for(task._chunk_queue.get(), timeout=wait_timeout)
            except asyncio.TimeoutError:
                continue

            if chunk is None:
                if task.status != STATUS_COMPLETED:
                    response = await self._openai_sync_response(task, request_body)
                    self._audit_client_response(task, response)
                    return response
                return StreamingResponse(
                    self._openai_stream_response(task, request_body),
                    media_type="text/event-stream",
                )

            return StreamingResponse(
                self._openai_stream_response(task, request_body, first_chunk=chunk),
                headers={"content-type": chunk.content_type if isinstance(chunk, UpstreamChunk) and chunk.protocol == "openai" else "text/event-stream"},
            )

    def _encode_openai_chunk(self, chunk: dict, task_id: str, request_body: dict) -> bytes | None:
        """Return an original OpenAI event or a converted event, omitting foreign controls."""
        raw = original_chunk(chunk, "openai")
        if raw is not None:
            return raw
        if isinstance(chunk, UpstreamChunk) and chunk.original is None:
            return None
        payload = self._ollama_chunk_to_openai(chunk, task_id, request_body)
        return f"data: {json.dumps(payload)}\n\n".encode()

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
            "resolved_protocol": self._model_protocol(selected["provider"], selected["model_id"]),
            "selection": "alias" if selected["rank"] == 0 else "model_id",
            "worker_preferred_provider": preferred_provider,
        }

    def _openai_models_response(self) -> dict:
        """Build OpenAI-compatible models list response."""
        now = int(time.time())
        data = []
        for model_info in self._collect_model_entries():
            extension = self._model_extension(model_info["id"])
            if extension is None:
                extension = {
                    "schema_version": 1,
                    "requested_model": model_info["id"],
                    "provider": "",
                    "model": model_info["real_id"],
                    "resources": [],
                    "state": {},
                }
            data.append({
                "id": model_info["id"],
                "object": "model",
                "created": now,
                "owned_by": "aidir",
                "schema_version": 1,
                "aidir": extension,
            })
        return {
            "object": "list",
            "schema_version": 1,
            "data": data,
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

    async def _safe_queue_state(self, protocol: str, operation) -> Response:
        """Await one queue-state operation and never leak an internal 500."""
        try:
            return await operation
        except Exception as exc:
            log("http", "error", f"Queue-state request failed: {exc}", self.id)
            return self._error_response(
                protocol=protocol,
                status_code=503,
                code="QUEUE_STATE_UNAVAILABLE",
                message="Queue state is temporarily unavailable",
            )

    async def _model_queue_state_response(
        self,
        *,
        protocol: str,
        provider_id: str,
        model_id: str,
        priority: int,
        request: Request | None = None,
    ) -> JSONResponse:
        """Return queue state for a provider/model pair."""
        if request is not None:
            unauthorized = self._authorize_request(request)
            if unauthorized is not None:
                return unauthorized
        if priority < 0:
            return self._error_response(
                protocol=protocol,
                status_code=400,
                code="INVALID_REQUEST",
                message="priority must be greater than or equal to 0",
            )

        requested_provider_id = str(provider_id)
        requested_model_id = str(model_id)
        resolved_route = await self._resolve_queue_state_route(
            provider_id=provider_id,
            model_id=model_id,
            priority=priority,
        )
        if isinstance(resolved_route, dict):
            provider_id = str(resolved_route.get("resolved_provider") or provider_id)
            model_id = str(resolved_route.get("resolved_model") or model_id)

        if self._provider_api(provider_id) == "smart":
            smart_cfg = self._find_provider_model_cfg(provider_id, model_id)
            if not isinstance(smart_cfg, dict):
                smart_cfg = self._find_provider_model_cfg(provider_id, requested_model_id) or {}
            return JSONResponse(self._smart_model_state(requested_model_id, smart_cfg))

        resolved = self._resolve_model_resource_requirements(provider_id, model_id)
        if resolved is None:
            return self._error_response(
                protocol=protocol,
                status_code=404,
                code="model_not_found" if protocol == "openai" else "INVALID_MODEL",
                message=f"Unknown provider/model pair: {provider_id}/{model_id}",
                error_type="invalid_request_error" if protocol == "openai" else None,
                error_param="model" if protocol == "openai" else None,
            )

        if self._core.queue is None:
            return self._error_response(
                protocol=protocol,
                status_code=503,
                code="QUEUE_UNAVAILABLE",
                message="Queue is not available",
            )

        queue_state = None
        if self._core.queue is not None:
            getter = getattr(self._core.queue, "get_model_queue_state", None)
            if callable(getter):
                queue_state = await getter(provider_id, model_id, priority=priority)
        if not isinstance(queue_state, dict):
            getter = getattr(self._core.queue, "get_resource_queue_state", None)
            if callable(getter):
                queue_state = await getter(resolved, priority=priority)
        if not isinstance(queue_state, dict):
            summary = self._task_queue_summary(provider_id, model_id)
            queue_state = {
                "queued_count_total": int(summary.get("queued") or 0),
                "queued_count_below_priority": 0,
                "priority_counts": [
                    {"priority": int(priority_name or 0), "count": count}
                    for priority_name, count in sorted(
                        (summary.get("by_priority") or {}).items(),
                        key=lambda item: str(item[0]),
                    )
                ],
            }

        local_filter = getattr(self._core.resources, "local_admission_requirements", None)
        local_resolved = (
            local_filter(resolved)
            if self._core.resources is not None and callable(local_filter)
            else resolved
        )
        admission = None
        if self._core.resources is not None:
            evaluator = getattr(self._core.resources, "evaluate_model_admission", None)
            if callable(evaluator):
                admission = evaluator(local_resolved, model_id, provider_id)
        resource_ready = bool(
            admission.get("resource_ready")
            if isinstance(admission, dict)
            else (self._core.resources and self._core.resources.check_available(local_resolved))
        )
        resource_ready_after_unload = bool(
            admission.get("resource_ready_after_unload")
            if isinstance(admission, dict)
            else (
                self._core.resources
                and self._core.resources.check_available_after_unload(local_resolved)
            )
        )
        resource_reusable = bool(
            self._core.resources
            and callable(getattr(self._core.resources, "check_available_for_reuse", None))
            and self._core.resources.check_available_for_reuse(
                local_resolved,
                model_id=model_id,
                provider_id=provider_id,
            )
        )
        blocked_by_same_or_higher = queue_state["queued_count_total"] - queue_state["queued_count_below_priority"]
        can_run_now = (
            bool(admission.get("admissible_now"))
            if isinstance(admission, dict)
            else ((resource_ready_after_unload or resource_reusable) and blocked_by_same_or_higher == 0)
        )
        can_run_now = bool(can_run_now and blocked_by_same_or_higher == 0)

        payload = {
            "schema_version": 1,
            "provider": provider_id,
            "model": model_id,
            "priority": priority,
            "resources": self._resource_usage_entries(provider_id, model_id, resolved),
            "can_run_now": can_run_now,
            "queued_count_below_priority": queue_state["queued_count_below_priority"],
            "queued_count_total": queue_state["queued_count_total"],
            "priority_counts": queue_state["priority_counts"],
            "resource_ready": resource_ready,
            "resource_ready_after_unload": resource_ready_after_unload,
            "resource_reusable": resource_reusable,
        }
        if isinstance(admission, dict):
            payload.update(
                {
                    "active_count": admission.get("active_count", 0),
                    "max_concurrent_requests": admission.get("max_concurrent_requests", 1),
                    "session_resources": admission.get("session_resources") or {},
                    "model_loaded": bool(admission.get("model_loaded")),
                    "admission": admission.get("admission"),
                    "free": admission.get("free") or {},
                    "needed": admission.get("needed") or {},
                }
            )
            slots = getattr(self._core.resources, "memory_slots", None)
            if callable(slots):
                payload["memory_slots"] = slots(model_id, provider_id)
        if provider_id != requested_provider_id or model_id != requested_model_id:
            payload["requested_provider"] = requested_provider_id
            payload["requested_model"] = requested_model_id
        return JSONResponse(payload)

    async def _resolve_queue_state_route(
        self,
        *,
        provider_id: str,
        model_id: str,
        priority: int,
    ) -> dict | None:
        """Resolve default and smart queue-state requests into a concrete provider/model pair."""
        worker_id = self._resolve_worker_id({})
        normalized_provider_id = str(provider_id or "").strip()
        normalized_model_id = str(model_id or "").strip()
        route: dict | None = None

        if normalized_provider_id == "default":
            route = self._resolve_model_route(normalized_model_id, worker_id)
        elif self._provider_api(normalized_provider_id) == "smart":
            smart_model_cfg = self._find_provider_model_cfg(normalized_provider_id, normalized_model_id)
            if isinstance(smart_model_cfg, dict):
                route = {
                    "requested_model": normalized_model_id,
                    "requested_alias": normalized_model_id,
                    "resolved_provider": normalized_provider_id,
                    "resolved_model": self._model_id(smart_model_cfg) or normalized_model_id,
                    "selection": "provider_model",
                    "worker_preferred_provider": self._preferred_provider_for_worker(worker_id),
                }

        if not isinstance(route, dict):
            return None

        if self._provider_api(str(route.get("resolved_provider") or "")) != "smart":
            return route

        # Resolve the smart alias when a candidate is schedulable. When none is,
        # keep the smart route so the queue-state response can report the model
        # as a whole instead of raising an internal error.
        try:
            return await self._resolve_smart_route(
                route,
                worker_id=worker_id,
                payload={
                    "model": route.get("requested_model") or normalized_model_id,
                    "priority": priority,
                },
            )
        except Exception as exc:
            log(
                "http",
                "warning",
                f"Smart queue-state resolution unavailable for {normalized_model_id}: {exc}",
                self.id,
            )
            return route

    async def _model_only_queue_state_response(
        self,
        *,
        protocol: str,
        model_id: str,
        priority: int,
        request: Request | None = None,
    ) -> JSONResponse:
        """Resolve external model id using inference routing rules and return queue state."""
        if request is not None:
            unauthorized = self._authorize_request(request)
            if unauthorized is not None:
                return unauthorized
        worker_id = self._resolve_worker_id({})
        route = self._resolve_model_route(model_id, worker_id)
        if route is None:
            return self._error_response(
                protocol=protocol,
                status_code=404,
                code="model_not_found" if protocol == "openai" else "INVALID_MODEL",
                message=f"Unknown model: {model_id}",
                error_type="invalid_request_error" if protocol == "openai" else None,
                error_param="model" if protocol == "openai" else None,
            )

        provider_id = str(route.get("resolved_provider") or "")
        resolved_model = str(route.get("resolved_model") or "")
        response = await self._model_queue_state_response(
            protocol=protocol,
            provider_id=provider_id,
            model_id=resolved_model,
            priority=priority,
        )
        try:
            payload = json.loads(response.body)
        except Exception:
            return response

        payload["requested_model"] = str(model_id)
        return JSONResponse(payload, status_code=response.status_code)

    async def _handle_ollama_show(self, request: Request) -> JSONResponse:
        """Return configured Ollama-compatible model metadata without contacting the upstream."""
        try:
            body = await request.json()
        except Exception:
            return self._error_response(protocol="ollama", status_code=400, code="INVALID_REQUEST", message="Invalid JSON body")
        if not isinstance(body, dict):
            return self._error_response(protocol="ollama", status_code=400, code="INVALID_REQUEST", message="Request body must be an object")

        model_name = str(body.get("name") or "").strip()
        if not model_name:
            return self._error_response(protocol="ollama", status_code=400, code="INVALID_REQUEST", message="Field 'name' is required")

        route = self._resolve_model_route(model_name, self._resolve_worker_id({}))
        if not isinstance(route, dict):
            return self._error_response(protocol="ollama", status_code=404, code="INVALID_MODEL", message=f"Unknown model: {model_name}")

        provider_id = str(route.get("resolved_provider") or "").strip()
        model_id = str(route.get("resolved_model") or "").strip()
        model_cfg = self._find_provider_model_cfg(provider_id, model_id)
        if not isinstance(model_cfg, dict):
            return self._error_response(protocol="ollama", status_code=404, code="INVALID_MODEL", message=f"Unknown model: {model_name}")

        return JSONResponse(self._ollama_show_response(model_name, provider_id, model_id, model_cfg))

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
        return self._openai_error_payload_with_type(code, message, task_id=task_id)

    @staticmethod
    def _openai_error_payload_with_type(
        code: str,
        message: str,
        task_id: str | None = None,
        *,
        error_type: str | None = None,
        error_param: str | None = None,
    ) -> dict:
        """OpenAI-style error payload with optional type/param overrides."""
        normalized_code = str(code or "server_error")
        normalized_type = str(error_type or normalized_code)
        payload = {
            "error": {
                "message": message,
                "type": normalized_type,
                "code": normalized_code,
            }
        }
        if error_param is not None:
            payload["error"]["param"] = error_param
        if task_id:
            payload["error"]["task_id"] = task_id
        return payload

    @staticmethod
    def _extract_model_not_found_name(raw_text: str) -> str:
        """Extract missing model id from upstream text when present."""
        match = re.search(r"model\s+['\"]([^'\"]+)['\"]\s+not\s+found", raw_text, flags=re.IGNORECASE)
        if not match:
            return ""
        return str(match.group(1)).strip()

    def _map_openai_failed_task_error(self, error: dict | None) -> dict[str, str | int | None]:
        """Map worker failure payload to OpenAI-compatible HTTP status and error envelope."""
        err = error if isinstance(error, dict) else {}
        err_code = str(err.get("code") or "server_error")
        err_message = str(err.get("message") or "Worker error")
        err_body = str(err.get("body") or "")
        combined = f"{err_message}\n{err_body}".lower()

        if err_code == "UPSTREAM_TIMEOUT":
            return {
                "status_code": 504,
                "code": "upstream_timeout",
                "message": "upstream timeout",
                "error_type": "upstream_timeout_error",
                "error_param": None,
            }

        if err_code == "INVALID_MODEL":
            return {
                "status_code": 404,
                "code": "model_not_found",
                "message": err_message,
                "error_type": "invalid_request_error",
                "error_param": "model",
            }

        if err_code == "UPSTREAM_ERROR" and "404" in combined and "model" in combined and "not found" in combined:
            missing_model = self._extract_model_not_found_name(f"{err_message}\n{err_body}")
            if missing_model:
                mapped_message = f"The model '{missing_model}' does not exist."
            else:
                mapped_message = "The requested model does not exist."
            return {
                "status_code": 404,
                "code": "model_not_found",
                "message": mapped_message,
                "error_type": "invalid_request_error",
                "error_param": "model",
            }

        return {
            "status_code": 502,
            "code": err_code,
            "message": err_message,
            "error_type": None,
            "error_param": None,
        }

    def _ollama_show_response(
        self,
        requested_name: str,
        provider_id: str,
        model_id: str,
        model_cfg: dict,
    ) -> dict:
        """Build stable Ollama show metadata from a configured provider/model pair."""
        provider_api = self._provider_api(provider_id)
        generation_fields = (
            "temperature", "top_p", "repeat_penalty", "repetition_penalty",
            "repeat_last_n", "num_predict", "max_tokens", "seed",
            "presence_penalty", "frequency_penalty", "top_k", "min_p",
        )
        parameters = "\n".join(
            f"{field} {model_cfg[field]}"
            for field in generation_fields
            if model_cfg.get(field) is not None
        )
        capabilities = model_cfg.get("capabilities")
        if not isinstance(capabilities, list):
            capabilities = ["completion", "chat"]

        return {
            "modelfile": "",
            "parameters": parameters,
            "template": str(model_cfg.get("template") or ""),
            "details": {
                "parent_model": str(model_cfg.get("parent_model") or ""),
                "format": str(model_cfg.get("format") or ("gguf" if provider_api == "llama-cpp" else "")),
                "family": str(model_cfg.get("family") or model_cfg.get("name") or model_id),
                "families": list(model_cfg.get("families") or []),
                "parameter_size": str(model_cfg.get("parameter_size") or ""),
                "quantization_level": str(model_cfg.get("quantization_level") or ""),
            },
            "model_info": {
                "aidir.provider": provider_id,
                "aidir.provider_api": provider_api,
                "aidir.model": model_id,
                "aidir.requested_name": requested_name,
                "aidir.context_window": model_cfg.get("contextWindow"),
                "aidir.context_length": model_cfg.get("contextWindow"),
            },
            "capabilities": [str(item) for item in capabilities],
        }

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
        error_type: str | None = None,
        error_param: str | None = None,
    ) -> JSONResponse:
        """Protocol-aware error response. Supports compatibility mode toggle."""
        if self._errors_compatibility_mode:
            payload = (
                self._openai_error_payload_with_type(
                    code,
                    message,
                    task_id=task_id,
                    error_type=error_type,
                    error_param=error_param,
                )
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
        usage = data.get("usage")
        if isinstance(usage, dict) and ("prompt_tokens" in usage or "completion_tokens" in usage):
            return dict(usage)
        if prompt_tokens is None and completion_tokens is None:
            return dict(usage) if isinstance(usage, dict) else None
        prompt_tokens = int(prompt_tokens or 0)
        completion_tokens = int(completion_tokens or 0)
        return {
            **(usage if isinstance(usage, dict) else {}),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

    @staticmethod
    def _ollama_tool_calls_to_openai(raw_calls: object, *, streaming: bool = False) -> list[dict]:
        """Convert Ollama tool calls into OpenAI-compatible function-call objects."""
        if not isinstance(raw_calls, list):
            return []

        tool_calls: list[dict] = []
        for fallback_index, raw_call in enumerate(raw_calls):
            if not isinstance(raw_call, dict):
                continue

            function = raw_call.get("function")
            function = function if isinstance(function, dict) else {}
            name = function.get("name") or raw_call.get("name")
            if not isinstance(name, str) or not name:
                continue

            arguments = function.get("arguments", raw_call.get("arguments", {}))
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments if arguments is not None else {})

            tool_call = {
                **raw_call,
                "id": str(raw_call.get("id") or ""),
                "type": str(raw_call.get("type") or "function"),
                "function": {
                    **function,
                    "name": name,
                    "arguments": arguments,
                },
            }
            if streaming:
                index = raw_call.get("index")
                tool_call["index"] = index if isinstance(index, int) else fallback_index
            tool_calls.append(tool_call)
        return tool_calls

    def _ollama_sync_to_openai(self, data: dict, task_id: str, request_body: dict) -> dict:
        """Convert non-stream ollama chat response to OpenAI chat.completion shape."""
        msg = {key: value for key, value in (data.get("message") or {}).items() if key != "anthropic_blocks"}
        content = msg.get("content", "")
        model = data.get("model") or request_body.get("model") or ""
        tool_calls = self._ollama_tool_calls_to_openai(msg.get("tool_calls"))
        message = {**msg, "role": msg.get("role", "assistant"), "content": content}
        if "reasoning_content" not in message and "thinking" in message:
            message["reasoning_content"] = message["thinking"]
        if tool_calls:
            message["tool_calls"] = tool_calls

        resp = {
            **{key: value for key, value in data.items() if key not in {
                "message", "done", "done_reason", "created_at", "choices",
                "total_duration", "load_duration",
                "prompt_eval_count", "prompt_eval_duration",
                "eval_count", "eval_duration",
            }},
            "id": data.get("id") or f"chatcmpl-{task_id}",
            "object": "chat.completion",
            "created": data.get("created", int(time.time())),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": data.get("done_reason") or ("tool_calls" if tool_calls else "stop"),
                }
            ],
        }
        if isinstance(data.get("choices"), list):
            resp["choices"] = data["choices"]

        usage = self._usage_from_ollama(data)
        if usage is not None:
            resp["usage"] = usage
        return resp

    def _ollama_chunk_to_openai(self, chunk: dict, task_id: str, request_body: dict) -> dict:
        """Convert one ollama stream chunk to OpenAI chat.completion.chunk shape."""
        if isinstance(chunk, UpstreamChunk) and chunk.protocol == "ollama" and chunk.original is not None:
            chunk = chunk.original
        msg = {key: value for key, value in (chunk.get("message") or {}).items() if key != "anthropic_blocks"}
        done = bool(chunk.get("done", False))
        tool_calls = self._ollama_tool_calls_to_openai(msg.get("tool_calls"), streaming=True)

        delta = dict(msg)
        if "reasoning_content" not in delta and "thinking" in delta:
            delta["reasoning_content"] = delta["thinking"]
        if tool_calls:
            delta["tool_calls"] = tool_calls

        finish_reason = (chunk.get("done_reason") or ("tool_calls" if tool_calls else "stop")) if done else None

        response = {
            **{key: value for key, value in chunk.items() if key not in {
                "message", "done", "done_reason", "created_at", "choices",
                "total_duration", "load_duration",
                "prompt_eval_count", "prompt_eval_duration",
                "eval_count", "eval_duration",
            }},
            "id": chunk.get("id") or f"chatcmpl-{task_id}",
            "object": "chat.completion.chunk",
            "created": chunk.get("created", int(time.time())),
            "model": chunk.get("model") or request_body.get("model") or "",
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }
            ],
        }
        if isinstance(chunk.get("choices"), list):
            choices = []
            for choice in chunk["choices"]:
                converted = dict(choice)
                if "message" in converted:
                    converted["delta"] = converted.pop("message")
                choices.append(converted)
            response["choices"] = choices
        usage = self._usage_from_ollama(chunk)
        if usage is not None:
            response["usage"] = usage
        return response
