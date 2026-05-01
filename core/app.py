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
from core.scheduler import Scheduler
from core.workers_loader import load_workers
from core.endpoints.endpoint_ollama import Endpoint_ollama
from core import log


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
        self.workers = load_workers(self.config.raw())
        # Initialize each worker with its config section
        items_cfg = self.config.get("workers", {}).get("items", {}) or {}
        for wid, w in self.workers.items():
            await w.initialize({**items_cfg.get(wid, {}), "_core": self})

        # ── Scheduler ─────────────────────────────────────────────────────
        self.scheduler = Scheduler(self.queue, self.workers)

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
        await self.queue.add_task(task)
        self.scheduler.notify_new_task()
        log("system", "info", f"Task {task.id} queued (type={task.type})")

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


# ── Server builders ───────────────────────────────────────────────────────────

def _build_ollama_server(core: Core, ep_cfg: dict) -> uvicorn.Server:
    endpoint = Endpoint_ollama(ep_cfg)
    app = endpoint.create_app(core)
    asyncio.get_event_loop().run_until_complete(endpoint.initialize(core)) \
        if False else None  # will be awaited in main()
    host = ep_cfg.get("bindAddress", "0.0.0.0")
    port = int(ep_cfg.get("port", 21434))
    cfg = uvicorn.Config(app, host=host, port=port, log_level="warning")
    return uvicorn.Server(cfg), endpoint


async def _init_endpoints(core: Core) -> list[tuple[uvicorn.Server, object]]:
    servers = []
    for ep_cfg in (core.config.get("endpoints") or []):
        api = ep_cfg.get("api")
        if api == "ollama":
            server, ep = _build_ollama_server(core, ep_cfg)
            await ep.initialize(core)
            servers.append((server, ep))
        else:
            log("system", "warn", f"Endpoint api={api} not implemented yet, skipping")
    return servers


def _build_webui_server(core: Core) -> uvicorn.Server:
    from webui.backend.app import create_app
    app = create_app(core)
    webui_cfg = core.config.get("webui") or {}
    host = webui_cfg.get("bind", "127.0.0.1")
    port = int(webui_cfg.get("port", 20080))
    cfg = uvicorn.Config(app, host=host, port=port, log_level="warning")
    return uvicorn.Server(cfg)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    core = Core()
    await core.start()

    # SIGHUP → reload config
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGHUP, core.reload_config)
    except (NotImplementedError, AttributeError):
        pass  # Windows

    endpoint_servers = await _init_endpoints(core)
    webui_server = _build_webui_server(core)

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


if __name__ == "__main__":
    asyncio.run(main())
