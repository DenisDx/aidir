"""Regression tests for the external MCP tools refresh cadence."""
from __future__ import annotations

import time
import unittest
from unittest.mock import AsyncMock

from workers.tool.external_mcp.app import ExternalMcpWorker


class _Redis:
    """Minimal Redis stub with the shared refresh timestamps."""

    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    async def get(self, key: str):
        """Return one stored timestamp."""
        return self.values.get(key)

    async def set(self, key: str, value: str, ex=None) -> None:
        """Store one timestamp."""
        self.values[key] = value


class ExternalMcpRefreshTests(unittest.IsolatedAsyncioTestCase):
    """The cron process must not force a tools refresh every minute."""

    async def test_cron_startup_respects_the_shared_refresh_ttl(self) -> None:
        """A recent shared refresh means no new server reads and no new logs."""
        now = time.time()
        redis = _Redis({
            "aidir:worker:external_mcp:last_tools_refresh": str(now),
        })
        worker = ExternalMcpWorker()
        worker._refresh_tools = AsyncMock()

        await worker.initialize({
            "_redis": redis,
            "_instance": "aidir",
            "_mark_startup": False,
            "tools_ttl": 300,
            "servers": {},
        })

        worker._refresh_tools.assert_not_awaited()
        self.assertNotIn("aidir:worker:external_mcp:startup_time", redis.values)

    async def test_main_service_startup_forces_the_initial_refresh(self) -> None:
        """The live service still builds the tool list when it starts."""
        now = time.time()
        redis = _Redis({
            "aidir:worker:external_mcp:last_tools_refresh": str(now),
        })
        worker = ExternalMcpWorker()
        worker._refresh_tools = AsyncMock()

        await worker.initialize({
            "_redis": redis,
            "_instance": "aidir",
            "_mark_startup": True,
            "tools_ttl": 300,
            "servers": {},
        })

        worker._refresh_tools.assert_awaited_once()
        self.assertIn("aidir:worker:external_mcp:startup_time", redis.values)


if __name__ == "__main__":
    unittest.main(verbosity=2)
