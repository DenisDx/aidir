"""
AI Director core service.
Entry point: python core/app.py
Starts all endpoint servers, webui backend, task scheduler in one asyncio loop.
Handles SIGHUP for config reload.
"""
from __future__ import annotations

import asyncio
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import copy

# Ensure project root is importable before internal imports when launched as
# `python /path/to/core/app.py` from systemd.
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import redis.asyncio as aioredis
import uvicorn

from core.config import Config, config as _global_config
from core.error_logging import log_exception
from core.envid import EnvidRegistry
from core.logger import logger
from core.queue_manager import QueueManager
from core.resources import Resources
from core.scheduler import Scheduler
from core.task import STATUS_CREATED, STATUS_QUEUED, STATUS_RUNNING
from core.task import STATUS_CANCELED, STATUS_COMPLETED, STATUS_FAILED
from core.workers_loader import load_workers, resolve_worker_configs
from core.config_merger import update_config
from core.endpoints.endpoint_ollama import Endpoint_ollama
from core.endpoints.endpoint_openaix import Endpoint_openaix
from core.endpoints.endpoint_mcp import Endpoint_mcp
from core import log


@dataclass
class ServiceBusyError(Exception):
    """Service is temporarily unavailable for new external tasks."""

    message: str
    code: str = "SERVICE_BUSY"


@dataclass
class ShutdownReport:
    """Graceful shutdown outcome summary."""

    timed_out: bool
    active_tasks: int


