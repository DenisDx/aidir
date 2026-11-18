"""Tests for the split task metadata/body layout."""
from __future__ import annotations

import unittest

from core.task_store import (
    BODY_FIELDS,
    BODY_FORMAT_NONE,
    BODY_FORMAT_ZLIB,
    TaskStore,
    decode_body,
    encode_body,
    offload_terminal_task_bodies,
    split_task_fields,
)


class FakePipeline:
    """Minimal async pipeline used by TaskStore."""

    def __init__(self, redis: "FakeRedis") -> None:
        self._redis = redis
        self._ops: list[tuple] = []

    def hset(self, key, mapping=None, field=None, value=None):
        self._ops.append(("hset", key, mapping, field, value))
        return self

    def hdel(self, key, *fields):
        self._ops.append(("hdel", key, fields))
        return self

    def delete(self, *keys):
        self._ops.append(("delete", keys))
        return self

    def zadd(self, key, mapping):
        self._ops.append(("zadd", key, mapping))
        return self

    def zrem(self, key, *members):
        self._ops.append(("zrem", key, members))
        return self

    def set(self, key, value):
        self._ops.append(("set", key, value))
        return self

    def get(self, key):
        self._ops.append(("get", key))
        return self

    def hget(self, key, field):
        self._ops.append(("hget", key, field))
        return self

    def hmget(self, key, *fields):
        self._ops.append(("hmget", key, fields))
        return self

    async def execute(self):
        result = []
        for op in self._ops:
            name = op[0]
            if name == "hset":
                result.append(await self._redis.hset(op[1], mapping=op[2], field=op[3], value=op[4]))
            elif name == "hdel":
                result.append(await self._redis.hdel(op[1], *op[2]))
            elif name == "delete":
                result.append(await self._redis.delete(*op[1]))
            elif name == "set":
                result.append(await self._redis.set(op[1], op[2]))
            elif name == "zadd":
                result.append(await self._redis.zadd(op[1], op[2]))
            elif name == "zrem":
                result.append(await self._redis.zrem(op[1], *op[2]))
            elif name == "get":
                result.append(await self._redis.get(op[1]))
            elif name == "hget":
                result.append(await self._redis.hget(op[1], op[2]))
            elif name == "hmget":
                result.append(await self._redis.hmget(op[1], *op[2]))
        self._ops = []
        return result


