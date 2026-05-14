"""
Task scheduler.
Background asyncio loop that pops tasks from the queue and dispatches them
to the appropriate worker. Runs concurrently with uvicorn servers.
TODO: add resource availability checks before dispatching (VRAM etc.).
"""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from core.task import Task, STATUS_QUEUED
from core.worker import WorkerResult
from core import log

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
            log("worker", "error", f"Task {task.id} timed out", worker.id)
            err = {"code": "TIMEOUT", "message": "Task run timeout exceeded"}
            if not await self._handle_reject(task, worker.id, err):
                await self._queue.mark_failed(task, err)

        except Exception as exc:
            log("worker", "error", f"Task {task.id} exception: {exc}", worker.id)
            err = {"code": "EXCEPTION", "message": str(exc)}
            if not await self._handle_reject(task, worker.id, err):
                await self._queue.mark_failed(task, err)

        finally:
            if self._resources and reserved_reqs:
                await self._resources.release_for(reserved_reqs, consumer_id=consumer_id, model_id=model_id)

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

        model_id = (task.payload or {}).get("model")
        provider_id = wcfg.get("provider")
        if model_id and provider_id:
            providers = (((self._full_config or {}).get("models") or {}).get("providers") or {})
            p = providers.get(provider_id) or {}
            for model in (p.get("models") or []):
                if model.get("id") != model_id:
                    continue
                for rid, vals in (model.get("resources") or {}).items():
                    merged.setdefault(rid, {})
                    for k, v in (vals or {}).items():
                        merged[rid][k] = int(merged[rid].get(k, 0)) + int(v)
                break

        task.resource_requirements = merged
        return merged

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