class Core:
    """
    Central orchestrator.
    Exposes the API consumed by endpoints and workers:
      - on_task_added(task)
      - on_task_complete(task)
      - delete_task(task_id)
    """

    def __init__(self) -> None:
        self.config: Config = _global_config
        self.redis: aioredis.Redis | None = None
        self.queue: QueueManager | None = None
        self.scheduler: Scheduler | None = None
        self.workers: dict = {}
        self.workers_cfg: dict[str, dict] = {}
        self.loop_workers: list[tuple[str, object]] = []
        self.resources = Resources([])
        self.envid_registry: EnvidRegistry | None = None
        self._background_tasks: set[asyncio.Task] = set()
        self._restart_requested = False
        self._shutdown_started = False
        self._shutdown_lock = asyncio.Lock()

    def _track_background_task(self, task: asyncio.Task) -> None:
        """Track a background task and log unexpected failures."""
        self._background_tasks.add(task)

        def _done(done_task: asyncio.Task) -> None:
            self._background_tasks.discard(done_task)
            try:
                exc = done_task.exception()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log("system", "error", f"Background task status check failed: {exc}")
                return

            if exc is not None:
                log("system", "error", f"Background task failed: {exc}")

        task.add_done_callback(_done)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        log("core", "info", "Core starting")

        # ── Redis ──────────────────────────────────────────────────────────
        redis_cfg = self.config.get("redis") or {}
        redis_url = (
            f"redis://:{redis_cfg.get('password')}@{redis_cfg.get('host','127.0.0.1')}"
            f":{redis_cfg.get('port', 6379)}"
            if redis_cfg.get("password")
            else f"redis://{redis_cfg.get('host','127.0.0.1')}:{redis_cfg.get('port', 6379)}"
        )
        self.redis = aioredis.from_url(redis_url, decode_responses=True)
        try:
            await self.redis.ping()
            log("core", "info", "Redis connected")
        except Exception as exc:
            log("system", "error", f"Redis connection failed: {exc}")
            raise

        # ── Queue ──────────────────────────────────────────────────────────
        instance = self.config.get("instance", "aidir")
        self.queue = QueueManager(self.redis, instance, status_change_callback=self._on_task_status_change)

        # ── Envid registry ─────────────────────────────────────────────────
        self.envid_registry = EnvidRegistry(self.redis, instance)
        await self.envid_registry.load_from_redis()
        await self.envid_registry.merge_from_config(self.config.raw())

        # ── Workers ────────────────────────────────────────────────────────
        workers_cfg = resolve_worker_configs(self.config.raw())
        self.workers_cfg = workers_cfg
        self.workers = load_workers(self.config.raw(), workers_cfg=workers_cfg)
        self.resources = Resources(self.config.get("resources") or [])
        self.resources.set_redis(self.redis, instance)
        # Initialize each worker with its config section
        for wid, w in self.workers.items():
            setattr(w, "_core", self)
            await w.initialize({**workers_cfg.get(wid, {}), "_core": self})

        self.loop_workers = []
        for wid, worker in self.workers.items():
            worker_loop = getattr(worker, "loop", None)
            if callable(worker_loop):
                self.loop_workers.append((wid, worker))

        if self.loop_workers:
            log(
                "core",
                "info",
                f"loop_workers initialized: {[wid for wid, _ in self.loop_workers]}",
            )

        # ── Scheduler ─────────────────────────────────────────────────────
        self.scheduler = Scheduler(
            self.queue,
            self.workers,
            workers_cfg=workers_cfg,
            resources=self.resources,
            full_config=self.config.raw(),
        )

        log("core", "info", "Core started")

    async def stop(self) -> None:
        started_at = time.monotonic()
        log("core", "info", "Core stopping")
        
        cancel_started = time.monotonic()
        for task in list(self._background_tasks):
            task.cancel()
        cancel_elapsed = time.monotonic() - cancel_started
        log("core", "info", f"Background task cancellation started in {cancel_elapsed:.2f}s")
        
        if self._background_tasks:
            log("core", "info", f"Stopping background tasks: count={len(self._background_tasks)}")
            gather_started = time.monotonic()
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            gather_elapsed = time.monotonic() - gather_started
            log("core", "info", f"Background tasks stopped in {gather_elapsed:.2f}s")
        
        if self.scheduler:
            log("core", "info", "Stopping scheduler")
            sched_started = time.monotonic()
            self.scheduler.stop()
            sched_elapsed = time.monotonic() - sched_started
            log("core", "info", f"Scheduler stopped in {sched_elapsed:.2f}s")
        
        if self.redis:
            log("core", "info", "Closing Redis connection")
            redis_started = time.monotonic()
            await self.redis.aclose()
            redis_elapsed = time.monotonic() - redis_started
            log("core", "info", f"Redis connection closed in {redis_elapsed:.2f}s")
        
        total_elapsed = time.monotonic() - started_at
        log("core", "info", f"Core stopping completed in {total_elapsed:.3f}s")

    def reload_config(self) -> None:
        """Reload config from disk (triggered by SIGHUP)."""
        try:
            self.config.load()
            log("system", "info", "Config reloaded")
        except Exception as exc:
            log("system", "error", f"Config reload failed: {exc}")

    # ── Core API ──────────────────────────────────────────────────────────────

    async def on_task_added(self, task) -> None:
        """Enqueue task and notify scheduler. Called by endpoints."""
        if self._restart_requested and getattr(task, "external", False):
            raise ServiceBusyError(
                message="Service restart in progress; new tasks are temporarily disabled",
                code="RESTART_IN_PROGRESS",
            )

        max_depth = self.task_parent_chain_max_depth()
        depth = self._task_parent_chain_depth(task)
        if depth > max_depth:
            error = {
                "code": "TASK_PARENT_CHAIN_TOO_DEEP",
                "message": f"Task parent chain depth {depth} exceeds limit {max_depth}",
            }
            task.status = STATUS_FAILED
            task.error = error
            task.finished_at = datetime.now(timezone.utc)
            if self.queue:
                await self.queue.mark_failed(task, error)
                await self.queue.delete_task(task.id)
            raise ServiceBusyError(message=error["message"], code=error["code"])

        await self.queue.add_task(task)
        self.scheduler.notify_new_task()
        log("system", "info", f"Task {task.id} queued (type={task.type})")

    def get_runtime_status(self) -> dict:
        """Return runtime status flags for API/UI."""
        active_tasks = self.scheduler.active_task_count() if self.scheduler else 0
        return {
            "restart_requested": self._restart_requested,
            "accepting_new_tasks": not self._restart_requested,
            "active_tasks": active_tasks,
            "restart_wait_timeout": self.restart_wait_timeout_seconds(),
        }

    def get_effective_worker_config(self, worker_id: str, envid_id: str | None = None) -> dict:
        """Return worker config merged with envid-specific workers override when available."""
        base_cfg = copy.deepcopy(self.workers_cfg.get(worker_id, {}) or {})
        if not envid_id or self.envid_registry is None:
            return base_cfg

        envid = self.envid_registry.get(envid_id)
        if envid is None or not isinstance(envid.workers, dict):
            return base_cfg

        override = envid.workers.get(worker_id)
        if not isinstance(override, dict):
            return base_cfg

        update_config(base_cfg, copy.deepcopy(override))
        return base_cfg

    async def request_restart(self) -> None:
        """Switch service into restart-drain mode."""
        if self._restart_requested:
            return
        self._restart_requested = True
        await self._cancel_pending_external_tasks()
        if self.scheduler:
            self.scheduler.notify_new_task()
        log("system", "info", "Restart requested; new external tasks are blocked")

    def restart_wait_timeout_seconds(self) -> int:
        """Return graceful restart wait timeout in seconds."""
        tasks_cfg = self.config.get("tasks") or {}
        return max(0, int(tasks_cfg.get("restart_wait_timeout", 120) or 0))

    def stop_wait_timeout_seconds(self) -> int:
        """Return stop wait timeout for signal-triggered shutdowns in seconds."""
        tasks_cfg = self.config.get("tasks") or {}
        configured = tasks_cfg.get("stop_wait_timeout")
        if configured is not None:
            return max(0, int(configured or 0))
        return min(self.restart_wait_timeout_seconds(), 10)

    async def graceful_shutdown(self, source: str = "signal") -> ShutdownReport:
        """Stop admission, wait for active tasks, then stop the scheduler."""
        started = time.monotonic()
        async with self._shutdown_lock:
            if self._shutdown_started:
                active_tasks = self.scheduler.active_task_count() if self.scheduler else 0
                return ShutdownReport(timed_out=False, active_tasks=active_tasks)

            self._shutdown_started = True

        await self.request_restart()
        log("core", "info", f"Graceful shutdown: request_restart completed in {time.monotonic() - started:.2f}s")

        timed_out = False
        active_tasks = 0
        if self.scheduler:
            timeout = self.stop_wait_timeout_seconds() if source == "signal" else self.restart_wait_timeout_seconds()
            labels = self.scheduler.active_task_labels()
            log(
                "system",
                "warn",
                f"Graceful shutdown started via {source}; active_tasks={len(labels)} timeout={timeout}s labels={labels}",
            )
            
            wait_started = time.monotonic()
            timed_out = not await self.scheduler.wait_for_active_tasks(timeout)
            wait_elapsed = time.monotonic() - wait_started
            active_tasks = self.scheduler.active_task_count()
            log("core", "info", f"Graceful shutdown: wait_for_active_tasks returned in {wait_elapsed:.2f}s timed_out={timed_out} active_tasks={active_tasks}")

            if timed_out:
                remaining = self.scheduler.active_task_labels()
                log(
                    "system",
                    "warn",
                    f"Graceful shutdown wait expired via {source}; canceling active tasks count={active_tasks} labels={remaining}",
                )
                cancel_started = time.monotonic()
                canceled = await self.scheduler.cancel_active_tasks(timeout=5.0)
                cancel_elapsed = time.monotonic() - cancel_started
                active_tasks = self.scheduler.active_task_count()
                log("core", "info", f"Graceful shutdown: cancel_active_tasks completed in {cancel_elapsed:.2f}s canceled={canceled} remaining={active_tasks}")
                log(
                    "system",
                    "warn",
                    f"Active task cancellation finished; canceled={canceled} remaining={active_tasks}",
                )

            stop_started = time.monotonic()
            self.scheduler.stop()
            stop_elapsed = time.monotonic() - stop_started
            log("core", "info", f"Graceful shutdown: scheduler.stop() completed in {stop_elapsed:.2f}s")
            log("system", "info", f"Scheduler stop requested via {source}")

        total_elapsed = time.monotonic() - started
        if timed_out:
            log(
                "system",
                "warn",
                f"Graceful shutdown timed out via {source} with {active_tasks} active task(s) remaining",
            )
        else:
            log("system", "info", f"Graceful shutdown drain completed via {source}")
        
        log("core", "info", f"Graceful shutdown: total time {total_elapsed:.2f}s timed_out={timed_out} active_tasks={active_tasks}")

        return ShutdownReport(timed_out=timed_out, active_tasks=active_tasks)

    async def _cancel_pending_external_tasks(self) -> None:
        """Cancel queued external tasks so they do not get stranded across restart."""
        if not self.queue:
            return

        for task in list(self.queue.list_tasks()):
            if not getattr(task, "external", False):
                continue
            if task.status not in {STATUS_CREATED, STATUS_QUEUED}:
                continue
            await self.queue.mark_canceled(task)
            await self.queue.delete_task(task.id)
            log("system", "info", f"Queued external task canceled for restart: {task.id}")

    async def on_task_complete(self, task) -> None:
        """
        Called by workers on completion.
        TODO: check and notify parent tasks in chains.
        """
        await self.queue.mark_completed(task)

    async def delete_task(self, task_id: str) -> None:
        """
        Remove task from queue and memory.
        TODO: recursively delete child tasks.
        """
        await self.queue.delete_task(task_id)

    def task_parent_chain_max_depth(self) -> int:
        """Return the maximum allowed parent callback chain depth."""
        tasks_cfg = self.config.get("tasks") or {}
        return max(1, int(tasks_cfg.get("task_parent_chain_max_depth", 100) or 100))

    def _task_parent_chain_depth(self, task) -> int:
        """Count nested parent_context.history links for a task."""
        depth = 0
        history = task.parent_context if isinstance(getattr(task, "parent_context", None), dict) else {}
        parent_worker = getattr(task, "parent_worker", None)

        while parent_worker:
            depth += 1
            if depth > 1000:
                break
            if not isinstance(history, dict):
                break
            history = history.get("history")
            if not isinstance(history, dict):
                break
            parent_worker = history.get("parent_worker")

        return depth

    async def _on_task_status_change(self, task) -> None:
        """Notify the top-level parent worker about any task status transition."""
        parent_worker_id = getattr(task, "parent_worker", None)
        if not parent_worker_id:
            return

        worker = self.workers.get(parent_worker_id)
        if worker is None:
            log("system", "critical", f"parent_worker {parent_worker_id} not found for task {task.id}")
            return

        try:
            await worker.on_child_task(task)
        except Exception as exc:
            log("system", "error", f"parent_worker callback failed for task {task.id}: {exc}")

    async def run_loop_workers_cycle(self, start_index: int = 0) -> int:
        """Run loop() for all loop workers in rotated order and return next start index."""
        if not self.loop_workers:
            return 0

        total = len(self.loop_workers)
        start = start_index % total
        ordered = self.loop_workers[start:] + self.loop_workers[:start]

        for wid, worker in ordered:
            try:
                await worker.loop()
            except Exception as exc:
                log("system", "error", f"loop() failed for worker {wid}: {exc}", "cron")

        return (start + 1) % total