class FakeRedis:
    """In-memory stand-in for the small Redis surface TaskStore uses."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.strings: dict[str, str] = {}
        self.zsets: dict[str, dict[str, float]] = {}

    async def hset(self, key, mapping=None, field=None, value=None):
        target = self.hashes.setdefault(key, {})
        if mapping:
            target.update({str(k): str(v) for k, v in mapping.items()})
        if field is not None:
            target[str(field)] = str(value)
        return len(mapping or {})

    async def hdel(self, key, *fields):
        target = self.hashes.get(key, {})
        removed = 0
        for name in fields:
            if name in target:
                del target[name]
                removed += 1
        return removed

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def hmget(self, key, *fields):
        target = self.hashes.get(key, {})
        return [target.get(name) for name in fields]

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def get(self, key):
        return self.strings.get(key)

    async def set(self, key, value):
        self.strings[key] = value
        return True

    async def zadd(self, key, mapping):
        target = self.zsets.setdefault(key, {})
        target.update({str(member): float(score) for member, score in mapping.items()})
        return len(mapping)

    async def zrem(self, key, *members):
        target = self.zsets.get(key, {})
        removed = 0
        for member in members:
            if member in target:
                del target[member]
                removed += 1
        return removed

    async def delete(self, *keys):
        removed = 0
        for key in keys:
            if key in self.strings:
                del self.strings[key]
                removed += 1
            if key in self.hashes:
                del self.hashes[key]
                removed += 1
            if key in self.zsets:
                del self.zsets[key]
                removed += 1
        return removed

    def pipeline(self, transaction=False):
        return FakePipeline(self)

    async def scan(self, cursor, match="*", count=10):
        import fnmatch

        keys = sorted(key for key in self.hashes if fnmatch.fnmatch(key, match))
        keys += sorted(key for key in self.strings if fnmatch.fnmatch(key, match))
        start = int(cursor)
        batch = keys[start:start + int(count)]
        next_cursor = start + len(batch)
        if next_cursor >= len(keys):
            next_cursor = 0
        return next_cursor, batch


def _full_task(task_id: str, *, payload: str = '{"model":"m","messages":[]}') -> dict[str, str]:
    return {
        "id": task_id,
        "type": "agent",
        "status": "completed",
        "priority": "5",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:01+00:00",
        "finished_at": "2026-01-01T00:00:01+00:00",
        "payload": payload,
        "result": '{"answer":"ok"}',
        "upstream_response": '{"raw":"ok"}',
        "llm_call_history": '[{"call_index":1}]',
        "request_preview": "type=agent model=m messages=0",
    }


class CodecTests(unittest.TestCase):
    def test_zlib_roundtrip(self):
        marker, text = encode_body({"payload": '{"messages":["' + "x" * 1000 + '"]}'})
        self.assertEqual(marker, BODY_FORMAT_ZLIB)
        self.assertLess(len(text), 1000)
        self.assertIn("payload", decode_body(marker, text))

    def test_empty_body_marker(self):
        marker, text = encode_body({})
        self.assertEqual(marker, BODY_FORMAT_NONE)
        self.assertEqual(text, "")

    def test_split_excludes_bodies(self):
        metadata, body = split_task_fields(_full_task("t1"))
        for name in BODY_FIELDS:
            self.assertNotIn(name, metadata)
            self.assertIn(name, body)
        self.assertEqual(metadata["id"], "t1")

    def test_damaged_body_is_empty(self):
        self.assertEqual(decode_body(BODY_FORMAT_ZLIB, "not-base64"), {})


class TaskStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = FakeRedis()
        self.store = TaskStore(self.redis, "aidir")

    async def test_save_terminal_moves_bodies(self):
        await self.store.save_terminal("t1", _full_task("t1"))
        meta = self.redis.hashes["aidir:task:t1"]
        for name in BODY_FIELDS:
            self.assertNotIn(name, meta)
        self.assertIn(meta["body_format"], (BODY_FORMAT_ZLIB, "j1"))
        self.assertIn("aidir:taskbody:t1", self.redis.strings)

        loaded = await self.store.load("t1")
        self.assertEqual(loaded["payload"], '{"model":"m","messages":[]}')
        self.assertEqual(loaded["result"], '{"answer":"ok"}')
        self.assertEqual(loaded["llm_call_history"], '[{"call_index":1}]')

    async def test_legacy_inline_hash_is_read(self):
        self.redis.hashes["aidir:task:t1"] = _full_task("t1")
        loaded = await self.store.load("t1")
        self.assertEqual(loaded["payload"], '{"model":"m","messages":[]}')

    async def test_load_payloads_handles_split_and_legacy(self):
        await self.store.save_terminal("split", _full_task("split"))
        self.redis.hashes["aidir:task:legacy"] = _full_task("legacy", payload='{"model":"legacy"}')
        payloads = await self.store.load_payloads(["split", "legacy", "missing"])
        self.assertIn("messages", payloads["split"])
        self.assertEqual(payloads["legacy"], '{"model":"legacy"}')
        self.assertEqual(payloads["missing"], "")

    async def test_delete_removes_both_keys(self):
        await self.store.save_terminal("t1", _full_task("t1"))
        await self.store.delete("t1")
        self.assertNotIn("aidir:task:t1", self.redis.hashes)
        self.assertNotIn("aidir:taskbody:t1", self.redis.strings)

    async def test_offload_converts_only_terminal_tasks(self):
        self.redis.hashes["aidir:task:done"] = _full_task("done")
        self.redis.hashes["aidir:task:running"] = {**_full_task("running"), "status": "running"}
        self.redis.hashes["aidir:task:already"] = {**_full_task("already"), "body_format": "0"}

        result = await offload_terminal_task_bodies(self.redis, "aidir", limit=10)
        self.assertEqual(result["offloaded"], 1)
        self.assertNotIn("payload", self.redis.hashes["aidir:task:done"])
        self.assertEqual(self.redis.hashes["aidir:task:running"]["payload"], '{"model":"m","messages":[]}')
        self.assertNotIn("aidir:taskbody:already", self.redis.strings)


if __name__ == "__main__":
    unittest.main()
