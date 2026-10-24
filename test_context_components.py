"""Direct coverage for the envid registry, MCP endpoint and context workers."""

from __future__ import annotations

import asyncio
import json
import unittest

from fastapi.testclient import TestClient

from core.context import Context
from core.endpoints.endpoint_mcp import Endpoint_mcp
from core.envid import EnvidRegistry
from core.task_types.task_agent import Task_agent
from workers.context.context_add_internal_tools.app import ContextAddInternalToolsWorker
from workers.context.context_builder.app import ContextBuilderWorker
from workers.context.context_render_openclaw_style.app import ContextRenderOpenClawStyleWorker


class _FakeRedis:
    """Minimal in-memory Redis used by the envid registry."""

    def __init__(self) -> None:
        self.sets: dict[str, set[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.values: dict[str, str] = {}

    async def smembers(self, key):
        """Return the members of one set."""
        return set(self.sets.get(key, set()))

    async def hgetall(self, key):
        """Return one hash."""
        return dict(self.hashes.get(key, {}))

    async def hset(self, key, field=None, value=None, mapping=None):
        """Write one hash field or mapping."""
        target = self.hashes.setdefault(key, {})
        if mapping is not None:
            target.update(mapping)
        elif field is not None:
            target[field] = value

    async def get(self, key):
        """Return one string."""
        return self.values.get(key)

    async def set(self, key, value):
        """Store one string."""
        self.values[key] = value

    def pipeline(self, transaction=False):
        """Return a pipeline executing against this store."""
        return _FakePipeline(self)


class _FakePipeline:
    """Tiny pipeline double."""

    def __init__(self, redis: _FakeRedis) -> None:
        self.redis = redis
        self.commands = []

    def sadd(self, key, value):
        """Queue a set add."""
        self.commands.append(("sadd", key, value))
        return self

    def hset(self, key, mapping):
        """Queue a hash write."""
        self.commands.append(("hset", key, mapping))
        return self

    async def execute(self):
        """Apply the queued commands."""
        for command in self.commands:
            if command[0] == "sadd":
                self.redis.sets.setdefault(command[1], set()).add(command[2])
            else:
                self.redis.hashes.setdefault(command[1], {}).update(command[2])
        return []


class EnvidRegistryTests(unittest.IsolatedAsyncioTestCase):
    """Validate envid persistence and context storage."""

    async def test_round_trip_config_and_context(self) -> None:
        """Save a config, reload it, and persist a context with saved_at."""
        redis = _FakeRedis()
        registry = EnvidRegistry(redis, "aidir")
        await registry.merge_from_config({"envids": {"items": {"demo": {"description": "Demo"}}}})
        reloaded = EnvidRegistry(redis, "aidir")
        await reloaded.load_from_redis()
        self.assertIsNotNone(reloaded.get("demo"))
        await reloaded.save_context("demo", {"history": [{"role": "user"}]})
        context = await reloaded.load_context("demo")
        self.assertIn("saved_at", context)


class _Config:
    """Nested configuration lookup for the MCP endpoint."""

    def __init__(self, data: dict) -> None:
        self.data = data

    def get(self, key: str, default=None):
        """Return one nested value."""
        node = self.data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


class _Core:
    """Minimal core used by the MCP endpoint."""

    def __init__(self) -> None:
        self.config = _Config({"default_auth_mode": "disabled", "http": {"max_request_size": 1024}})
        self.workers = {}
        self.queue = None
        self.audit_log = None


class McpEndpointTests(unittest.TestCase):
    """Validate MCP health and initialization."""

    def test_health_and_initialize(self) -> None:
        """Answer health and the MCP initialize handshake."""
        endpoint = Endpoint_mcp({"id": "mcp", "api": "mcp"})
        with TestClient(endpoint.create_app(_Core())) as client:
            self.assertEqual(client.get("/health").json(), {"status": "ok"})
            response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["protocolVersion"], "2024-11-05")


class ContextWorkerTests(unittest.IsolatedAsyncioTestCase):
    """Validate the three context workers directly."""

    async def test_builder_merges_saved_context(self) -> None:
        """Load the saved envid context and keep task overrides."""
        class Registry:
            async def load_context(self, _):
                return {"system": "saved"}

            async def mark_used(self, _):
                return None

        class Core:
            envid_registry = Registry()

        worker = ContextBuilderWorker()
        await worker.initialize({"_core": Core()})
        task = Task_agent(payload={"envid": "demo"}, stream=False)
        task.context = Context.empty()
        task.context.system_rendered = "override"
        result = await worker.execute(task)
        self.assertTrue(result.ok)
        self.assertEqual(task.context.envid, "demo")

    async def test_internal_tools_and_renderer(self) -> None:
        """Add a configured tool and render it into the system prompt."""
        tools_worker = ContextAddInternalToolsWorker()
        await tools_worker.initialize({"tools": {"search": {"worker": "web_search", "description": "Search"}}})
        task = Task_agent(payload={}, stream=False)
        task.context = Context.empty()
        self.assertTrue((await tools_worker.execute(task)).ok)
        renderer = ContextRenderOpenClawStyleWorker()
        self.assertTrue((await renderer.execute(task)).ok)
        self.assertIn("search", task.context.system_rendered)