# ── Server builders ───────────────────────────────────────────────────────────


class _Server(uvicorn.Server):
    """uvicorn Server that does not install its own signal handlers.
    Allows the main asyncio loop to handle SIGTERM/SIGINT globally.
    """
    def install_signal_handlers(self) -> None:
        pass


def _build_ollama_server(core: Core, ep_cfg: dict) -> uvicorn.Server:
    endpoint = Endpoint_ollama(ep_cfg)
    app = endpoint.create_app(core)
    asyncio.get_event_loop().run_until_complete(endpoint.initialize(core)) \
        if False else None  # will be awaited in main()
    host = ep_cfg.get("bindAddress", "0.0.0.0")
    port = int(ep_cfg.get("port", 21434))
    cfg = uvicorn.Config(app, host=host, port=port, log_level="warning")
    return _Server(cfg), endpoint


def _build_openaix_server(core: Core, ep_cfg: dict) -> uvicorn.Server:
    endpoint = Endpoint_openaix(ep_cfg)
    app = endpoint.create_app(core)
    asyncio.get_event_loop().run_until_complete(endpoint.initialize(core)) \
        if False else None
    host = ep_cfg.get("bindAddress", "0.0.0.0")
    port = int(ep_cfg.get("port", 21434))
    cfg = uvicorn.Config(app, host=host, port=port, log_level="warning")
    return _Server(cfg), endpoint


