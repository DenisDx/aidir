"""Regression tests for the endpoint authentication and the configuration reload.

Two rounds of hardening:

  1. The surfaces that were open by accident - the MCP tool endpoint, the
     queue-state routes, the peer task list and the peer resource inventory -
     can require a token, and the peer surfaces do so by default.
  2. A reload re-applies what may change at runtime: the workers are
     re-initialized and the resource registry is updated in place, so the
     reservations of the running tasks survive. The structural sections are
     reported instead of being pretended away.
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from core.endpoints.endpoint_mcp import Endpoint_mcp
from core.endpoints.endpoint_openaix import Endpoint_openaix
from core.resources import Resources
from core.scheduler import Scheduler


class _Config:
    """Provide dotted configuration lookup for the endpoint tests."""

    def __init__(self, values: dict) -> None:
        """Store the configuration values."""
        self._values = values

    def get(self, key: str, default=None):
        """Return a nested value or the supplied default."""
        value = self._values
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value

    def raw(self) -> dict:
        """Return the whole configuration."""
        return self._values

    def load(self) -> None:
        """Reload the configuration; the double already holds the new values."""


class _Core:
    """Core double with the configuration, the queue and the resources."""

    def __init__(self, config: dict, tasks: list | None = None) -> None:
        """Store the parts the endpoints read."""
        self.config = _Config(config)
        self.queue = SimpleNamespace(list_tasks=lambda: list(tasks or []))
        self.resources = Resources([{"id": "gpu", "type": "cuda", "limits": {"VRAM": 20}}])
        self.scheduler = None
        self.workers = {}


class McpAuthenticationTests(unittest.TestCase):
    """The MCP tool endpoint must be able to require a token."""

    def _client(self, endpoint_config: dict, config: dict) -> TestClient:
        """Build a client for one configured MCP endpoint."""
        endpoint = Endpoint_mcp({"id": "mcp", "api": "mcp", **endpoint_config})
        return TestClient(endpoint.create_app(_Core(config)))

    def test_the_tools_are_public_without_the_requirement(self):
        """The default stays as it was: an existing deployment keeps working."""
        with self._client({}, {}) as client:
            response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})

        self.assertEqual(response.status_code, 200)

    def test_a_token_is_required_when_the_endpoint_asks_for_one(self):
        """Without a token the tool surface is closed."""
        with self._client({"require_auth": True}, {}) as client:
            response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})

        self.assertEqual(response.status_code, 401)

    def test_a_valid_token_opens_the_tool_surface(self):
        """The account that holds the token may use the tools."""
        config = {"users": {"items": [{"login": "mcp", "token": "mcp-token"}]}}
        with self._client({"require_auth": True}, config) as client:
            response = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={"Authorization": "Bearer mcp-token"},
            )

        self.assertEqual(response.status_code, 200)

    def test_an_invalid_token_is_refused(self):
        """A wrong token does not open the tool surface."""
        config = {"users": {"items": [{"login": "mcp", "token": "mcp-token"}]}}
        with self._client({"require_auth": True}, config) as client:
            response = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={"Authorization": "Bearer wrong"},
            )

        self.assertEqual(response.status_code, 401)


class QueueStateAuthenticationTests(unittest.TestCase):
    """The queue-state routes follow the endpoint's authentication policy."""

    def _client(self, endpoint_config: dict, config: dict) -> TestClient:
        """Build a client for one configured OpenAIx endpoint."""
        endpoint = Endpoint_openaix({"id": "openaix", "api": "openaix", **endpoint_config})
        return TestClient(endpoint.create_app(_Core(config)))

    def test_the_queue_state_is_public_without_the_requirement(self):
        """The default stays as it was."""
        with self._client({}, {}) as client:
            response = client.get("/v1/models/some-model/queue-state")

        self.assertNotEqual(response.status_code, 401)

    def test_a_token_is_required_when_the_endpoint_asks_for_one(self):
        """The queue depth is not public when the endpoint is closed."""
        with self._client({"require_auth": True}, {}) as client:
            response = client.get("/v1/models/some-model/queue-state")

        self.assertEqual(response.status_code, 401)

    def test_a_valid_token_opens_the_queue_state(self):
        """The account that holds the token may read the queue depth."""
        config = {"users": {"items": [{"login": "reader", "token": "reader-token"}]}}
        with self._client({"require_auth": True}, config) as client:
            response = client.get(
                "/v1/models/some-model/queue-state",
                headers={"Authorization": "Bearer reader-token"},
            )

        self.assertNotEqual(response.status_code, 401)


