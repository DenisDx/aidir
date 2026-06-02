"""
Task scheduler.
Background asyncio loop that pops tasks from the queue and dispatches them
to the appropriate worker. Runs concurrently with uvicorn servers.
TODO: add resource availability checks before dispatching (VRAM etc.).
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import quote
from typing import TYPE_CHECKING

import httpx

from core.task import Task, STATUS_QUEUED
from core.worker import WorkerResult
from core import log
from core.smart_router import SmartRouter, SmartRouteError

if TYPE_CHECKING:
    from core.queue_manager import QueueManager
    from core.resources import Resources
    from core.worker import BaseWorker

_SUPPORTED_TYPES = ("agent", "tool", "context")  # TODO: extend with request, tts, stt…


class Scheduler:
    """
    Polls the priority queue and runs tasks via workers.
    Each task is executed as an independent asyncio task for concurrency.
    """

    def __init__(
        self,
        queue: "QueueManager",
        workers: dict[str, "BaseWorker"],
        workers_cfg: dict | None = None,
        resources: "Resources | None" = None,
        full_config: dict | None = None,
    ) -> None:
        self._queue = queue
        self._workers = workers
        self._workers_cfg = workers_cfg or {}
        self._resources = resources
        self._full_config = full_config or {}
        self._running = False
        self._wake = asyncio.Event()
        self._active_runs: set[asyncio.Task] = set()
        self._idle = asyncio.Event()
        self._idle.set()

    def notify_new_task(self) -> None:
        """Wake the scheduler loop when a new task is enqueued."""
        self._wake.set()

    async def run(self) -> None:
        """Main scheduler loop. Runs until stop() is called."""
        self._running = True
        log("system", "info", "Scheduler started")

        while self._running:
            dispatched = False

            for task_type in _SUPPORTED_TYPES:
                task_id = await self._queue.pop_next(task_type)
                if task_id is None:
                    continue

                task = self._queue.get_task(task_id)
                if task is None:
                    log("system", "warn", f"Task {task_id} popped but not in memory")
                    continue

                # Task is scheduled for later retry.
                if task.next_retry_at and task.next_retry_at > time.time():
                    await self._queue.add_task(task)
                    continue

                worker = self._select_worker(task)
                if worker is None:
                    # No compatible worker – re-enqueue and skip
                    await self._queue.add_task(task)
                    log("system", "warn",
                        f"No worker for task {task_id} (type={task_type}), re-queued")
                    continue

                await self._refresh_smart_route_for_dispatch(task, worker.id)

                reqs = self._resolve_resource_requirements(task, worker.id)
                if reqs and self._resources and not self._resources.check_available(reqs):
                    if self._resources.check_available_after_unload(reqs):
                        # Soft consumers (alive-time models) block the resource; force-unload them.
                        log("system", "info",
                            f"Task {task.id} needs force-unload of idle models to free resources")
                        await self._resources.force_unload_for(reqs, self._full_config)
                        # After unload, verify (hard check — soft consumers cleared)
                        if not self._resources.check_available_after_unload(reqs):
                            task.next_retry_at = time.time() + 5
                            await self._queue.add_task(task)
                            log("system", "warn",
                                f"Task {task.id} delayed: resource unload did not free enough space")
                            continue
                        # Fall through — resources are now available
                    else:
                        # Not enough even with force unload
                        task.next_retry_at = time.time() + 1
                        await self._queue.add_task(task)
                        log("system", "info", f"Task {task.id} delayed: insufficient resources")
                        continue

                bg_task = asyncio.create_task(
                    self._run_task(task, worker, reqs),
                    name=f"task:{task.id}:{worker.id}",
                )
                self._active_runs.add(bg_task)
                self._idle.clear()
                bg_task.add_done_callback(self._on_run_done)
                dispatched = True

            if not dispatched:
                # Sleep until a new task arrives or polling interval elapses
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
        
        log("core", "info", "Scheduler run loop exited")

    def stop(self) -> None:
        log("core", "info", f"Scheduler stop requested: _running={self._running} active_tasks={len(self._active_runs)}")
        self._running = False
        self._wake.set()

    def active_task_count(self) -> int:
        """Return number of currently executing tasks."""
        return len(self._active_runs)

    def active_task_labels(self) -> list[str]:
        """Return readable labels for currently executing tasks."""
        return sorted(run.get_name() for run in self._active_runs)

    async def wait_for_active_tasks(self, timeout: int) -> bool:
        """Wait until all active tasks finish; return True if drained in time."""
        started = time.monotonic()
        count = len(self._active_runs)
        
        if not self._active_runs:
            log("core", "info", "wait_for_active_tasks: no active tasks, returning immediately")
            return True
        
        log("core", "info", f"wait_for_active_tasks: BEGIN count={count} timeout={timeout}s tasks={self.active_task_labels()}")
        
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=timeout or None)
            elapsed = time.monotonic() - started
            log("core", "info", f"wait_for_active_tasks: SUCCESS all drained in {elapsed:.2f}s")
            return True
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - started
            remaining = self.active_task_labels()
            log("core", "warn", f"wait_for_active_tasks: TIMEOUT after {elapsed:.2f}s count={len(remaining)} remaining={remaining}")
            return False

    async def cancel_active_tasks(self, timeout: float = 5.0) -> int:
        """Cancel all active task executions and wait briefly for them to finish."""
        runs = list(self._active_runs)
        
        if not runs:
            log("core", "info", "cancel_active_tasks: no active tasks to cancel")
            return 0
        
        started = time.monotonic()
        log("core", "info", f"cancel_active_tasks: BEGIN count={len(runs)} timeout={timeout}s tasks={[r.get_name() for r in runs]}")
        
        for run in runs:
            run.cancel()

        try:
            await asyncio.wait_for(
                asyncio.gather(*runs, return_exceptions=True),
                timeout=timeout or None,
            )
            elapsed = time.monotonic() - started
            log("core", "info", f"cancel_active_tasks: SUCCESS canceled {len(runs)} tasks in {elapsed:.2f}s")
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - started
            remaining = [r.get_name() for r in runs if not r.done()]
            log("core", "warn", f"cancel_active_tasks: TIMEOUT after {elapsed:.2f}s canceled attempts={len(runs)} still_running={len(remaining)} tasks={remaining}")

        return len(runs)

    async def cancel_task(self, task_id: str, timeout: float = 5.0) -> bool:
        """Cancel one active task by id and wait briefly for its runner to finish."""
        target_run = None
        prefix = f"task:{task_id}:"
        for run in self._active_runs:
            if run.get_name().startswith(prefix):
                target_run = run
                break

        if target_run is None:
            return False

        log("core", "info", f"cancel_task: BEGIN task_id={task_id} timeout={timeout}s run={target_run.get_name()}")
        target_run.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(target_run, return_exceptions=True), timeout=timeout or None)
            log("core", "info", f"cancel_task: SUCCESS task_id={task_id}")
        except asyncio.TimeoutError:
            log("core", "warn", f"cancel_task: TIMEOUT task_id={task_id} timeout={timeout}s")
        return True

    # ── Internal ─────────────────────────────────────────────────────────────

    def _on_run_done(self, bg_task: asyncio.Task) -> None:
        """Track completion of background worker runs."""
        self._active_runs.discard(bg_task)
        if not self._active_runs:
            self._idle.set()

    def _select_worker(self, task: Task) -> "BaseWorker | None":
        """
        Select a worker for the task.
        Explicit worker_id in task takes priority; otherwise first match by type.
        TODO: check resource availability (VRAM etc.) before selecting.
        """
        if task.worker_id and task.worker_id in self._workers:
            w = self._workers[task.worker_id]
            if w.enabled:
                return w

        for w in self._workers.values():
            if w.task_type == task.type and w.enabled:
                return w

        return None

    @staticmethod
    def _run_timeout_source(task: Task) -> str:
        """Describe where the effective task run timeout came from."""
        payload = task.payload if isinstance(task.payload, dict) else {}
        if payload.get("timeout") is not None:
            return "payload.timeout"
        return "tasks.run_timeout / TASK_RUN_TIMEOUT_SECONDS"

    @classmethod
    def _build_run_timeout_message(cls, task: Task, elapsed_seconds: float) -> str:
        """Build an informative task run-timeout message for logs and API errors."""
        try:
            timeout_limit = int(task.run_timeout)
        except (TypeError, ValueError):
            timeout_limit = 0

        timeout_source = cls._run_timeout_source(task)
        return (
            f"Task run timeout exceeded after {elapsed_seconds:.2f}s "
            f"(limit={timeout_limit}s, parameter=task.run_timeout, source={timeout_source})"
        )

    async def _run_task(
        self,
        task: Task,
        worker: "BaseWorker",
        reserved_reqs: dict[str, dict[str, int]] | None = None,
    ) -> None:
        """Execute one task; handle timeouts and exceptions."""
        log("worker", "info", f"Starting task {task.id}", worker.id)
        await self._queue.mark_running(task.id, worker.id)
        consumer_id = f"{task.id}:{worker.id}"
        # Model id is used to track soft consumers (alive_time) after release
        model_id: str | None = (task.payload or {}).get("model") or None

        if self._resources and reserved_reqs:
            await self._resources.reserve_blind_for(reserved_reqs, consumer_id=consumer_id)

        started = time.monotonic()

        try:
            result: WorkerResult = await asyncio.wait_for(
                worker.execute(task, emit_chunk=self._make_emitter(task)),
                timeout=task.run_timeout or None,
            )
            if result.ok:
                task.result = result.data
                task.retry_attempt = 0
                task.fallback_index = 0
                task.next_retry_at = 0.0
                await self._queue.mark_completed(task)
                log("worker", "info", f"Task {task.id} completed", worker.id)
            else:
                err = result.error or {"code": "WORKER_ERROR", "message": "Worker returned error"}
                if not await self._handle_reject(task, worker.id, err):
                    await self._queue.mark_failed(task, err)
                    log("worker", "warn", f"Task {task.id} failed: {err}", worker.id)

        except asyncio.TimeoutError:
            elapsed = time.monotonic() - started
            timeout_message = self._build_run_timeout_message(task, elapsed)
            log("worker", "error", f"Task {task.id} timed out: {timeout_message}", worker.id)
            err = {"code": "TIMEOUT", "message": timeout_message}
            if not await self._handle_reject(task, worker.id, err):
                await self._queue.mark_failed(task, err)

        except asyncio.CancelledError:
            log("worker", "warn", f"Task {task.id} canceled", worker.id)
            await self._queue.mark_canceled(task)
            raise

        except Exception as exc:
            log("worker", "error", f"Task {task.id} exception: {exc}", worker.id)
            err = {"code": "EXCEPTION", "message": str(exc)}
            if not await self._handle_reject(task, worker.id, err):
                await self._queue.mark_failed(task, err)

        finally:
            if self._resources and reserved_reqs:
                await self._resources.release_for(reserved_reqs, consumer_id=consumer_id, model_id=model_id)

    async def _refresh_smart_route_for_dispatch(self, task: Task, worker_id: str) -> None:
        """Re-resolve queued smart routes so stale busy-fallback selections can move to a newly available candidate."""
        route_cfg = (task.config or {}).get("route") if isinstance(task.config, dict) else None
        if not isinstance(route_cfg, dict):
            return
        if str(route_cfg.get("selection") or "").strip() != "smart_route":
            return

        requested_provider = str(route_cfg.get("requested_provider") or "").strip()
        requested_model = str(
            route_cfg.get("requested_alias")
            or route_cfg.get("requested_model")
            or route_cfg.get("resolved_model")
            or ""
        ).strip()
        if not requested_provider or not requested_model:
            return
        if self._provider_api(requested_provider) != "smart":
            return

        base_route = {
            "requested_model": requested_model,
            "requested_alias": str(route_cfg.get("requested_alias") or requested_model).strip(),
            "resolved_provider": requested_provider,
            "resolved_model": requested_model,
        }
        request_payload = dict(task.payload or {})
        request_payload["model"] = requested_model
        incoming_bearer_token = ""
        if isinstance(task.config, dict):
            incoming_bearer_token = str(task.config.get("incoming_bearer_token") or "").strip()

        try:
            resolution = await self._make_smart_router(worker_id).resolve_route(
                base_route,
                request_payload=request_payload,
                request_priority=task.priority,
                incoming_bearer_token=incoming_bearer_token,
            )
        except SmartRouteError:
            return

        next_route = dict(resolution.route or {})
        if not next_route:
            return

        current_provider = str(route_cfg.get("resolved_provider") or "").strip()
        current_model = str(route_cfg.get("resolved_model") or "").strip()
        next_provider = str(next_route.get("resolved_provider") or "").strip()
        next_model = str(next_route.get("resolved_model") or "").strip()
        if current_provider == next_provider and current_model == next_model:
            task.config = dict(task.config or {})
            task.config["route"] = next_route
            return

        task.config = dict(task.config or {})
        task.config["route"] = next_route
        task.payload = dict(task.payload or {})
        task.payload["model"] = next_model
        task.resource_requirements = {}

        resolved_worker = str(next_route.get("resolved_worker") or "").strip()
        if resolved_worker:
            task.worker_id = resolved_worker

        log(
            "system",
            "info",
            (
                f"Task {task.id} smart route refreshed before dispatch: "
                f"{current_provider}/{current_model} -> {next_provider}/{next_model}"
            ),
        )

    def _resolve_resource_requirements(self, task: Task, worker_id: str) -> dict[str, dict[str, int]]:
        """Resolve resource requirements from task or worker/model config."""
        if task.resource_requirements:
            return task.resource_requirements

        merged: dict[str, dict[str, int]] = {}

        wcfg = self._workers_cfg.get(worker_id, {}) or {}
        for rid, vals in (wcfg.get("resources") or {}).items():
            merged.setdefault(rid, {})
            for k, v in (vals or {}).items():
                merged[rid][k] = int(merged[rid].get(k, 0)) + int(v)

        route_cfg = (task.config or {}).get("route") if isinstance(task.config, dict) else None
        if not isinstance(route_cfg, dict):
            route_cfg = {}

        model_id = route_cfg.get("resolved_model") or (task.payload or {}).get("model")
        provider_id = route_cfg.get("resolved_provider") or wcfg.get("provider")
        if model_id and provider_id:
            providers = (((self._full_config or {}).get("models") or {}).get("providers") or {})
            p = providers.get(provider_id) or {}
            for model in (p.get("models") or []):
                if model_id not in {model.get("id"), model.get("name"), model.get("alias")}:
                    continue
                for rid, vals in (model.get("resources") or {}).items():
                    merged.setdefault(rid, {})
                    for k, v in (vals or {}).items():
                        merged[rid][k] = int(merged[rid].get(k, 0)) + int(v)
                break

        task.resource_requirements = merged
        return merged

    def _make_smart_router(self, worker_id: str) -> SmartRouter:
        """Build a shared smart router for scheduler-time route refresh."""

        async def get_local_queue_state(requirements: dict, priority: int) -> dict | None:
            return await self._queue.get_resource_queue_state(requirements, priority=priority)

        def check_resource_available(requirements: dict) -> bool:
            if self._resources is None:
                return False
            return bool(self._resources.check_available(requirements))

        def check_resource_available_after_unload(requirements: dict) -> bool:
            if self._resources is None:
                return False
            return bool(self._resources.check_available_after_unload(requirements))

        return SmartRouter(
            endpoint_id="scheduler",
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
        )

    def _provider_api(self, provider_id: str) -> str:
        """Return provider api type from full config."""
        providers = (((self._full_config or {}).get("models") or {}).get("providers") or {})
        provider_cfg = providers.get(provider_id) if isinstance(providers, dict) else None
        return str((provider_cfg or {}).get("api") or "").strip()

    def _provider_cfg(self, provider_id: str) -> dict:
        """Return provider config from full config."""
        providers = (((self._full_config or {}).get("models") or {}).get("providers") or {})
        provider_cfg = providers.get(provider_id) if isinstance(providers, dict) else None
        return provider_cfg if isinstance(provider_cfg, dict) else {}

    @staticmethod
    def _model_id(model_cfg: dict) -> str:
        """Return external model id from provider model config."""
        return str(model_cfg.get("id") or "").strip()

    @staticmethod
    def _model_alias(model_cfg: dict) -> str:
        """Return external model alias from provider model config."""
        return str(model_cfg.get("alias") or "").strip()

    def _find_provider_model_cfg(self, provider_id: str, model_id: str) -> dict | None:
        """Return one provider model config matched by id, name, or alias."""
        models = self._provider_cfg(provider_id).get("models") or []
        if not isinstance(models, list):
            return None

        normalized = str(model_id or "").strip()
        for model_cfg in models:
            if not isinstance(model_cfg, dict):
                continue
            if normalized not in {
                self._model_id(model_cfg),
                self._model_alias(model_cfg),
                str(model_cfg.get("name") or "").strip(),
            }:
                continue
            return model_cfg
        return None

    def _resolve_model_resource_requirements(self, provider_id: str, model_id: str) -> dict | None:
        """Return resource requirements for one concrete provider/model pair."""
        model_cfg = self._find_provider_model_cfg(provider_id, model_id)
        if not isinstance(model_cfg, dict):
            return None
        resources = model_cfg.get("resources")
        return dict(resources) if isinstance(resources, dict) else None

    @staticmethod
    def _resolve_probe_timeout_ms(item: dict) -> int:
        """Resolve smart-candidate probe timeout in milliseconds."""
        try:
            parsed = int((item or {}).get("request_timeout_ms", 1500))
        except Exception:
            parsed = 1500
        return max(1, parsed)

    def _resolve_worker_id_for_route(self, worker_id: str, route: dict | None) -> str:
        """Adjust worker selection for providers that require a different execution path."""
        if not isinstance(route, dict):
            return worker_id

        resolved_worker = str(route.get("resolved_worker") or "").strip()
        if resolved_worker:
            return resolved_worker

        provider_id = str(route.get("resolved_provider") or "").strip()
        if self._provider_api(provider_id) != "openaix":
            return worker_id
        if "openaix" in self._workers:
            return "openaix"
        return worker_id

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
        """Confirm that an Ollama provider answers and reports the requested model in /api/tags."""
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

    def _resolve_probe_headers(self, provider_id: str, incoming_bearer_token: str = "") -> dict[str, str]:
        """Resolve smart-probe auth headers with provider override or bearer fallback."""
        provider_auth = self._provider_cfg(provider_id).get("auth")
        headers = self._build_auth_headers(provider_auth if isinstance(provider_auth, dict) else {})
        if not headers and incoming_bearer_token.strip():
            headers["Authorization"] = f"Bearer {incoming_bearer_token.strip()}"
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
        return headers

    @staticmethod
    def _reason_from_error(error: dict) -> str:
        """Map worker error to reject reason: unavailable|busy|error."""
        code = str((error or {}).get("code", "")).upper()
        if code in {"UPSTREAM_UNREACHABLE", "CONNECT_ERROR", "CONNECTION_ERROR", "UNAVAILABLE"}:
            return "unavailable"
        if code in {"UPSTREAM_BUSY", "RESOURCE_BUSY", "BUSY"}:
            return "busy"
        return "error"

    def _effective_policy(self, task: Task, worker_id: str, reason: str) -> dict:
        """Build effective reject policy from worker config + task overrides."""
        wcfg = self._workers_cfg.get(worker_id, {}) or {}

        retry_count = int(task.retry_count or wcfg.get("retry_count") or 0)
        retry_period = int(task.retry_period or wcfg.get("retry_period") or 0)
        fallbacks = list(task.fallbacks or wcfg.get("fallbacks") or [])

        on_reject = dict(wcfg.get("on_reject") or {})
        on_reject.update(task.on_reject or {})
        reason_cfg = (on_reject.get(reason) or {})

        action = str(reason_cfg.get("action") or "cancel").strip().lower()
        return {
            "action": action,
            "retry_count": int(reason_cfg.get("retry_count", retry_count)),
            "retry_period": int(reason_cfg.get("retry_period", retry_period)),
            "fallbacks": fallbacks,
        }

    async def _handle_reject(self, task: Task, worker_id: str, error: dict) -> bool:
        """Apply reject policy. Return True if task was re-queued, False if should fail now."""
        reason = self._reason_from_error(error)
        pol = self._effective_policy(task, worker_id, reason)

        action = pol["action"]
        retry_count = int(pol["retry_count"])
        retry_period = int(pol["retry_period"])
        fallbacks = list(pol["fallbacks"])

        def _schedule_retry() -> None:
            task.retry_attempt += 1
            task.next_retry_at = time.time() + max(0, retry_period)

        if action == "retry":
            if task.retry_attempt >= retry_count:
                return False
            _schedule_retry()
            await self._queue.add_task(task)
            log("worker", "info", f"Task {task.id} retry scheduled #{task.retry_attempt}", worker_id)
            return True

        if action in {"fallback", "fallback-retry"}:
            if not fallbacks:
                return False

            if task.fallback_index < len(fallbacks):
                task.worker_id = fallbacks[task.fallback_index]
                task.fallback_index += 1
                task.next_retry_at = time.time() + max(0, retry_period)
                await self._queue.add_task(task)
                log("worker", "info", f"Task {task.id} fallback -> {task.worker_id}", worker_id)
                return True

            # End of fallback chain
            if action == "fallback-retry" and task.retry_attempt < retry_count:
                _schedule_retry()
                task.fallback_index = 0
                task.worker_id = fallbacks[0]
                task.fallback_index = 1
                await self._queue.add_task(task)
                log("worker", "info", f"Task {task.id} fallback cycle retry #{task.retry_attempt}", worker_id)
                return True

            return False

        # cancel or unknown action
        return False

    @staticmethod
    def _make_emitter(task: Task):
        """Return async callable that puts a chunk into task's queue."""
        async def emit_chunk(chunk: dict) -> None:
            await task._chunk_queue.put(chunk)
        return emit_chunk
