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


async def main() -> None:
    redis = await _connect_redis()
    try:
        await wipe_logs(redis)
        await health_check(redis)
        await cleanup_stale_tasks(redis)
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