class ResourceRegistryUpdateTests(unittest.IsolatedAsyncioTestCase):
    """The resource registry must be updatable without losing the reservations."""

    async def test_an_update_keeps_the_reservations(self):
        """A reload must not throw away the memory a running task holds."""
        resources = Resources([{"id": "gpu", "type": "cuda", "limits": {"VRAM": 20}}])
        await resources.reserve_blind_for({"gpu": {"VRAM": 8}}, consumer_id="task-1")

        changes = resources.apply_config([
            {"id": "gpu", "type": "cuda", "limits": {"VRAM": 40}, "keep_alive": 60},
        ])

        self.assertEqual(changes["updated"], ["gpu"])
        self.assertEqual(changes["added"], [])
        self.assertEqual(changes["removed"], [])
        self.assertEqual(resources.get("gpu").limits, {"VRAM": 40})
        self.assertEqual(resources.get("gpu").used.get("VRAM"), 8)
        self.assertEqual(resources.get("gpu").keep_alive, 60)

    def test_a_new_resource_starts_empty_and_a_removed_one_disappears(self):
        """The registry follows the configuration."""
        resources = Resources([{"id": "gpu", "type": "cuda", "limits": {"VRAM": 20}}])

        changes = resources.apply_config([
            {"id": "other", "type": "cuda", "limits": {"VRAM": 10}},
        ])

        self.assertEqual(changes["added"], ["other"])
        self.assertEqual(changes["removed"], ["gpu"])
        self.assertIsNone(resources.get("gpu"))
        self.assertIsNotNone(resources.get("other"))
        self.assertEqual(resources.get("other").used.get("VRAM", 0), 0)


class ReloadConfigTests(unittest.IsolatedAsyncioTestCase):
    """A reload must re-apply the runtime parts and report the structural ones."""

    def _core(self, config: dict) -> object:
        """Build a Core double with the pieces the reload touches."""
        from core.app import Core

        core = Core.__new__(Core)
        core.config = _Config(config)
        core.workers = {}
        core.workers_cfg = {}
        core.resources = Resources(config.get("resources") or [])
        return core

    async def test_the_workers_are_re_initialized(self):
        """A worker whose section changed is initialized again."""
        from core.app import Core

        worker = SimpleNamespace(id="openaix", initialize=AsyncMock())
        core = self._core({"workers": {"items": {"openaix": {"provider": "smart"}}}})
        core.workers = {"openaix": worker}

        with patch("core.app.resolve_worker_configs", return_value={"openaix": {"provider": "smart"}}), \
             patch.object(Core, "_sections_needing_restart", staticmethod(lambda before, after: [])):
            result = await Core.reload_config(core)

        worker.initialize.assert_awaited_once()
        self.assertEqual(result["workers"], ["openaix"])

    async def test_the_changed_structural_sections_are_reported(self):
        """A changed listener is reported instead of being pretended away."""
        from core.app import Core

        core = self._core({
            "endpoints": [{"id": "openaix", "port": 21434}],
            "workers": {"items": {}},
        })
        core.workers = {}
        before = {"endpoints": [{"id": "openaix", "port": 21434}]}
        after = {"endpoints": [{"id": "openaix", "port": 21435}]}

        changed = Core._sections_needing_restart(before, after)

        self.assertEqual(changed, ["endpoints"])


if __name__ == "__main__":
    unittest.main()
