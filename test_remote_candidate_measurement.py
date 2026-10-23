"""Regression tests for the measurement of a remote smart-routing candidate.

A candidate on another aidir instance cannot be evaluated from the local
configuration: its capacity belongs to that instance. The router has to ask it,
and a candidate that cannot be asked is not ready.
"""
from __future__ import annotations

import unittest

from core.kv_affinity import KvAffinityStore
from core.smart_router import SmartRouter


class _Config:
    """Configuration double for the router."""

    def __init__(self) -> None:
        """Build the provider and model catalogue."""
        self._providers = {
            "smart": {
                "api": "smart",
                "models": [
                    {
                        "id": "local-only",
                        "alias": "local-only",
                        "type": "first_available",
                        "items": [
                            {"provider": "llama_local", "model": "m-local", "fallback_prio": 10},
                            {"provider": "remote_aidir_110", "model": "m-remote", "fallback_prio": 1},
                        ],
                    }
                ],
            },
            "llama_local": {
                "api": "llama-cpp",
                "baseUrl": "http://127.0.0.1:8888",
                "models": [{"id": "m-local", "resources": {"local_machine": {"VRAM": 22000}}}],
            },
            "remote_aidir_110": {
                "api": "openaix",
                "baseUrl": "http://192.168.1.110:21434",
                "models": [{"id": "m-remote", "resources": {"remote_110": {"VRAM": 22000}}}],
            },
        }

    def find_provider_model_cfg(self, provider_id: str, model_id: str):
        """Return the model configuration of one provider."""
        provider = self._providers.get(provider_id) or {}
        for model in provider.get("models", []):
            if model.get("id") == model_id:
                return model
        return None

    def provider_api(self, provider_id: str) -> str:
        """Return the api of one provider."""
        return str((self._providers.get(provider_id) or {}).get("api") or "")

    def resolve_model_resource_requirements(self, provider_id: str, model_id: str):
        """Return the resources of one model."""
        model = self.find_provider_model_cfg(provider_id, model_id) or {}
        resources = model.get("resources")
        return dict(resources) if isinstance(resources, dict) else None


class _Router(SmartRouter):
    """Router with the callbacks the test controls."""

    def __init__(self, remote_state: dict | None, remote_error: str | None = None) -> None:
        """Store what the remote probe answers."""
        self.remote_state = remote_state
        self.remote_error = remote_error
        self.remote_probes: list[tuple[str, str]] = []
        self.local_probes: list[tuple[str, str]] = []
        config = _Config()
        super().__init__(
            endpoint_id="test",
            default_worker_id="openaix",
            find_provider_model_cfg=config.find_provider_model_cfg,
            provider_api=config.provider_api,
            resolve_model_resource_requirements=config.resolve_model_resource_requirements,
            get_local_queue_state=self._local_queue_state,
            check_resource_available=self._resource_available,
            check_resource_available_after_unload=self._resource_available,
            check_resource_available_for_reuse=self._resource_reusable,
            probe_remote_model_queue_state=self._probe_remote,
            probe_ollama_model_availability=self._probe_local,
            resolve_probe_timeout_ms=lambda item: 1500,
            # The affinity makes the router evaluate every candidate, which is
            # the production behavior: every candidate is measured before one
            # of them is chosen.
            kv_affinity=KvAffinityStore(),
            affinity_config={"enabled": True},
        )

    async def _local_queue_state(self, requirements, priority):
        """Report an empty local queue."""
        return {"queued_count_total": 0, "queued_count_below_priority": 0, "priority_counts": []}

    def _resource_available(self, requirements) -> bool:
        """Report the local capacity as free."""
        return True

    def _resource_reusable(self, requirements, model_id, provider_id) -> bool:
        """Report no warm model to reuse."""
        return False

    async def _probe_local(self, provider_id, model_id, *, timeout_ms, incoming_bearer_token=""):
        """Record a local probe and report success."""
        self.local_probes.append((provider_id, model_id))
        return True

    async def _probe_remote(self, provider_id, model_id, *, priority, timeout_ms, incoming_bearer_token=""):
        """Record a remote probe and answer with the configured state."""
        self.remote_probes.append((provider_id, model_id))
        if self.remote_error is not None:
            return None
        return self.remote_state


class RemoteCandidateMeasurementTests(unittest.IsolatedAsyncioTestCase):
    """A remote candidate must be measured, not assumed."""

    def _route(self) -> dict:
        """Return the smart route request."""
        return {
            "requested_model": "local-only",
            "requested_alias": "local-only",
            "resolved_provider": "smart",
            "resolved_model": "local-only",
        }

    def _payload(self) -> dict:
        """Return the request payload."""
        return {"model": "local-only", "messages": [{"role": "user", "content": "one"}]}

    async def test_a_remote_candidate_is_probed_over_the_network(self):
        """The remote instance answers whether the model can run."""
        router = _Router({"can_run_now": True, "admission": "ready", "model_loaded": True})
        resolution = await router.resolve_route(self._route(), request_payload=self._payload())

        self.assertIn(("remote_aidir_110", "m-remote"), router.remote_probes)
        self.assertNotIn(("remote_aidir_110", "m-remote"), router.local_probes)
        probes = {item["provider"]: item for item in resolution.route["candidate_probes"]}
        self.assertEqual(probes["remote_aidir_110"]["probe_source"], "remote_openaix")

    async def test_a_remote_that_cannot_run_is_not_ready(self):
        """The remote's own answer decides, not the local configuration."""
        router = _Router({
            "can_run_now": False,
            "admission": "base_memory",
            "free": {"local_machine": {"VRAM": 13795.0}},
            "needed": {"local_machine": {"VRAM": 21500}},
        })
        resolution = await router.resolve_route(self._route(), request_payload=self._payload())

        probes = {item["provider"]: item for item in resolution.route["candidate_probes"]}
        remote = probes["remote_aidir_110"]
        self.assertFalse(remote["can_run_now"])
        self.assertEqual(remote["admission"], "base_memory")

    async def test_a_remote_that_does_not_answer_is_not_ready(self):
        """A candidate that cannot be measured is not preferred."""
        router = _Router(None, remote_error="unreachable")
        resolution = await router.resolve_route(self._route(), request_payload=self._payload())

        probes = {item["provider"]: item for item in resolution.route["candidate_probes"]}
        remote = probes["remote_aidir_110"]
        self.assertFalse(remote["probe_ok"])
        self.assertEqual(remote["probe_source"], "remote_openaix")
        self.assertEqual(resolution.route["resolved_provider"], "llama_local")


if __name__ == "__main__":
    unittest.main()