def _build_mcp_server(core: Core, ep_cfg: dict) -> uvicorn.Server:
    """Build MCP endpoint server from endpoint config."""
    endpoint = Endpoint_mcp(ep_cfg)
    app = endpoint.create_app(core)
    asyncio.get_event_loop().run_until_complete(endpoint.initialize(core)) \
        if False else None
    host = ep_cfg.get("bindAddress", "0.0.0.0")
    port = int(ep_cfg.get("port", 20001))
    cfg = uvicorn.Config(app, host=host, port=port, log_level="warning")
    return _Server(cfg), endpoint


async def _init_endpoints(core: Core) -> list[tuple[uvicorn.Server, object]]:
    servers = []
    for ep_cfg in (core.config.get("endpoints") or []):
        api = ep_cfg.get("api")
        if api == "ollama":
            server, ep = _build_ollama_server(core, ep_cfg)
            await ep.initialize(core)
            servers.append((server, ep))
        elif api == "openaix":
            server, ep = _build_openaix_server(core, ep_cfg)
            await ep.initialize(core)
            servers.append((server, ep))
        elif api == "mcp":
            server, ep = _build_mcp_server(core, ep_cfg)
            await ep.initialize(core)
            servers.append((server, ep))
        else:
            log("system", "warn", f"Endpoint api={api} not implemented yet, skipping")
    return servers


