"""Routing tests for Anthropic providers and per-model Messages protocols."""
from __future__ import annotations

import unittest

from core.endpoints.endpoint_anthropic import Endpoint_anthropic
from core.smart_router import SmartRouter


class _Config:
    """Dotted configuration accessor for routing tests."""

    def __init__(self, data: dict) -> None:
        """Store the nested configuration."""
        self._data = data

    def get(self, key: str, default=None):
        """Return a dotted value or the default."""
        value = self._data
        for part in key.split("."):
            if not isinstance(value, dict):
                return default
            value = value.get(part)
            if value is None:
                return default
        return value


class _Core:
    """Minimal core exposing the routing configuration."""

    def __init__(self) -> None:
        """Create the provider catalogue used by direct and smart routes."""
        data = {
            "models": {
                "providers": {
                    "anthropic_cloud": {
                        "api": "anthropic",
                        "baseUrl": "http://anthropic",
                        "models": [{"id": "claude-sonnet", "alias": "claude"}],
                    },
                    "opencode_go": {
                        "api": "opencode-go",
                        "baseUrl": "http://go",
                        "models": [
                            {"id": "glm-5.3", "alias": "go-glm"},
                            {"id": "minimax-m3", "alias": "go-minimax", "protocol": "anthropic-messages"},
                        ],
                    },
                    "smart": {
                        "api": "smart",
                        "models": [
                            {
                                "id": "any",
                                "type": "first_available",
                                "items": [
                                    {"provider": "opencode_go", "model": "minimax-m3"},
                                    {"provider": "anthropic_cloud", "model": "claude-sonnet"},
                                ],
                            }
                        ],
                    },
                }
            },
            "workers": {
                "default": "openaix",
                "items": {"openaix": {}, "call_anthropic": {}, "call_opencode_go": {}},
            },
        }
        self.config = _Config(data)
        self.audit_log = None
        self.resources = None
        self.queue = None


class AnthropicRoutingTests(unittest.IsolatedAsyncioTestCase):
    """Verify worker and URL selection for direct and smart routes."""

    def setUp(self) -> None:
        """Create an endpoint bound to the routing catalogue."""
        self.core = _Core()
        self.endpoint = Endpoint_anthropic({"id": "anthropic", "api": "anthropic"})
        self.endpoint._core = self.core

    def test_direct_anthropic_provider_selects_worker(self) -> None:
        """Route a generic Anthropic model to call_anthropic."""
        route = self.endpoint._resolve_model_route("claude", "openaix")
        self.assertEqual(route["resolved_provider"], "anthropic_cloud")
        self.assertEqual(route["resolved_protocol"], "anthropic-messages")
        self.assertEqual(self.endpoint._resolve_worker_id_for_route("openaix", route), "call_anthropic")

    def test_direct_opencode_messages_model_selects_go_worker(self) -> None:
        """Route an OpenCode Go Messages model to call_opencode_go."""
        route = self.endpoint._resolve_model_route("go-minimax", "openaix")
        self.assertEqual(route["resolved_protocol"], "anthropic-messages")
        self.assertEqual(self.endpoint._resolve_worker_id_for_route("openaix", route), "call_opencode_go")

    def test_direct_opencode_chat_model_stays_chat(self) -> None:
        """Keep a Chat Completions OpenCode Go model on the existing path."""
        route = self.endpoint._resolve_model_route("go-glm", "openaix")
        self.assertEqual(route["resolved_protocol"], "chat-completions")
        self.assertEqual(self.endpoint._resolve_worker_id_for_route("openaix", route), "call_opencode_go")

    async def test_smart_route_carries_protocol(self) -> None:
        """Carry the selected candidate's protocol into smart route metadata."""
        async def probe(provider_id: str, model_id: str, **_: object) -> bool:
            """Report every candidate available."""
            return True

        def find(provider_id: str, model_id: str):
            """Resolve a provider model entry."""
            return self.endpoint._find_provider_model_cfg(provider_id, model_id)

        router = SmartRouter(
            endpoint_id="anthropic",
            default_worker_id="openaix",
            find_provider_model_cfg=find,
            provider_api=self.endpoint._provider_api,
            resolve_model_resource_requirements=lambda provider_id, model_id: None,
            get_local_queue_state=None,
            check_resource_available=None,
            check_resource_available_after_unload=None,
            probe_remote_model_queue_state=None,
            probe_ollama_model_availability=None,
            probe_openai_model_availability=probe,
            resolve_probe_timeout_ms=lambda item: 100,
            resolve_worker_id_for_route=self.endpoint._resolve_worker_id_for_route,
            resolve_model_protocol=self.endpoint._model_protocol,
        )
        route = self.endpoint._resolve_model_route("any", "openaix")
        resolution = await router.resolve_route(route, request_payload={"model": "any"})
        self.assertEqual(resolution.route["resolved_provider"], "opencode_go")
        self.assertEqual(resolution.route["resolved_protocol"], "anthropic-messages")
        self.assertEqual(resolution.resolved_worker, "call_opencode_go")


if __name__ == "__main__":
    unittest.main(verbosity=2)
