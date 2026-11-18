"""Copy one Redis-compatible datastore into another, splitting task bodies.

The source is read with SCAN, so a large task history does not need to fit in
the migration process. Terminal task hashes are written through TaskStore,
which moves their bodies into one compressed string key; other keys are copied
by type with their TTL preserved.

Typical cutover to Dragonfly with SSD tiering::

    python -m core.migrate_datastore \
        --source-port 6379 --dest-port 6380

The destination should have a memory cap and tiering enabled: compressed task
bodies are large string values, so they can be offloaded while metadata stays
in RAM.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

import redis.asyncio as aioredis

from core.task_store import TaskStore

_TERMINAL = ("completed", "failed", "canceled")


def _client(host: str, port: int, password: str, db: int) -> aioredis.Redis:
    """Build one async client with text decoding."""
    return aioredis.Redis(
        host=host,
        port=port,
        password=password or None,
        db=db,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=60,
    )


async def _copy_key(source, dest, key: str) -> str:
    """Copy one key and return the Redis type that was handled.

    Task hashes in a terminal state are written in the split layout, because a
    plain hash would keep the whole body in RAM on the destination.
    """
    key_type = await source.type(key)
    if key_type == "string":
        if ":taskbody:" in key:
            # Bodies are rebuilt from the task hash below; copying the raw blob
            # could disagree with the marker written for the destination.
            return "taskbody"
        value = await source.get(key)
        if value is not None:
            await dest.set(key, value)
    elif key_type == "hash":
        raw = await source.hgetall(key)
        if ":task:" in key:
            namespace, _, task_id = key.rpartition(":task:")
            source_store = TaskStore(source, namespace)
            # Load merges a split body with its metadata, so an already
            # converted task is not written back without its payload.
            full = await source_store.load(task_id)
            if full is None:
                return "skip"
            dest_store = TaskStore(dest, namespace)
            if str(full.get("status") or "") in _TERMINAL:
                await dest_store.save_terminal(task_id, full)
            else:
                await dest.hset(key, mapping=full)
                await dest.delete(dest_store.body_key(task_id))
                await dest.hdel(key, "body_format")
        elif raw:
            await dest.hset(key, mapping=raw)
    elif key_type == "set":
        members = await source.smembers(key)
        if members:
            await dest.sadd(key, *members)
    elif key_type == "zset":
        rows = await source.zrange(key, 0, -1, withscores=True)
        if rows:
            await dest.zadd(key, {member: score for member, score in rows})
    elif key_type == "list":
        rows = await source.lrange(key, 0, -1)
        if rows:
            await dest.rpush(key, *rows)
    else:
        return key_type

    pttl = await source.pttl(key)
    if pttl and pttl > 0:
        await dest.pexpire(key, pttl)
    return key_type


async def migrate(
    source_host: str,
    source_port: int,
    source_password: str,
    source_db: int,
    dest_host: str,
    dest_port: int,
    dest_password: str,
    dest_db: int,
    *,
    pattern: str = "*",
    limit: int = 0,
    dry_run: bool = False,
) -> dict[str, int]:
    """Copy keys from source to destination and return counters."""
    source = _client(source_host, source_port, source_password, source_db)
    dest = _client(dest_host, dest_port, dest_password, dest_db)
    counts: dict[str, int] = {}
    copied = 0
    failed = 0
    started = time.monotonic()
    try:
        await source.ping()
        await dest.ping()
        cursor = 0
        while True:
            cursor, keys = await source.scan(cursor, match=pattern, count=200)
            for key in keys:
                try:
                    if dry_run:
                        key_type = await source.type(key)
                    else:
                        key_type = await _copy_key(source, dest, key)
                    counts[key_type] = counts.get(key_type, 0) + 1
                    copied += 1
                except Exception as exc:
                    failed += 1
                    print(f"skip {key}: {exc.__class__.__name__}: {exc}", file=sys.stderr)
                if copied and copied % 1000 == 0:
                    elapsed = max(0.001, time.monotonic() - started)
                    print(f"copied {copied} keys ({copied / elapsed:.0f}/s)")
                if limit and copied >= limit:
                    break
            if (limit and copied >= limit) or cursor == 0:
                break
    finally:
        await source.aclose()
        await dest.aclose()
    counts["failed"] = failed
    counts["total"] = copied
    return counts


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Copy Redis-compatible data between two servers")
    parser.add_argument("--source-host", default="127.0.0.1")
    parser.add_argument("--source-port", type=int, default=6379)
    parser.add_argument("--source-password", default="")
    parser.add_argument("--source-db", type=int, default=0)
    parser.add_argument("--dest-host", default="127.0.0.1")
    parser.add_argument("--dest-port", type=int, default=6380)
    parser.add_argument("--dest-password", default="")
    parser.add_argument("--dest-db", type=int, default=0)
    parser.add_argument("--pattern", default="*", help="SCAN pattern, e.g. 'aidir111:*'")
    parser.add_argument("--limit", type=int, default=0, help="Stop after N keys (0 = all)")
    parser.add_argument("--dry-run", action="store_true", help="Only count source keys by type")
    return parser.parse_args()


async def main() -> None:
    """Run the migration with command-line arguments."""
    args = _args()
    counts = await migrate(
        args.source_host,
        args.source_port,
        args.source_password,
        args.source_db,
        args.dest_host,
        args.dest_port,
        args.dest_password,
        args.dest_db,
        pattern=args.pattern,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    print("migration counters:", counts)


if __name__ == "__main__":
    asyncio.run(main())
