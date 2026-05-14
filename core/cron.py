"""
Cron script for periodic maintenance tasks.
Run via crontab every CRON_PERIOD seconds:
  */1 * * * * /path/to/aidir/venv/bin/python /path/to/aidir/core/cron.py

The script uses a Redis key to track when each periodic job was last run,
so not every job runs on every cron invocation.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

# Load config and env before anything else
from core.config import config  # noqa: E402
from core import log             # noqa: E402
from core.app import Core        # noqa: E402
from core.workers_loader import resolve_worker_configs  # noqa: E402

import redis.asyncio as aioredis  # noqa: E402

_LOGS_DIR = _ROOT / "logs"


async def _connect_redis() -> aioredis.Redis:
    redis_cfg = config.get("redis") or {}
    url = (
        f"redis://:{redis_cfg.get('password')}@{redis_cfg.get('host','127.0.0.1')}"
        f":{redis_cfg.get('port',6379)}"
        if redis_cfg.get("password")
        else f"redis://{redis_cfg.get('host','127.0.0.1')}:{redis_cfg.get('port',6379)}"
    )
    return aioredis.from_url(url, decode_responses=True)


async def _should_run(redis: aioredis.Redis, job: str, interval: int) -> bool:
    """Return True if at least `interval` seconds have passed since last run."""
    if interval <= 0:
        return False
    key = f"{config.get('instance','aidir')}:cron:{job}:last_run"
    last = await redis.get(key)
    if last and (time.time() - float(last)) < interval:
        return False
    await redis.set(key, str(time.time()))
    return True


async def wipe_logs(redis: aioredis.Redis) -> None:
    """Remove log entries older than configured max age."""
    wipe_period = int(config.get("logging.wipe_period") or 0)
    if wipe_period <= 0:
        return

    if not await _should_run(redis, "wipe_logs", wipe_period):
        return

    wipe_max_age = int(config.get("logging.wipe_max_age") or wipe_period)
    from core.logger import logger
    logger.wipe_logs(wipe_max_age)
    log("system", "info", f"Log wipe done (max_age={wipe_max_age}s)")


async def health_check(redis: aioredis.Redis) -> None:
    """Ping Redis and check that core service is reachable."""
    if not await _should_run(redis, "health_check", 300):
        return

    try:
        await redis.ping()
        log("system", "info", "Health check OK: Redis reachable")
    except Exception as exc:
        log("system", "error", f"Health check FAIL: Redis unreachable: {exc}")

    # TODO: add HTTP health checks for core endpoints when port is known


def _trim_log_file(log_file: Path, max_bytes: int) -> int:
    """
    Trim log file from beginning if it exceeds max_bytes.
    Returns number of bytes removed. Preserves complete lines only.
    """
    if max_bytes <= 0:
        return 0
    
    try:
        size = log_file.stat().st_size
        if size <= max_bytes:
            return 0
        
        # Read all lines
        lines = log_file.read_text(encoding="utf-8").splitlines(keepends=True)
        
        # Calculate cumulative size from the end
        current_size = 0
        start_idx = len(lines)
        
        # Keep lines from the end until we exceed max_bytes
        for i in range(len(lines) - 1, -1, -1):
            line_size = len(lines[i].encode("utf-8"))
            if current_size + line_size > max_bytes:
                start_idx = i + 1
                break
        else:
            # All lines fit within max_bytes, but we're being conservative
            start_idx = 1 if lines else 0
        
        # Write trimmed content
        trimmed_lines = lines[start_idx:]
        new_content = "".join(trimmed_lines)
        log_file.write_text(new_content, encoding="utf-8")
        
        bytes_removed = size - len(new_content.encode("utf-8"))
        return bytes_removed
    except Exception:
        return 0


async def trim_logs_by_size(redis: aioredis.Redis) -> None:
    """Trim log files that exceed configured max_log_size."""
    max_log_size = int(config.get("logging.max_log_size") or 0)
    if max_log_size <= 0:
        return
    
    # Check every 3600 seconds (1 hour)
    if not await _should_run(redis, "trim_logs_by_size", 3600):
        return
    
    total_trimmed = 0
    trimmed_files = []
    
    try:
        _LOGS_DIR.mkdir(parents=True, exist_ok=True)
        
        # Process all .log and .jsonl files
        for log_file in _LOGS_DIR.glob("*.log"):
            bytes_removed = _trim_log_file(log_file, max_log_size)
            if bytes_removed > 0:
                total_trimmed += bytes_removed
                trimmed_files.append((log_file.name, bytes_removed))
        
        for log_file in _LOGS_DIR.glob("*.jsonl"):
            bytes_removed = _trim_log_file(log_file, max_log_size)
            if bytes_removed > 0:
                total_trimmed += bytes_removed
                trimmed_files.append((log_file.name, bytes_removed))
        
        if trimmed_files:
            details = ", ".join(f"{name}({b//1024}KB)" for name, b in trimmed_files)
            log("system", "info", f"Log trim done (max_size={max_log_size}, removed={total_trimmed//1024}KB): {details}")
    except Exception as exc:
        log("system", "error", f"trim_logs_by_size failed: {exc}")


async def cleanup_stale_tasks(redis: aioredis.Redis) -> None:
    """
    Detect tasks stuck in 'running' state without a live worker and cancel them.
    TODO: implement when task-worker heartbeat mechanism is added.
    """


async def cleanup_expired_tasks(redis: aioredis.Redis) -> None:
    """Delete completed external tasks older than config.external_task_live seconds."""
    interval = 86400  # run once per day
    if not await _should_run(redis, "cleanup_expired_tasks", interval):
        return

    max_age = int(config.get("external_task_live") or 0)
    if max_age <= 0:
        return

    ns = config.get("instance", "aidir")
    pattern = f"{ns}:task:*"
    cutoff = time.time() - max_age
    deleted = 0

    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match=pattern, count=100)
        for key in keys:
            try:
                fields = await redis.hmget(key, "external", "finished_at", "status")
                is_external, finished_at, status = fields
                if is_external != "1":
                    continue
                if status not in ("completed", "failed", "canceled"):
                    continue
                if not finished_at:
                    continue
                from datetime import datetime, timezone
                ft = datetime.fromisoformat(finished_at).timestamp()
                if ft < cutoff:
                    await redis.delete(key)
                    deleted += 1
            except Exception as exc:
                log("system", "warning", f"cleanup_expired_tasks: skip {key}: {exc}")
        if cursor == 0:
            break

    if deleted:
        log("system", "info", f"cleanup_expired_tasks: deleted {deleted} expired task(s)")


async def run_loop_workers_cycle(redis: aioredis.Redis) -> None:
    """Run loop() for all workers that expose it, rotating start position each cron cycle."""
    core = Core()
    try:
        await core.start()

        loop_workers = core.loop_workers
        if not loop_workers:
            return

        ns = config.get("instance", "aidir")
        key = f"{ns}:cron:loop_workers:start"
        raw = await redis.get(key)

        try:
            start_index = int(raw) if raw is not None else 0
        except Exception:
            start_index = 0

        total = len(loop_workers)
        start_index = start_index % total

        next_index = await core.run_loop_workers_cycle(start_index=start_index)
        await redis.set(key, str(next_index))
    finally:
        await core.stop()


async def keep_alive_ping(redis: aioredis.Redis) -> None:
    """
    Send keep-alive pings to models that should remain loaded in memory.
    Reads model activity records written by Resources.release_for() and pings
    models via provider API if keep_alive_period has elapsed since last use/ping.
    """
    ns = config.get("instance", "aidir")
    pattern = f"{ns}:resource:*:activity:*"
    now = time.time()
    pinged = 0

    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match=pattern, count=100)
        for key in keys:
            try:
                import json
                raw = await redis.get(key)
                if not raw:
                    continue
                data = json.loads(raw)
                keep_alive = int(data.get("keep_alive") or 0)
                keep_alive_period = int(data.get("keep_alive_period") or 0)
                if not keep_alive or not keep_alive_period:
                    continue
                released_at = float(data.get("released_at") or 0)
                age = now - released_at
                # Skip if keep_alive window has passed or ping not yet due
                if age >= keep_alive or age < keep_alive_period:
                    continue
                model_id = data.get("model_id") or ""
                resource_id = data.get("resource_id") or ""
                provider_id = data.get("provider") or ""
                if not model_id or not provider_id:
                    continue
                # Send keep-alive ping via provider API
                providers = ((config.raw().get("models") or {}).get("providers") or {})
                provider = providers.get(provider_id) or {}
                base_url = (provider.get("baseUrl") or "").rstrip("/")
                api_type = provider.get("api") or ""
                if not base_url or api_type != "ollama":
                    continue
                import httpx
                url = f"{base_url}/api/generate"
                async with httpx.AsyncClient(timeout=15.0) as client:
                    await client.post(url, json={"model": model_id, "keep_alive": keep_alive_period * 2})
                # Update last-activity timestamp in Redis
                data["released_at"] = now
                ttl = max(keep_alive, 3600)
                await redis.set(key, json.dumps(data), ex=ttl)
                pinged += 1
                log("system", "info",
                    f"keep_alive ping: {model_id} on {resource_id} (age={age:.0f}s)")
            except Exception as exc:
                log("system", "warn", f"keep_alive_ping: error processing {key}: {exc}")
        if cursor == 0:
            break

    if pinged:
        log("system", "info", f"keep_alive_ping: sent {pinged} ping(s)")


async def refresh_external_mcp_tools(redis: aioredis.Redis) -> None:
    """Refresh external_mcp tools asynchronously when startup/TTL rules require it."""
    if not await _should_run(redis, "refresh_external_mcp_tools", 60):
        return

    workers_cfg = resolve_worker_configs(config.raw())
    external_cfg = workers_cfg.get("external_mcp") or {}
    if not external_cfg.get("enabled", True):
        return

    from workers.tool.external_mcp.app import ExternalMcpWorker

    worker = ExternalMcpWorker()
    await worker.initialize(
        {
            **external_cfg,
            "_redis": redis,
            "_instance": config.get("instance", "aidir"),
            "_mark_startup": False,
        }
    )
    await worker.refresh_tools_if_due(reason="cron")


async def main() -> None:
    redis = await _connect_redis()
    try:
        await run_loop_workers_cycle(redis)
        await refresh_external_mcp_tools(redis)
        await wipe_logs(redis)
        await trim_logs_by_size(redis)
        await health_check(redis)
        await cleanup_stale_tasks(redis)
        await cleanup_expired_tasks(redis)
        await keep_alive_ping(redis)
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
