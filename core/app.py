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
from dataclasses import dataclass
from pathlib import Path

# Ensure project root is importable before internal imports when launched as
# `python /path/to/core/app.py` from systemd.
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import redis.asyncio as aioredis
import uvicorn

from core.config import Config, config as _global_config
from core.logger import logger
from core.queue_manager import QueueManager
from core.resources import Resources
from core.scheduler import Scheduler
from core.task import STATUS_CREATED, STATUS_QUEUED, STATUS_RUNNING
from core.workers_loader import load_workers, resolve_worker_configs
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
        self.loop_workers: list[tuple[str, object]] = []
        self.resources = Resources([])
        self._restart_requested = False
        self._shutdown_started = False
        self._shutdown_lock = asyncio.Lock()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        log("system", "info", "Core starting")

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
            log("system", "info", "Redis connected")
        except Exception as exc:
            log("system", "error", f"Redis connection failed: {exc}")
            raise

        # ── Queue ──────────────────────────────────────────────────────────
        instance = self.config.get("instance", "aidir")
        self.queue = QueueManager(self.redis, instance)

        # ── Workers ────────────────────────────────────────────────────────
        workers_cfg = resolve_worker_configs(self.config.raw())
        self.workers = load_workers(self.config.raw(), workers_cfg=workers_cfg)
        self.resources = Resources(self.config.get("resources") or [])
        # Initialize each worker with its config section
        for wid, w in self.workers.items():
            await w.initialize({**workers_cfg.get(wid, {}), "_core": self})

        self.loop_workers = []
        for wid, worker in self.workers.items():
            worker_loop = getattr(worker, "loop", None)
            if callable(worker_loop):
                self.loop_workers.append((wid, worker))

        if self.loop_workers:
            log(
                "system",
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

        log("system", "info", "Core started")

    async def stop(self) -> None:
        log("system", "info", "Core stopping")
        if self.scheduler:
            self.scheduler.stop()
        if self.redis:
            await self.redis.aclose()

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

    async def graceful_shutdown(self) -> ShutdownReport:
        """Stop admission, wait for active tasks, then stop the scheduler."""
        async with self._shutdown_lock:
            if self._shutdown_started:
                active_tasks = self.scheduler.active_task_count() if self.scheduler else 0
                return ShutdownReport(timed_out=False, active_tasks=active_tasks)

            self._shutdown_started = True

        await self.request_restart()

        timed_out = False
        active_tasks = 0
        if self.scheduler:
            timeout = self.restart_wait_timeout_seconds()
            timed_out = not await self.scheduler.wait_for_active_tasks(timeout)
            active_tasks = self.scheduler.active_task_count()
            self.scheduler.stop()

        if timed_out:
            log(
                "system",
                "warn",
                f"Graceful shutdown timed out with {active_tasks} active task(s) remaining",
            )
        else:
            log("system", "info", "Graceful shutdown drain completed")

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
    core = Core()
    await core.start()

    loop = asyncio.get_running_loop()
    all_servers: list[uvicorn.Server] = []
    restart_via_self_exit = False

    async def _shutdown_async(source: str) -> None:
        """Drain active work and then stop all uvicorn servers."""
        nonlocal restart_via_self_exit
        log("system", "info", f"Shutdown requested via {source}")
        if source == "webui":
            restart_via_self_exit = True
        await core.graceful_shutdown()
        for srv in all_servers:
            srv.should_exit = True

    def _schedule_shutdown(source: str) -> None:
        """Schedule async shutdown from signal-safe contexts."""
        loop.create_task(_shutdown_async(source))

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

    log("system", "info", "All services started")
    try:
        await asyncio.gather(*coroutines)
    finally:
        await core.stop()

    if restart_via_self_exit:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