def _build_webui_server(core: Core, restart_callback=None) -> uvicorn.Server:
    from webui.backend.app import create_app
    app = create_app(core, restart_callback=restart_callback)
    webui_cfg = core.config.get("webui") or {}
    host = webui_cfg.get("bind", "127.0.0.1")
    port = int(webui_cfg.get("port", 20080))
    cfg = uvicorn.Config(app, host=host, port=port, log_level="warning")
    return _Server(cfg)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    log("core", "info", "=== APPLICATION STARTUP BEGIN ===")
    core = Core()
    await core.start()
    log("core", "info", "Core initialization completed")

    loop = asyncio.get_running_loop()
    all_servers: list[uvicorn.Server] = []
    restart_via_self_exit = False

    async def _shutdown_async(source: str) -> None:
        """Drain active work and then stop all uvicorn servers."""
        nonlocal restart_via_self_exit
        shutdown_started = time.monotonic()
        log("core", "info", f"Graceful shutdown BEGIN (source={source})")
        if source == "webui":
            restart_via_self_exit = True
        
        drain_started = time.monotonic()
        report = await core.graceful_shutdown(source=source)
        drain_elapsed = time.monotonic() - drain_started
        log("core", "info", f"Graceful shutdown drain complete in {drain_elapsed:.2f}s: timed_out={report.timed_out} active_tasks={report.active_tasks}")
        
        servers_started = time.monotonic()
        for srv in all_servers:
            log("core", "info", f"Requesting server stop: {type(srv).__name__}")
            srv.should_exit = True
        servers_elapsed = time.monotonic() - servers_started
        log("core", "info", f"All server stop requests sent in {servers_elapsed:.2f}s (source={source})")
        
        total_elapsed = time.monotonic() - shutdown_started
        log("core", "info", f"_shutdown_async total: {total_elapsed:.2f}s")

    def _schedule_shutdown(source: str) -> None:
        """Schedule async shutdown from signal-safe contexts."""
        loop.create_task(_shutdown_async(source))

    def _loop_exception_handler(_loop: asyncio.AbstractEventLoop, context: dict) -> None:
        """Log unhandled asyncio loop exceptions into application logs."""
        exc = context.get("exception")
        message = str(context.get("message") or "Unhandled asyncio loop exception")
        if exc is not None:
            log_exception("system", "asyncio", message, exc)
        else:
            log("system", "error", f"{message}; context={context}", "asyncio")

    loop.set_exception_handler(_loop_exception_handler)

    # SIGHUP → reload config
    try:
        loop.add_signal_handler(signal.SIGHUP, core.reload_config)
    except (NotImplementedError, AttributeError):
        pass  # Windows

    endpoint_servers = await _init_endpoints(core)
    webui_server = _build_webui_server(
        core,
        restart_callback=lambda: _shutdown_async("webui"),
    )
    all_servers.extend([s for s, _ in endpoint_servers] + [webui_server])

    def _shutdown() -> None:
        """Start graceful shutdown from SIGTERM/SIGINT."""
        log("core", "info", "SIGTERM/SIGINT received; initiating graceful shutdown")
        _schedule_shutdown("signal")

    try:
        loop.add_signal_handler(signal.SIGTERM, _shutdown)
        loop.add_signal_handler(signal.SIGINT, _shutdown)
    except (NotImplementedError, AttributeError):
        pass  # Windows

    # Gather: scheduler + all servers
    coroutines = [core.scheduler.run()]
    for server, _ in endpoint_servers:
        coroutines.append(server.serve())
    coroutines.append(webui_server.serve())

    log("core", "info", "All services starting")
    try:
        await asyncio.gather(*coroutines)
    finally:
        log("core", "info", "Application shutdown: executing core.stop()")
        await core.stop()
        log("core", "info", "=== APPLICATION SHUTDOWN COMPLETE ===")

    if restart_via_self_exit:
        raise SystemExit(1)


if __name__ == "__main__":
    log("core", "info", "=== PROCESS STARTED ===")
    try:
        asyncio.run(main())
    except BaseException as exc:
        log_exception("system", "fatal", "Fatal crash in core entrypoint", exc)
        raise
    finally:
        log("core", "info", "=== PROCESS EXITED ===")
