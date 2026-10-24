"""Smart-router tests for the direct single-candidate strategy."""
from __future__ import annotations

import unittest

from core.smart_router import SmartRouteError, SmartRouter


class DirectRoutingTests(unittest.IsolatedAsyncioTestCase):
    """A direct smart model always targets its one configured candidate."""

    def _router(self, items: list[dict], admission: str = "ready") -> SmartRouter:
        """Build a router with one smart model and a fixed admission state."""
        self.items = items
        return SmartRouter(
            endpoint_id="test",
            default_worker_id="worker",
            find_provider_model_cfg=self._model_cfg,
            provider_api=lambda provider: "ollama",
            resolve_model_resource_requirements=lambda provider, model: {"gpu": {"VRAM": 1000}},
            get_local_queue_state=self._queue,
            check_resource_available=lambda requirements: True,
            check_resource_available_after_unload=lambda requirements: True,
            check_resource_available_for_reuse=lambda requirements, model, provider: True,
            probe_remote_model_queue_state=self._remote,
            probe_ollama_model_availability=self._probe,
            resolve_probe_timeout_ms=lambda item: 100,
            resolve_worker_id_for_route=lambda worker, route: worker,
            get_model_queue_state=self._queue,
            evaluate_model_admission=lambda requirements, model, provider: {
                "admission": admission,
                "admissible_now": admission == "ready",
                "admissible_after_unload": True,
                "model_loaded": True,
                "active_count": 0,
                "max_concurrent_requests": 1,
                "free": {"gpu": {"VRAM": 1000}},
                "needed": {"gpu": {"VRAM": 1000}},
                "resource_ready": admission == "ready",
                "resource_ready_after_unload": True,
            },
        )

    def _model_cfg(self, provider: str, model: str) -> dict | None:
        """Return the direct smart model or a concrete candidate."""
        if provider == "smart" and model == "direct1":
            return {"id": "direct1", "type": "direct", "items": self.items}
        if provider in {"p1", "p2"}:
            return {"id": model}
        return None

    async def _queue(self, *args, **kwargs) -> dict | None:
        """Report an empty queue."""
        return {"queued_count_total": 0, "queued_count_below_priority": 0}

    async def _remote(self, *args, **kwargs) -> dict | None:
        """Return no remote state."""
        return None

    async def _probe(self, *args, **kwargs) -> bool:
        """Report every candidate as reachable."""
        return True

    async def test_direct_selects_its_only_candidate_when_ready(self) -> None:
        """A ready direct candidate is selected immediately."""
        router = self._router([{"provider": "p1", "model": "m1"}])
        resolution = await router.resolve_route(
            {"requested_model": "direct1", "resolved_provider": "smart", "resolved_model": "direct1"},
            request_payload={"messages": [{"role": "user", "content": "hello"}]},
        )
        self.assertEqual((resolution.resolved_provider, resolution.resolved_model), ("p1", "m1"))
        self.assertEqual(resolution.route["selection_reason"], "immediate")

    async def test_direct_queues_when_admission_is_not_ready(self) -> None:
        """A direct candidate that cannot start is queued, not forced."""
        router = self._router([{"provider": "p1", "model": "m1"}], admission="concurrency")
        resolution = await router.resolve_route(
            {"requested_model": "direct1", "resolved_provider": "smart", "resolved_model": "direct1"},
            request_payload={"messages": [{"role": "user", "content": "hello"}]},
        )
        self.assertEqual((resolution.resolved_provider, resolution.resolved_model), ("p1", "m1"))
        self.assertEqual(resolution.route["selection_reason"], "busy_fallback")

    async def test_direct_rejects_multiple_candidates(self) -> None:
        """A direct model with more than one item is a configuration error."""
        router = self._router([
            {"provider": "p1", "model": "m1"},
            {"provider": "p2", "model": "m2"},
        ])
        with self.assertRaises(SmartRouteError) as raised:
            await router.resolve_route(
                {"requested_model": "direct1", "resolved_provider": "smart", "resolved_model": "direct1"},
                request_payload={"messages": [{"role": "user", "content": "hello"}]},
            )
        self.assertEqual(raised.exception.code, "INVALID_ROUTING_CONFIG")
        self.assertEqual(raised.exception.status_code, 400)

    async def test_direct_rejects_no_candidates(self) -> None:
        """A direct model without a candidate is a configuration error."""
        router = self._router([])
        with self.assertRaises(SmartRouteError) as raised:
            await router.resolve_route(
                {"requested_model": "direct1", "resolved_provider": "smart", "resolved_model": "direct1"},
                request_payload={"messages": [{"role": "user", "content": "hello"}]},
            )
        self.assertEqual(raised.exception.code, "INVALID_ROUTING_CONFIG")
        self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
