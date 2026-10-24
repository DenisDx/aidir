"""Regression tests for scheduler OpenAI-family probes and cron maintenance."""

from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from core import cron
from core.scheduler import Scheduler


class _Response:
    """Minimal HTTP response double."""

    def __init__(self, status_code: int, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        """Return the configured payload."""
        return self._payload


class _Client:
    """Async HTTP client double returning a fixed response."""

    response = _Response(200, {"data": [{"id": "model-a"}]})
    requests: list[str] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    async def __aenter__(self):
        """Enter the client context."""
        return self

    async def __aexit__(self, *args) -> None:
        """Exit the client context."""

    async def get(self, url: str):
        """Return the fixed response and record the URL."""
        self.requests.append(url)
        return self.response


class SchedulerOpenAIProbeTests(unittest.TestCase):
    """Validate that the scheduler probes OpenAI-family providers."""

    def test_probe_lists_openai_model(self) -> None:
        """Return true when the provider model list contains the model."""
        scheduler = Scheduler.__new__(Scheduler)
        scheduler._full_config = {
            "models": {"providers": {"remote": {
                "api": "openai",
                "baseUrl": "http://example.test/v1",
                "auth": {"token": "secret"},
            }}}
        }
        _Client.requests = []
        _Client.response = _Response(200, {"data": [{"id": "model-a"}]})
        with patch("core.scheduler.httpx.AsyncClient", _Client):
            result = asyncio.run(scheduler._probe_openai_model_availability(
                "remote", "model-a", timeout_ms=100, incoming_bearer_token="token",
            ))
        self.assertTrue(result)
        self.assertEqual(_Client.requests[-1], "http://example.test/v1/models")
        self.assertEqual(_Client.response.status_code, 200)


class _Redis:
    """Redis double for envid-context pruning."""

    def __init__(self, data: dict[str, str]) -> None:
        self.data = dict(data)
        self.deleted: list[str] = []

    async def scan(self, cursor, match, count):
        """Return all keys once."""
        return 0, list(self.data)

    async def get(self, key: str):
        """Read one key."""
        return self.data.get(key)

    async def delete(self, key: str):
        """Delete one key."""
        self.deleted.append(key)
        return 1 if self.data.pop(key, None) is not None else 0


class EnvidPruneTests(unittest.TestCase):
    """Validate envid context retention."""

    def setUp(self) -> None:
        """Preserve module globals."""
        self.orig_config = cron.config
        self.orig_should_run = cron._should_run

    def tearDown(self) -> None:
        """Restore module globals."""
        cron.config = self.orig_config
        cron._should_run = self.orig_should_run

    def test_prunes_only_expired_contexts(self) -> None:
        """Delete contexts whose saved_at is older than the retention."""
        old = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        fresh = datetime.now(timezone.utc).isoformat()
        redis = _Redis({
            "aidir:ctx:old": json.dumps({"saved_at": old}),
            "aidir:ctx:fresh": json.dumps({"saved_at": fresh}),
        })
        cron.config = type("Cfg", (), {"get": staticmethod(lambda key, default=None: {
            "instance": "aidir",
            "envids.check_period_minutes": 1,
            "envids.store_duration_minutes": 10,
        }.get(key, default))})()

        async def _run():
            cron._should_run = lambda *args: _true()
            await cron.prune_envid_contexts(redis)

        async def _true():
            return True

        asyncio.run(_run())
        self.assertEqual(redis.deleted, ["aidir:ctx:old"])


class HealthCheckTests(unittest.TestCase):
    """Validate HTTP health probes for OpenAIx and MCP."""

    def test_probes_configured_endpoints(self) -> None:
        """Probe both configured routes and ignore other endpoint types."""
        orig_config = cron.config
        orig_should_run = cron._should_run
        cron.config = type("Cfg", (), {"get": staticmethod(lambda key, default=None: {
            "endpoints": [
                {"id": "openaix", "api": "openaix", "port": 21434, "bindAddress": "0.0.0.0"},
                {"id": "mcp", "api": "mcp", "port": 20001, "bindAddress": "127.0.0.1"},
                {"id": "anthropic", "api": "anthropic", "port": 21435},
            ],
        }.get(key, default))})()
        _Client.requests = []
        _Client.response = _Response(200, {})

        async def _run():
            cron._should_run = lambda *args: _true()
            with patch("core.cron.httpx.AsyncClient", _Client):
                await cron.health_check(type("R", (), {"ping": lambda self: _ping()})())

        async def _true():
            return True

        async def _ping():
            return True

        try:
            asyncio.run(_run())
        finally:
            cron.config = orig_config
            cron._should_run = orig_should_run
        self.assertEqual(_Client.requests, [
            "http://127.0.0.1:21434/health",
            "http://127.0.0.1:20001/health",
        ])


class RemoteProbeCacheTests(unittest.TestCase):
    """Validate remote smart-probe caching and in-flight deduplication."""

    def test_concurrent_probes_share_one_remote_call(self) -> None:
        """Two concurrent probes use one upstream call and a short cache hit."""
        from core.smart_router import SmartRouter

        calls = 0
        router = SmartRouter.__new__(SmartRouter)
        router._remote_probe_cache = {}
        router._remote_probe_inflight = {}

        async def _probe(*args, **kwargs):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return {"can_run_now": True}

        router._probe_remote_model_queue_state = _probe

        async def _run():
            return await asyncio.gather(
                router._probe_remote_cached("peer", "model", priority=5, timeout_ms=100),
                router._probe_remote_cached("peer", "model", priority=5, timeout_ms=100),
            )

        results = asyncio.run(_run())
        self.assertEqual(calls, 1)
        self.assertEqual(results[0], {"can_run_now": True})
        self.assertEqual(results[1], {"can_run_now": True})
        asyncio.run(router._probe_remote_cached("peer", "model", priority=5, timeout_ms=100))
        self.assertEqual(calls, 1)


class WorkerSelectionTests(unittest.TestCase):
    """Validate that a resolved provider route wins over an incompatible pin."""

    def test_route_worker_wins_over_wrong_pin(self) -> None:
        """Select the resolved worker instead of an incompatible pinned worker."""
        scheduler = Scheduler.__new__(Scheduler)
        scheduler._workers = {
            "wrong": type("W", (), {"enabled": True, "task_type": "agent"})(),
            "right": type("W", (), {"enabled": True, "task_type": "agent"})(),
        }
        task = type("T", (), {
            "type": "agent",
            "worker_id": "wrong",
            "config": {"route": {"resolved_worker": "right"}},
        })()
        self.assertIs(scheduler._select_worker(task), scheduler._workers["right"])


class StaleTaskCleanupTests(unittest.TestCase):
    """Validate stale running-task failure."""

    def test_fails_task_past_run_timeout(self) -> None:
        """Mark a running task failed when its worker never finished it."""
        class R:
            def __init__(self):
                self.hash = {
                    "id": "old",
                    "status": "running",
                    "started_at": (datetime.now(timezone.utc) - timedelta(seconds=100)).isoformat(),
                    "run_timeout": "10",
                }
                self.failed = {}

            async def scan(self, cursor, match, count):
                return 0, ["aidir:task:old"]

            async def hmget(self, key, *fields):
                return [self.hash.get(field) for field in fields]

            async def hset(self, key, mapping):
                self.failed.update(mapping)

        orig_config, orig_should_run = cron.config, cron._should_run
        cron.config = type("Cfg", (), {"get": staticmethod(lambda key, default=None: {
            "instance": "aidir", "cron_period": 60,
        }.get(key, default))})()

        async def _true():
            return True

        async def _run(redis):
            cron._should_run = lambda *args: _true()
            await cron.cleanup_stale_tasks(redis)

        redis = R()
        try:
            asyncio.run(_run(redis))
        finally:
            cron.config, cron._should_run = orig_config, orig_should_run
        self.assertEqual(redis.failed.get("error_code"), "STALE_TASK")
        self.assertEqual(redis.failed.get("status"), "failed")


class KvAffinityMirrorTests(unittest.IsolatedAsyncioTestCase):
    """Validate Redis mirror lookup for affinity records."""

    async def test_lookup_remote_returns_expiring_record(self) -> None:
        """Load a mirrored record and return its matched anchor."""
        from core.kv_affinity import KvAffinityStore

        store = KvAffinityStore()
        number = 16
        fingerprint = store.fingerprint({"messages": [{"role": "user", "content": "x" * number}]})
        self.assertIsNotNone(fingerprint)
        record = {
            "hash": fingerprint.hash,
            "anchor_bytes": fingerprint.anchor_bytes,
            "provider": "peer",
            "model": "model",
            "expires_at": __import__("time").time() + 60,
        }

        class Redis:
            async def mget(self, keys):
                return [json.dumps(record) if key.endswith(fingerprint.hash) else None for key in keys]

        store.attach_redis(Redis(), "aidir")
        found = await store.lookup_remote(fingerprint)
        self.assertEqual(found["provider"], "peer")
        self.assertEqual(found["matched_anchor_bytes"], fingerprint.anchor_bytes)


class LocalProbeSharingTests(unittest.IsolatedAsyncioTestCase):
    """Validate that concurrent tasks share one local candidate probe."""

    async def test_concurrent_tasks_share_one_candidate_probe(self) -> None:
        """Run evaluate_candidate once for two waiters of the same candidate."""
        from core.smart_router import SmartRouter

        router = object.__new__(SmartRouter)
        router._candidate_inflight = {}
        calls: list[int] = []

        async def _evaluate(item, *, request_priority, index, incoming_bearer_token=""):
            calls.append(index)
            await asyncio.sleep(0.01)
            return {"provider": item["provider"], "model": item["model"], "index": index}

        router.evaluate_candidate = _evaluate
        item = {"provider": "p1", "model": "m1"}
        first, second = await asyncio.gather(
            router._evaluate_candidate_shared(item, request_priority=1, index=0),
            router._evaluate_candidate_shared(item, request_priority=1, index=1),
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual((first["index"], second["index"]), (0, 1))
