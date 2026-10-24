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

from datetime import datetime, timezone

from core.task import Task, STATUS_QUEUED
from core.worker import WorkerResult
from core import log
from core.smart_router import SmartRouter, SmartRouteError
from core.kv_affinity import KvAffinityStore, ModelReadinessTracker

if TYPE_CHECKING:
    from core.hooks import HookManager
    from core.queue_manager import QueueManager
    from core.resource_monitor import ResourceMonitor
    from core.resources import Resources
    from core.worker import BaseWorker

_SUPPORTED_TYPES = ("agent", "tool")  # TODO: extend with request, tts, stt…

# How many times a task may be deferred because a remote aidir refuses to run
# the model before the task is failed with a clear error. Without a limit the
# task waits for its whole queue timeout and the client sees nothing.
_MAX_REMOTE_REFUSALS = 5


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
        hooks: "HookManager | None" = None,
        resource_monitor: "ResourceMonitor | None" = None,
        kv_affinity: KvAffinityStore | None = None,
        model_readiness: ModelReadinessTracker | None = None,
        affinity_config: dict | None = None,
    ) -> None:
        self._queue = queue
        self._workers = workers
        self._workers_cfg = workers_cfg or {}
        self._resources = resources
        self._full_config = full_config or {}
        self._hooks = hooks
        self._resource_monitor = resource_monitor
        self._kv_affinity = kv_affinity
        self._model_readiness = model_readiness
        self._affinity_config = affinity_config if isinstance(affinity_config, dict) else {}
        self._running = False
        self._wake = asyncio.Event()
        self._active_runs: set[asyncio.Task] = set()
        self._idle = asyncio.Event()
        self._idle.set()
        # Remote candidates that refused the task, and how often the refusal was
        # repeated. A remote that cannot run the model is not a transient
        # condition, so the task is re-routed instead of being deferred forever.
        self._remote_blocked_candidates: dict[str, set[tuple[str, str]]] = {}
        self._remote_refusal_counts: dict[str, int] = {}
        self._peer_auth_warned: set[str] = set()
        # Why every waiting task waits. The log is not enough: the default log
        # level hides the deferral messages, so the operator would never learn
        # why a task is not running.
        self._deferred: dict[str, dict] = {}

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
                deferred_tasks: list[Task] = []
                task_id = await self._queue.pop_next(task_type)
                while task_id is not None:
                    task = self._queue.get_task(task_id)
                    if task is None:
                        log("system", "warn", f"Task {task_id} popped but not in memory")
                        task_id = await self._queue.pop_next(task_type)
                        continue

                    if await self._expire_queued_task_if_needed(task):
                        task_id = await self._queue.pop_next(task_type)
                        continue

                    # Task is scheduled for later retry.
                    if task.next_retry_at and task.next_retry_at > time.time():
                        deferred_tasks.append(task)
                        task_id = await self._queue.pop_next(task_type)
                        continue

                    worker = self._select_worker(task)
                    if worker is None:
                        # No compatible worker – re-enqueue and skip
                        deferred_tasks.append(task)
                        self._note_deferral(task, "no worker for this task type")
                        log("system", "warn",
                            f"No worker for task {task_id} (type={task_type}), re-queued")
                        task_id = await self._queue.pop_next(task_type)
                        continue

                    await self._refresh_smart_route_for_dispatch(task, worker.id)

                    reqs = self._resolve_resource_requirements(task, worker.id)
                    requested_model_id = str(((task.payload or {}).get("model") or "")).strip()
                    requested_provider_id = self._resolve_task_provider_id(task, worker.id)
                    if reqs and self._resource_monitor and not await self._resource_monitor.refresh_required_availability(reqs):
                        task.next_retry_at = time.time() + 1
                        deferred_tasks.append(task)
                        self._note_deferral(task, "the measured availability of a resource is unavailable")
                        log("system", "warn", f"Task {task.id} delayed: resource availability probe failed")
                        task_id = await self._queue.pop_next(task_type)
                        continue
                    remote_ok, remote_kind, remote_reason = await self._remote_route_status(
                        task,
                        requested_provider_id,
                        requested_model_id,
                    )
                    if not remote_ok:
                        await self._handle_remote_refusal(
                            task,
                            worker,
                            requested_provider_id,
                            requested_model_id,
                            remote_kind,
                            remote_reason,
                            deferred_tasks,
                        )
                        task_id = await self._queue.pop_next(task_type)
                        continue
                    local_reqs = self._resources.local_admission_requirements(reqs) if self._resources else reqs
                    model_admission = None
                    model_max = 1
                    if local_reqs and self._resources and requested_model_id:
                        model_max, _ = self._resources.model_parallel_config(
                            requested_provider_id,
                            requested_model_id,
                        )

                    if local_reqs and self._resources and model_max > 1:
                        admission = self._resources.evaluate_model_admission(
                            local_reqs,
                            requested_model_id,
                            requested_provider_id,
                        )
                        if not admission.get("admissible_now") and admission.get("admission") == "waiting_unload":
                            log("system", "info",
                                f"Task {task.id} needs force-unload of idle models to fit a session")
                            await self._resources.force_unload_for(
                                admission.get("needed") or local_reqs,
                                self._full_config,
                                keep_model_id=requested_model_id or None,
                                keep_provider_id=requested_provider_id or None,
                            )
                            admission = self._resources.evaluate_model_admission(
                                local_reqs,
                                requested_model_id,
                                requested_provider_id,
                            )
                        if not admission.get("admissible_now"):
                            task.next_retry_at = time.time() + 1
                            deferred_tasks.append(task)
                            self._note_deferral(task, f"the model is not admissible: {admission.get('admission')}")
                            log("system", "warning",
                                f"Task {task.id} delayed: {admission.get('admission')} for model {requested_model_id}")
                            task_id = await self._queue.pop_next(task_type)
                            continue
                        model_admission = await self._resources.reserve_model_start(
                            local_reqs,
                            requested_model_id,
                            requested_provider_id,
                            consumer_id=f"{task.id}:{worker.id}",
                        )
                        if not isinstance(model_admission, dict) or not model_admission.get("admissible_now"):
                            task.next_retry_at = time.time() + 1
                            deferred_tasks.append(task)
                            self._note_deferral(task, "the resources changed during the admission")
                            log("system", "warning", f"Task {task.id} delayed: resources changed during model admission")
                            task_id = await self._queue.pop_next(task_type)
                            continue
                    elif local_reqs and self._resources:
                        if not self._resources.check_available(local_reqs):
                            if requested_model_id and self._resources.check_available_for_reuse(
                                local_reqs, requested_model_id, requested_provider_id
                            ):
                                log("system", "info", f"Task {task.id} reusing warm model {requested_model_id}")
                            elif self._resources.check_available_after_unload(local_reqs):
                                # Soft consumers (alive-time models) block the resource; force-unload them.
                                log("system", "info",
                                    f"Task {task.id} needs force-unload of idle models to free resources")
                                await self._resources.force_unload_for(
                                    local_reqs,
                                    self._full_config,
                                    keep_model_id=requested_model_id or None,
                                    keep_provider_id=requested_provider_id or None,
                                )
                                # Re-check actual soft reservations; a failed unload must keep its VRAM occupied.
                                if not self._resources.check_available(local_reqs):
                                    task.next_retry_at = time.time() + 5
                                    deferred_tasks.append(task)
                                    self._note_deferral(task, "the idle models did not free enough memory")
                                    log("system", "warn",
                                        f"Task {task.id} delayed: resource unload did not free enough space")
                                    task_id = await self._queue.pop_next(task_type)
                                    continue
                                # Fall through — resources are now available
                            else:
                                # Not enough even with force unload
                                task.next_retry_at = time.time() + 1
                                deferred_tasks.append(task)
                                self._note_deferral(task, "insufficient resources")
                                log("system", "warning", f"Task {task.id} delayed: insufficient resources")
                                task_id = await self._queue.pop_next(task_type)
                                continue

                        reserved = await self._resources.reserve_if_available_or_reuse(
                            local_reqs,
                            consumer_id=f"{task.id}:{worker.id}",
                            model_id=(task.payload or {}).get("model") or None,
                            provider_id=requested_provider_id,
                        )
                        if not reserved:
                            task.next_retry_at = time.time() + 1
                            deferred_tasks.append(task)
                            self._note_deferral(task, "the resources changed during the admission")
                            log("system", "warning", f"Task {task.id} delayed: resources changed during admission")
                            task_id = await self._queue.pop_next(task_type)
                            continue
                    bg_task = asyncio.create_task(
                        self._run_task(
                            task,
                            worker,
                            local_reqs,
                            resources_reserved=True,
                            model_admission=model_admission,
                        ),
                        name=f"task:{task.id}:{worker.id}",
                    )
                    self._active_runs.add(bg_task)
                    self._idle.clear()
                    bg_task.add_done_callback(self._on_run_done)
                    dispatched = True
                    task_id = await self._queue.pop_next(task_type)

                for deferred_task in deferred_tasks:
                    await self._queue.add_task(deferred_task)

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
        """Select the worker that can serve the resolved provider route."""
        route = (task.config or {}).get("route") if isinstance(task.config, dict) else None
        route = route if isinstance(route, dict) else {}
        resolved_worker = str(route.get("resolved_worker") or "").strip()
        if resolved_worker and resolved_worker in self._workers:
            worker = self._workers[resolved_worker]
            if worker.enabled:
                return worker

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
        if (getattr(task, "config", None) or {}).get("request_timeout") is not None:
            return "request.timeout"
        return "tasks.run_timeout / TASK_RUN_TIMEOUT_SECONDS"

    @staticmethod
    def _queue_timeout_source(task: Task) -> str:
        """Describe where the effective task queue timeout came from."""
        return "tasks.queue_timeout / TASK_QUEUE_TIMEOUT_SECONDS"

    @staticmethod
    def _normalize_utc(value: datetime | None) -> datetime | None:
        """Return a timezone-aware UTC datetime or None when unavailable."""
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @classmethod
    def _queue_elapsed_seconds(cls, task: Task) -> float:
        """Return how long the task has waited before its first execution."""
        created_at = cls._normalize_utc(getattr(task, "created_at", None))
        if created_at is None:
            return 0.0
        return max(0.0, (datetime.now(timezone.utc) - created_at).total_seconds())

    @classmethod
    def _queue_timeout_expired(cls, task: Task) -> bool:
        """Return whether a queued task exceeded its pre-execution queue timeout."""
        if getattr(task, "started_at", None) is not None:
            return False
        try:
            timeout_limit = int(getattr(task, "queue_timeout", 0) or 0)
        except (TypeError, ValueError):
            timeout_limit = 0
        if timeout_limit <= 0:
            return False
        return cls._queue_elapsed_seconds(task) >= timeout_limit

    @classmethod
    def _build_queue_timeout_message(cls, task: Task, elapsed_seconds: float) -> str:
        """Build an informative queue-timeout message for logs and API errors."""
        try:
            timeout_limit = int(task.queue_timeout)
        except (TypeError, ValueError):
            timeout_limit = 0

        timeout_source = cls._queue_timeout_source(task)
        return (
            f"Task queue timeout exceeded after {elapsed_seconds:.2f}s "
            f"(limit={timeout_limit}s, parameter=task.queue_timeout, source={timeout_source})"
        )

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
        resources_reserved: bool = False,
        model_admission: dict | None = None,
    ) -> None:
        """Execute one task; handle timeouts and exceptions."""
        log("worker", "info", f"Starting task {task.id}", worker.id)
        self._clear_deferral(task.id)
        await self._queue.mark_running(task.id, worker.id)
        consumer_id = f"{task.id}:{worker.id}"
        # Model id is used to track soft consumers (alive_time) after release
        model_id: str | None = (task.payload or {}).get("model") or None
        provider_id = self._resolve_task_provider_id(task, worker.id)
        if provider_id:
            task.route_provider_id = provider_id

        if self._resources and reserved_reqs and not resources_reserved:
            await self._resources.reserve_blind_for(
                reserved_reqs,
                consumer_id=consumer_id,
                model_id=model_id,
                provider_id=provider_id,
            )

        model_counted = bool(model_admission is not None)
        if not model_counted and self._resources and model_id and provider_id:
            self._resources.mark_model_started(provider_id, model_id)
            model_counted = True

        started = time.monotonic()
        failed_local_server_start = False

        try:
            if await self._apply_hook_control(task):
                return
            if self._hooks is not None and task.type == "agent":
                await self._hooks.emit("before_llm_request", task, task.payload)
            if await self._apply_hook_control(task):
                return
            result: WorkerResult = await asyncio.wait_for(
                worker.execute(task, emit_chunk=self._make_emitter(task)),
                timeout=task.run_timeout or None,
            )
            if result.ok:
                task.result = result.data
                task.upstream_response = result.upstream_response
                if self._hooks is not None and task.type == "agent":
                    await self._hooks.emit("llm_response_complete", task, result.data)
                if await self._apply_hook_control(task):
                    return
                task.retry_attempt = 0
                task.fallback_index = 0
                task.next_retry_at = 0.0
                if self._hooks is not None:
                    await self._hooks.emit("before_consumer_delivery", task, result.data)
                if await self._apply_hook_control(task):
                    return
                await self._queue.mark_completed(task)
                log("worker", "info", f"Task {task.id} completed", worker.id)
            else:
                err = result.error or {"code": "WORKER_ERROR", "message": "Worker returned error"}
                task.error = err
                if self._hooks is not None and task.type == "agent":
                    await self._hooks.emit("llm_response_complete", task, err)
                if await self._apply_hook_control(task):
                    return
                if await self._fallback_smart_route_after_execution_failure(task, worker.id, err):
                    return
                failed_local_server_start = (
                    self._provider_api(provider_id) == "llama-cpp"
                    and str(err.get("code") or "") in {
                        "INVALID_EXEC_CMD",
                        "LLAMA_CPP_START_FAILED",
                        "UPSTREAM_UNREACHABLE",
                    }
                )
                if self._hooks is not None:
                    await self._hooks.emit("before_consumer_delivery", task, err)
                if await self._apply_hook_control(task):
                    return
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
            if self._resources and model_admission is not None and model_id and provider_id:
                await self._resources.release_model_session(
                    model_admission.get("session_requirements") or {},
                    model_id,
                    provider_id,
                    consumer_id,
                    base_requirements=model_admission.get("base_requirements") or reserved_reqs or {},
                    retain_base=not failed_local_server_start,
                )
            elif self._resources and reserved_reqs:
                await self._resources.release_for(
                    reserved_reqs,
                    consumer_id=consumer_id,
                    model_id=model_id,
                    provider_id=provider_id,
                    retain_model=not failed_local_server_start,
                )
            if self._resources and model_counted and model_admission is None and model_id and provider_id:
                self._resources.mark_model_finished(provider_id, model_id)
            self._clear_remote_tracking(task.id)
            self._clear_deferral(task.id)

    async def _fallback_smart_route_after_execution_failure(
        self,
        task: Task,
        worker_id: str,
        error: dict,
    ) -> bool:
        """Requeue a failed smart route on the next eligible concrete candidate."""
        if str((error or {}).get("code") or "").upper() != "UPSTREAM_ERROR":
            return False

        route_cfg = (task.config or {}).get("route") if isinstance(task.config, dict) else None
        if not isinstance(route_cfg, dict) or str(route_cfg.get("selection") or "") != "smart_route":
            return False

        requested_provider = str(route_cfg.get("requested_provider") or "").strip()
        requested_model = str(
            route_cfg.get("requested_alias")
            or route_cfg.get("requested_model")
            or ""
        ).strip()
        failed_provider = str(route_cfg.get("resolved_provider") or "").strip()
        failed_model = str(route_cfg.get("resolved_model") or "").strip()
        if (
            not requested_provider
            or not requested_model
            or not failed_provider
            or not failed_model
            or self._provider_api(requested_provider) != "smart"
        ):
            return False

        if self._kv_affinity is not None:
            self._kv_affinity.forget_candidate(failed_provider, failed_model)
        if self._model_readiness is not None:
            self._model_readiness.clear_candidate(failed_provider, failed_model)

        request_payload = dict(task.payload or {})
        request_payload["model"] = requested_model
        if isinstance(task.config, dict) and task.config.get("kv_client_key"):
            request_payload["_aidir_kv_key"] = str(task.config.get("kv_client_key") or "")
        incoming_bearer_token = str((task.config or {}).get("incoming_bearer_token") or "").strip()
        base_route = {
            "requested_model": requested_model,
            "requested_alias": str(route_cfg.get("requested_alias") or requested_model).strip(),
            "resolved_provider": requested_provider,
            "resolved_model": requested_model,
        }

        try:
            resolution = await self._make_smart_router(worker_id).resolve_route(
                base_route,
                request_payload=request_payload,
                request_priority=task.priority,
                incoming_bearer_token=incoming_bearer_token,
                excluded_candidates={(failed_provider, failed_model)},
            )
        except SmartRouteError:
            return False
        except Exception as exc:
            log(
                "worker",
                "error",
                f"Task {task.id} smart execution fallback resolution failed: {exc}",
                worker_id,
            )
            return False

        next_route = dict(resolution.route or {})
        next_provider = str(next_route.get("resolved_provider") or "").strip()
        next_model = str(next_route.get("resolved_model") or "").strip()
        if not next_provider or not next_model or (next_provider, next_model) == (failed_provider, failed_model):
            return False

        previous_config = task.config
        previous_payload = task.payload
        previous_requirements = task.resource_requirements
        previous_route_provider_id = task.route_provider_id
        previous_model_id = task.model_id
        previous_worker_id = task.worker_id
        previous_next_retry_at = task.next_retry_at
        task.config = dict(task.config or {})
        task.config["route"] = next_route
        task.payload = dict(task.payload or {})
        task.payload["model"] = next_model
        task.resource_requirements = {}
        task.route_provider_id = next_provider
        task.model_id = next_model
        task.worker_id = str(next_route.get("resolved_worker") or worker_id)
        task.next_retry_at = 0.0
        try:
            await self._queue.add_task(task)
        except Exception as exc:
            task.config = previous_config
            task.payload = previous_payload
            task.resource_requirements = previous_requirements
            task.route_provider_id = previous_route_provider_id
            task.model_id = previous_model_id
            task.worker_id = previous_worker_id
            task.next_retry_at = previous_next_retry_at
            log(
                "worker",
                "error",
                f"Task {task.id} smart execution fallback requeue failed: {exc}",
                worker_id,
            )
            return False
        log(
            "worker",
            "info",
            (
                f"Task {task.id} smart execution fallback: "
                f"{failed_provider}/{failed_model} -> {next_provider}/{next_model}"
            ),
            worker_id,
        )
        return True

    async def _apply_hook_control(self, task: Task) -> bool:
        """Apply a hook-requested retry or cancellation before delivery."""
        if self._hooks is None:
            return False
        reason = self._hooks.consume_cancel_request(task)
        if reason:
            task.error = {"code": "HOOK_CANCELED", "message": reason}
            await self._queue.mark_canceled(task)
            log("worker", "info", f"Task {task.id} canceled by hook: {reason}")
            return True

        retry_requested, provider_id = self._hooks.consume_retry_request(task)
        if not retry_requested:
            return False
        if provider_id:
            task.route_provider_id = provider_id
        task.result = None
        task.error = None
        task.upstream_response = None
        await self._queue.add_task(task, is_retry=True)
        self.notify_new_task()
        log("worker", "info", f"Task {task.id} requeued by hook")
        return True

    async def _expire_queued_task_if_needed(self, task: Task) -> bool:
        """Fail queued tasks that exceeded queue timeout before their first run."""
        if not self._queue_timeout_expired(task):
            return False

        elapsed = self._queue_elapsed_seconds(task)
        timeout_message = self._build_queue_timeout_message(task, elapsed)
        log("worker", "warning", f"Task {task.id} queue timeout: {timeout_message}", "scheduler")
        await self._queue.mark_failed(task, {"code": "QUEUE_TIMEOUT", "message": timeout_message})
        return True

    async def _handle_remote_refusal(
        self,
        task: Task,
        worker: "BaseWorker",
        provider_id: str,
        model_id: str,
        kind: str,
        reason: str,
        deferred_tasks: list,
    ) -> None:
        """Re-route a task whose remote candidate refused it, or fail it with a clear reason.

        A `capacity` refusal is a stable fact: the remote answered and cannot
        run the model. The candidate is excluded for the rest of the task's
        life, so the route moves to a candidate that can take it. When no
        candidate is left the task fails instead of waiting for its whole queue
        timeout for a fact that will not change.

        An `unreachable` refusal is transient: the remote may be restarting. The
        candidate is skipped for this attempt only, so the task goes back to it
        as soon as it answers. While it is down the task waits, bounded by its
        own queue timeout.
        """
        blocked = self._remote_blocked_candidates.setdefault(task.id, set())
        excluded = set(blocked)
        if kind == "capacity":
            blocked.add((provider_id, model_id))
        else:
            excluded.add((provider_id, model_id))
        refreshed = await self._refresh_smart_route_for_dispatch(
            task,
            worker.id,
            excluded_candidates=excluded,
        )
        if refreshed:
            self._remote_refusal_counts.pop(task.id, None)
            # The task was already popped from the queue. It has to go back in,
            # otherwise the new route is never dispatched.
            task.next_retry_at = 0.0
            deferred_tasks.append(task)
            return

        if kind != "capacity":
            # The remote is down. The task waits for it, and every attempt
            # probes it again. The queue timeout is the bound.
            task.next_retry_at = time.time() + 1
            deferred_tasks.append(task)
            self._note_deferral(task, f"waiting for the remote aidir '{provider_id}': {reason}")
            log(
                "system",
                "warning",
                f"Task {task.id} delayed: remote aidir provider '{provider_id}' is unreachable "
                f"({reason}); the task waits for it or is re-routed",
            )
            return

        attempts = self._remote_refusal_counts.get(task.id, 0) + 1
        self._remote_refusal_counts[task.id] = attempts
        if attempts >= _MAX_REMOTE_REFUSALS:
            self._clear_remote_tracking(task.id)
            self._clear_deferral(task.id)
            await self._queue.mark_failed(task, {
                "code": "REMOTE_CANNOT_RUN",
                "message": (
                    f"No candidate can run the task: the remote aidir provider "
                    f"'{provider_id}' cannot run '{model_id}' ({reason})"
                ),
            })
            return

        task.next_retry_at = time.time() + 1
        deferred_tasks.append(task)
        self._note_deferral(task, f"the remote aidir '{provider_id}' cannot run '{model_id}': {reason}")
        log(
            "system",
            "warning",
            f"Task {task.id} delayed: remote aidir provider '{provider_id}' cannot run "
            f"'{model_id}' ({reason}), attempt {attempts}/{_MAX_REMOTE_REFUSALS}",
        )

    def _clear_remote_tracking(self, task_id: str) -> None:
        """Drop the per-task remote refusal bookkeeping."""
        self._remote_blocked_candidates.pop(task_id, None)
        self._remote_refusal_counts.pop(task_id, None)

    def _note_deferral(self, task: Task, reason: str) -> None:
        """Record why a task waits, so the operator can see it in the status."""
        entry = self._deferred.get(task.id)
        if entry is None:
            entry = {"since": time.time()}
            self._deferred[task.id] = entry
        entry["reason"] = reason
        route = (task.config or {}).get("route") if isinstance(task.config, dict) else None
        route = route if isinstance(route, dict) else {}
        entry["provider"] = str(route.get("resolved_provider") or task.route_provider_id or "")
        entry["model"] = str(route.get("resolved_model") or task.model_id or "")
        entry["worker"] = str(task.worker_id or "")

    def _clear_deferral(self, task_id: str) -> None:
        """Drop the deferral record of a task that is no longer waiting."""
        self._deferred.pop(task_id, None)

    def defer_reason_for(self, task_id: str) -> dict:
        """Return why one task waits, or an empty mapping when it does not wait."""
        entry = self._deferred.get(task_id)
        return dict(entry) if entry else {}

    def deferred_tasks_snapshot(self) -> dict:
        """Return the waiting tasks and the reason every one of them waits."""
        by_reason: dict[str, int] = {}
        for entry in self._deferred.values():
            reason = str(entry.get("reason") or "unknown")
            by_reason[reason] = by_reason.get(reason, 0) + 1
        tasks = [
            {
                "id": task_id,
                "reason": entry.get("reason"),
                "provider": entry.get("provider", ""),
                "model": entry.get("model", ""),
                "worker": entry.get("worker", ""),
                "waiting_since": entry.get("since"),
            }
            for task_id, entry in sorted(self._deferred.items(), key=lambda item: item[1].get("since") or 0)
        ]
        return {"count": len(tasks), "by_reason": by_reason, "tasks": tasks[:50]}

    async def _refresh_smart_route_for_dispatch(
        self,
        task: Task,
        worker_id: str,
        excluded_candidates: set[tuple[str, str]] | None = None,
    ) -> bool:
        """Re-resolve queued smart routes so stale busy-fallback selections can move to a newly available candidate.

        Returns whether the resolved route changed.
        """
        route_cfg = (task.config or {}).get("route") if isinstance(task.config, dict) else None
        if not isinstance(route_cfg, dict):
            return False
        if str(route_cfg.get("selection") or "").strip() != "smart_route":
            return False

        requested_provider = str(route_cfg.get("requested_provider") or "").strip()
        requested_model = str(
            route_cfg.get("requested_alias")
            or route_cfg.get("requested_model")
            or route_cfg.get("resolved_model")
            or ""
        ).strip()
        if not requested_provider or not requested_model:
            return False
        if self._provider_api(requested_provider) != "smart":
            return False

        base_route = {
            "requested_model": requested_model,
            "requested_alias": str(route_cfg.get("requested_alias") or requested_model).strip(),
            "resolved_provider": requested_provider,
            "resolved_model": requested_model,
        }
        request_payload = dict(task.payload or {})
        request_payload["model"] = requested_model
        if isinstance(task.config, dict) and task.config.get("kv_client_key"):
            request_payload["_aidir_kv_key"] = str(task.config.get("kv_client_key") or "")
        incoming_bearer_token = ""
        if isinstance(task.config, dict):
            incoming_bearer_token = str(task.config.get("incoming_bearer_token") or "").strip()

        try:
            resolution = await self._make_smart_router(worker_id).resolve_route(
                base_route,
                request_payload=request_payload,
                request_priority=task.priority,
                incoming_bearer_token=incoming_bearer_token,
                excluded_candidates=excluded_candidates,
            )
        except SmartRouteError:
            return False

        next_route = dict(resolution.route or {})
        if not next_route:
            return False

        current_provider = str(route_cfg.get("resolved_provider") or "").strip()
        current_model = str(route_cfg.get("resolved_model") or "").strip()
        next_provider = str(next_route.get("resolved_provider") or "").strip()
        next_model = str(next_route.get("resolved_model") or "").strip()
        if current_provider == next_provider and current_model == next_model:
            task.config = dict(task.config or {})
            task.config["route"] = next_route
            return False

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
        return True

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

    def _resolve_task_provider_id(self, task: Task, worker_id: str) -> str:
        """Return the resolved route provider, falling back to the selected worker provider."""
        route_cfg = (task.config or {}).get("route") if isinstance(task.config, dict) else None
        if isinstance(route_cfg, dict):
            provider_id = str(route_cfg.get("resolved_provider") or "").strip()
            if provider_id:
                return provider_id
        worker_cfg = self._workers_cfg.get(worker_id, {}) or {}
        return str(worker_cfg.get("provider") or "").strip()

    async def _remote_route_can_run(
        self,
        task: Task,
        provider_id: str,
        model_id: str,
    ) -> bool:
        """Ask a remote aidir whether its resolved model can run before forwarding work."""
        ok, _kind, _reason = await self._remote_route_status(task, provider_id, model_id)
        return ok

    async def _remote_route_status(
        self,
        task: Task,
        provider_id: str,
        model_id: str,
    ) -> tuple[bool, str, str]:
        """Ask a remote aidir whether its resolved model can run, and say why not.

        The kind of refusal decides what happens next:

        - `capacity` - the remote answered and cannot run the model now. That is
          a property of its free memory and it does not change while the task
          waits, so the candidate is excluded for good.
        - `unreachable` - the remote did not answer. That is transient: the
          remote may be restarting, so the candidate is skipped for this attempt
          only and the task goes back to it as soon as it answers.

        Returns (may_run, kind, reason).
        """
        if not provider_id or self._provider_api(provider_id) != "openaix":
            return True, "", ""
        if not model_id:
            return False, "unreachable", "the remote model id is empty"
        incoming_bearer_token = ""
        if isinstance(task.config, dict):
            incoming_bearer_token = str(task.config.get("incoming_bearer_token") or "").strip()
        queue_state = await self._probe_remote_model_queue_state(
            provider_id,
            model_id,
            priority=task.priority,
            timeout_ms=1500,
            incoming_bearer_token=incoming_bearer_token,
        )
        if not isinstance(queue_state, dict):
            return False, "unreachable", "the remote aidir did not answer the queue-state probe"
        if not queue_state.get("can_run_now"):
            admission = str(queue_state.get("admission") or "unknown")
            free = queue_state.get("free") or {}
            needed = queue_state.get("needed") or {}
            return (
                False,
                "capacity",
                f"the remote reports admission={admission}, free={free}, needed={needed}",
            )
        return True, "", ""

    def _make_smart_router(self, worker_id: str) -> SmartRouter:
        """Build a shared smart router for scheduler-time route refresh."""

        async def get_local_queue_state(requirements: dict, priority: int) -> dict | None:
            return await self._queue.get_resource_queue_state(requirements, priority=priority)

        async def get_model_queue_state(provider_id: str, model_id: str, priority: int) -> dict | None:
            getter = getattr(self._queue, "get_model_queue_state", None)
            if not callable(getter):
                return None
            return await getter(provider_id, model_id, priority=priority)

        def evaluate_model_admission(requirements: dict, model_id: str, provider_id: str) -> dict | None:
            if self._resources is None:
                return None
            evaluator = getattr(self._resources, "evaluate_model_admission", None)
            if not callable(evaluator):
                return None
            return evaluator(requirements, model_id, provider_id)

        def check_resource_available(requirements: dict) -> bool:
            if self._resources is None:
                return False
            return bool(self._resources.check_available(requirements))

        def check_resource_available_after_unload(requirements: dict) -> bool:
            if self._resources is None:
                return False
            return bool(self._resources.check_available_after_unload(requirements))

        def check_resource_available_for_reuse(
            requirements: dict,
            model_id: str,
            provider_id: str,
        ) -> bool:
            if self._resources is None:
                return False
            checker = getattr(self._resources, "check_available_for_reuse", None)
            return bool(checker(requirements, model_id, provider_id)) if callable(checker) else False

        return SmartRouter(
            endpoint_id="scheduler",
            default_worker_id=worker_id,
            find_provider_model_cfg=self._find_provider_model_cfg,
            provider_api=self._provider_api,
            resolve_model_resource_requirements=self._resolve_model_resource_requirements,
            get_local_queue_state=get_local_queue_state,
            check_resource_available=check_resource_available,
            check_resource_available_after_unload=check_resource_available_after_unload,
            check_resource_available_for_reuse=check_resource_available_for_reuse,
            probe_remote_model_queue_state=self._probe_remote_model_queue_state,
            probe_ollama_model_availability=self._probe_ollama_model_availability,
            probe_openai_model_availability=self._probe_openai_model_availability,
            resolve_probe_timeout_ms=self._resolve_probe_timeout_ms,
            resolve_worker_id_for_route=self._resolve_worker_id_for_route,
            kv_affinity=self._kv_affinity,
            readiness=self._model_readiness,
            affinity_config=self._affinity_config,
            get_model_queue_state=get_model_queue_state,
            evaluate_model_admission=evaluate_model_admission,
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
        if self._provider_api(provider_id) == "llama-cpp":
            if "call_llama_cpp" in self._workers:
                return "call_llama_cpp"
            return worker_id
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

                    if response.status_code in {401, 403}:
                        # The peer publishes its state only with a token, and
                        # this instance has none. Say so once, instead of
                        # letting the candidate disappear silently.
                        self._warn_peer_unauthorized(provider_id, response.status_code)
                        return None

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

    async def _probe_openai_model_availability(
        self,
        provider_id: str,
        model_id: str,
        *,
        timeout_ms: int,
        incoming_bearer_token: str = "",
    ) -> bool:
        """Confirm an OpenAI-compatible provider lists the requested model."""
        provider_cfg = self._provider_cfg(provider_id)
        base_url = str(provider_cfg.get("baseUrl") or "").rstrip("/")
        if base_url.endswith("/v1"):
            base_url = base_url[:-3]
        if not base_url:
            return False
        headers = self._resolve_probe_headers(provider_id, incoming_bearer_token)
        if self._provider_api(provider_id) == "anthropic":
            headers.setdefault("anthropic-version", str(provider_cfg.get("anthropic_version") or "2023-06-01"))
        try:
            async with httpx.AsyncClient(timeout=max(0.001, timeout_ms / 1000.0), headers=headers) as client:
                response = await client.get(f"{base_url}/v1/models")
            if not 200 <= response.status_code < 300:
                return False
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return False
        items = payload.get("data") if isinstance(payload, dict) else None
        return any(isinstance(item, dict) and str(item.get("id") or "") == model_id for item in items or [])

    async def _probe_ollama_model_availability(
        self,
        provider_id: str,
        model_id: str,
        *,
        timeout_ms: int,
        incoming_bearer_token: str = "",
    ) -> bool:
        """Confirm that an Ollama or llama.cpp provider reports the requested model."""
        provider_cfg = self._provider_cfg(provider_id)
        base_url = str(provider_cfg.get("baseUrl") or "").rstrip("/")
        if not base_url:
            return False

        api_type = self._provider_api(provider_id)

        headers = self._resolve_probe_headers(provider_id, incoming_bearer_token)
        timeout_seconds = max(0.001, timeout_ms / 1000.0)

        try:
            async with httpx.AsyncClient(timeout=timeout_seconds, headers=headers) as client:
                response = await client.get(f"{base_url}/v1/models" if api_type == "llama-cpp" else f"{base_url}/api/tags")
        except httpx.HTTPError:
            if api_type == "llama-cpp" and str(provider_cfg.get("exec_cmd") or "").strip():
                return True
            return False

        if response.status_code < 200 or response.status_code >= 300:
            return False

        try:
            payload = response.json()
        except Exception:
            return False

        models = payload.get("data") if api_type == "llama-cpp" and isinstance(payload, dict) else payload.get("models") if isinstance(payload, dict) else None
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

    def _warn_peer_unauthorized(self, provider_id: str, status_code: int) -> None:
        """Report once that a peer refused this instance because it has no token."""
        key = f"peer-auth:{provider_id}"
        if key in self._peer_auth_warned:
            return
        self._peer_auth_warned.add(key)
        log(
            "system",
            "warning",
            (
                f"The peer provider '{provider_id}' answered HTTP {status_code}: it publishes its "
                "state only with a token, and this instance sends none. Add auth to the provider, "
                "or set the peer's peer_tasks/peer_resources auth_mode to disabled."
            ),
            "routing",
        )

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

        async def _requeue() -> bool:
            """Requeue the failed task, keeping the original error when persistence fails."""
            try:
                await self._queue.add_task(task)
            except Exception as exc:
                log("worker", "error", f"Task {task.id} requeue failed: {exc}", worker_id)
                return False
            return True

        if action == "retry":
            if task.retry_attempt >= retry_count:
                return False
            _schedule_retry()
            if not await _requeue():
                return False
            log("worker", "info", f"Task {task.id} retry scheduled #{task.retry_attempt}", worker_id)
            return True

        if action in {"fallback", "fallback-retry"}:
            if not fallbacks:
                return False

            if task.fallback_index < len(fallbacks):
                task.worker_id = fallbacks[task.fallback_index]
                task.fallback_index += 1
                task.next_retry_at = time.time() + max(0, retry_period)
                if not await _requeue():
                    return False
                log("worker", "info", f"Task {task.id} fallback -> {task.worker_id}", worker_id)
                return True

            # End of fallback chain
            if action == "fallback-retry" and task.retry_attempt < retry_count:
                _schedule_retry()
                task.fallback_index = 0
                task.worker_id = fallbacks[0]
                task.fallback_index = 1
                if not await _requeue():
                    return False
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
