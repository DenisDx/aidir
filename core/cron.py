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

import redis.asyncio as aioredis  # noqa: E402


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


async def main() -> None:
    redis = await _connect_redis()
    try:
        await run_loop_workers_cycle(redis)
        await wipe_logs(redis)
        await health_check(redis)
        await cleanup_stale_tasks(redis)
        await cleanup_expired_tasks(redis)
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
