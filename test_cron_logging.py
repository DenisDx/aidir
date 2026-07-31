"""Regression tests for cron logging maintenance behavior."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from core import cron


class _FakeRedis:
    """Minimal async Redis stub for cron tests."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.closed = False

    async def get(self, key: str):
        return self.data.get(key)

    async def set(self, key: str, value: str):
        self.data[key] = value

    async def aclose(self):
        self.closed = True


class _DummyConfig:
    """Config stub implementing get/raw used by cron module."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def get(self, key: str, default=None):
        node = self._data
        for part in key.split("."):
            if not isinstance(node, dict):
                return default
            node = node.get(part)
            if node is None:
                return default
        return node

    def raw(self) -> dict:
        return self._data


class TestCronLogging(unittest.TestCase):
    """Validate cron trim/wipe behavior for all logs and per-log overrides."""

    def setUp(self) -> None:
        self.orig_logs_dir = cron._LOGS_DIR
        self.orig_config = cron.config
        self.orig_should_run = cron._should_run

    def tearDown(self) -> None:
        cron._LOGS_DIR = self.orig_logs_dir
        cron.config = self.orig_config
        cron._should_run = self.orig_should_run

    def test_trim_applies_global_size_to_log_and_jsonl(self) -> None:
        """Trims both .log and .jsonl files using global max_log_size."""
        with tempfile.TemporaryDirectory() as tmp:
            logs_dir = Path(tmp)
            cron._LOGS_DIR = logs_dir
            cron.config = _DummyConfig({
                "instance": "aidir",
                "logging": {
                    "wipe_period": 1,
                    "max_log_size": 200,
                },
            })

            async def _always(*_args, **_kwargs):
                return True

            cron._should_run = _always

            # Build oversized files with complete lines.
            (logs_dir / "worker.log").write_text(("line\n" * 200), encoding="utf-8")
            (logs_dir / "openaix_call_log.jsonl").write_text(("{\"x\":1}\n" * 200), encoding="utf-8")

            redis = _FakeRedis()
            asyncio.run(cron.trim_logs_by_size(redis))

            self.assertLessEqual((logs_dir / "worker.log").stat().st_size, 200)
            self.assertLessEqual((logs_dir / "openaix_call_log.jsonl").stat().st_size, 200)

    def test_trim_uses_per_log_override_when_defined(self) -> None:
        """Uses override max_log_size for matching log file while keeping global defaults."""
        with tempfile.TemporaryDirectory() as tmp:
            logs_dir = Path(tmp)
            cron._LOGS_DIR = logs_dir
            cron.config = _DummyConfig({
                "instance": "aidir",
                "logging": {
                    "wipe_period": 1,
                    "max_log_size": 300,
                    "logs": {
                        "worker.log": {
                            "max_log_size": 120,
                        }
                    },
                },
            })

            async def _always(*_args, **_kwargs):
                return True

            cron._should_run = _always

            (logs_dir / "worker.log").write_text(("line\n" * 200), encoding="utf-8")
            (logs_dir / "all.log").write_text(("line\n" * 200), encoding="utf-8")

            redis = _FakeRedis()
            asyncio.run(cron.trim_logs_by_size(redis))

            # Override is stricter than global value.
            self.assertLessEqual((logs_dir / "worker.log").stat().st_size, 120)
            self.assertLessEqual((logs_dir / "all.log").stat().st_size, 300)

    def test_main_runs_remaining_jobs_when_one_fails(self) -> None:
        """Keeps running remaining cron jobs even if one of them raises an exception."""

        class _CloseOnlyRedis:
            def __init__(self):
                self.closed = False

            async def aclose(self):
                self.closed = True

        redis = _CloseOnlyRedis()
        calls: list[str] = []

        async def _connect():
            return redis

        async def _fail(_redis):
            calls.append("loop")
            raise RuntimeError("boom")

        async def _ok(name):
            calls.append(name)

        # Keep originals and restore manually in finally for this test scope.
        orig_connect = cron._connect_redis
        orig_run_loop = cron.run_loop_workers_cycle
        orig_refresh = cron.refresh_external_mcp_tools
        orig_wipe = cron.wipe_logs
        orig_trim = cron.trim_logs_by_size
        orig_health = cron.health_check
        orig_cleanup_stale = cron.cleanup_stale_tasks
        orig_cleanup_expired = cron.cleanup_expired_tasks
        orig_keep_alive = cron.keep_alive_ping
        orig_log = cron.log

        try:
            cron._connect_redis = _connect
            cron.run_loop_workers_cycle = _fail
            cron.refresh_external_mcp_tools = lambda r: _ok("refresh")
            cron.wipe_logs = lambda r: _ok("wipe")
            cron.trim_logs_by_size = lambda r: _ok("trim")
            cron.health_check = lambda r: _ok("health")
            cron.cleanup_stale_tasks = lambda r: _ok("cleanup_stale")
            cron.cleanup_expired_tasks = lambda r: _ok("cleanup_expired")
            cron.keep_alive_ping = lambda r: _ok("keep_alive")
            cron.log = lambda *args, **kwargs: None

            asyncio.run(cron.main())
        finally:
            cron._connect_redis = orig_connect
            cron.run_loop_workers_cycle = orig_run_loop
            cron.refresh_external_mcp_tools = orig_refresh
            cron.wipe_logs = orig_wipe
            cron.trim_logs_by_size = orig_trim
            cron.health_check = orig_health
            cron.cleanup_stale_tasks = orig_cleanup_stale
            cron.cleanup_expired_tasks = orig_cleanup_expired
            cron.keep_alive_ping = orig_keep_alive
            cron.log = orig_log

        self.assertEqual(calls, [
            "loop",
            "refresh",
            "wipe",
            "trim",
            "health",
            "cleanup_stale",
            "cleanup_expired",
            "keep_alive",
        ])
        self.assertTrue(redis.closed)


if __name__ == "__main__":
    unittest.main(verbosity=2)