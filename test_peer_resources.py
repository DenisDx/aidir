"""Regression tests for OpenAIx peer resource responses."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from core.endpoints.endpoint_openaix import Endpoint_openaix
from core.peer_resources import PeerResourceClient
from core.resources import Resources


class _Config:
    """Provide dotted configuration lookup for endpoint tests."""

    def __init__(self, values: dict) -> None:
        self._values = values

    def get(self, key: str, default=None):
        """Return a nested value or the supplied default."""
        value = self._values
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


class _Core:
    """Provide resources and configuration required by the OpenAIx endpoint."""

    def __init__(self, config: dict) -> None:
        self.config = _Config(config)
        self.resources = Resources([{
            "id": "gpu",
            "type": "cuda",
            "limits": {"VRAM": 20},
            "units": {"VRAM": "MiB"},
            "availability": {"command": "nvidia-smi", "metric": "VRAM"},
        }])
        self.resources.get("gpu").record_observed_availability("command", {"VRAM": 12})
        self.envid_registry = None
        self.audit_log = None


class _Response:
    """Provide one HTTP response for peer client tests."""

    def __init__(self, status_code: int, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        """Return the configured JSON payload or raise for an invalid body."""
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _AsyncClient:
    """Provide a deterministic async HTTP client for peer discovery tests."""

    def __init__(self, responses: list[_Response], **kwargs) -> None:
        self._responses = responses
        self.headers = kwargs.get("headers", {})
        self.urls: list[str] = []

    async def __aenter__(self):
        """Enter the async client context."""
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        """Exit the async client context."""

    async def get(self, url: str):
        """Return the next deterministic response."""
        self.urls.append(url)
        return self._responses.pop(0)


class TestPeerResources(unittest.TestCase):
    """Verify peer resource API access and response redaction."""

    def _client(self, endpoint_config: dict, config: dict) -> TestClient:
        """Create a test client for one configured endpoint."""
        endpoint = Endpoint_openaix({"id": "openaix", **endpoint_config})
        return TestClient(endpoint.create_app(_Core(config)))

    def test_resources_are_public_by_default_and_sanitized(self) -> None:
        """Return the minimal resource response without executable or internal state."""
        with self._client({}, {"http": {"max_request_size": 1024}}) as client:
            response = client.get("/v1/resources/gpu")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["id"], "gpu")
        self.assertEqual(payload["availability"]["available"], {"VRAM": 12.0})
        self.assertEqual(payload["telemetry"], {})
        self.assertEqual(payload["units"], {"VRAM": "MiB"})
        self.assertNotIn("nvidia-smi", response.text)
        self.assertNotIn("consumers", payload)
        self.assertNotIn("monitoring", payload)

    def test_endpoint_auth_mode_requires_valid_bearer_token(self) -> None:
        """Reject anonymous and invalid peer requests while accepting a configured token."""
        config = {
            "http": {"max_request_size": 1024},
            "users": {"items": [{"token": "peer-token"}]},
        }
        endpoint_config = {"peer_resources": {"auth_mode": "endpoint"}}
        with self._client(endpoint_config, config) as client:
            self.assertEqual(client.get("/api/resources").status_code, 401)
            self.assertEqual(
                client.get("/api/resources", headers={"Authorization": "Bearer invalid"}).status_code,
                401,
            )
            response = client.get("/api/resources", headers={"Authorization": "Bearer peer-token"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["protocol_version"], 1)
        self.assertEqual(response.json()["data"][0]["id"], "gpu")

    def test_peer_client_caches_definitive_non_peer_but_retries_transient_failures(self) -> None:
        """Cache endpoint absence while allowing a later retry after an HTTP failure."""
        config = {
            "models": {
                "providers": {
                    "peer": {
                        "api": "openaix",
                        "baseUrl": "http://peer.example",
                    }
                }
            }
        }

        async def exercise() -> None:
            """Run both cache classifications against mocked HTTP clients."""
            client = PeerResourceClient(config)
            missing_factory = lambda **kwargs: _AsyncClient([_Response(404), _Response(404)], **kwargs)
            with patch("core.peer_resources.httpx.AsyncClient", side_effect=missing_factory) as factory:
                first = await client.probe_resource("peer", "gpu", 100)
                second = await client.probe_resource("peer", "gpu", 100)
            self.assertEqual(first.status, "not_peer")
            self.assertEqual(second.status, "not_peer")
            self.assertEqual(factory.call_count, 1)

            retrying = PeerResourceClient(config)
            payload = {
                "id": "gpu",
                "limits": {"VRAM": 20},
                "availability": {"status": "ok", "available": {"VRAM": 12}},
            }
            transient_factory = lambda **kwargs: _AsyncClient([_Response(503)], **kwargs)
            success_factory = lambda **kwargs: _AsyncClient([_Response(200, payload)], **kwargs)
            with patch(
                "core.peer_resources.httpx.AsyncClient",
                side_effect=[transient_factory(), success_factory()],
            ) as factory:
                failed = await retrying.probe_resource("peer", "gpu", 100)
                succeeded = await retrying.probe_resource("peer", "gpu", 100)
            self.assertEqual(failed.status, "failed")
            self.assertEqual(succeeded.status, "ok")
            self.assertEqual(factory.call_count, 2)

        import asyncio
        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main(verbosity=2)
