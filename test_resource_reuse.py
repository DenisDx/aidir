"""Regression tests for warm-model reuse without unnecessary unload."""

from __future__ import annotations

import unittest

from core.resource import Resource
from core.resources import Resources


class TestResourceReuse(unittest.IsolatedAsyncioTestCase):
    """Validate that repeated requests can reuse the same warm model."""

    async def test_resource_reuses_same_soft_model_without_double_counting(self) -> None:
        """Turns a matching soft consumer back into active usage instead of treating it as a blocker."""
        resource = Resource("gpu", "cuda", {"VRAM": 10}, alive_time=300)

        await resource.release({"VRAM": 8}, consumer_id="prev", model_id="model-a")

        self.assertFalse(resource.is_available({"VRAM": 8}))
        self.assertTrue(resource.is_available_for_reuse({"VRAM": 8}, "model-a"))

        await resource.reserve_blind({"VRAM": 8}, consumer_id="next", model_id="model-a")

        snapshot = resource.snapshot()
        self.assertEqual(snapshot["used"]["VRAM"], 8)
        self.assertEqual(snapshot["soft_used"].get("VRAM", 0), 0)
        self.assertEqual([item["model_id"] for item in snapshot["soft_consumers"]], [])

    async def test_resource_reuse_still_respects_other_soft_consumers(self) -> None:
        """Refuses same-model reuse when other warm models still exceed the resource limit."""
        resource = Resource("gpu", "cuda", {"VRAM": 10}, alive_time=300)

        await resource.release({"VRAM": 8}, consumer_id="prev-a", model_id="model-a")
        await resource.release({"VRAM": 4}, consumer_id="prev-b", model_id="model-b")

        self.assertFalse(resource.is_available_for_reuse({"VRAM": 8}, "model-a"))

    async def test_force_unload_keeps_requested_warm_model(self) -> None:
        """Selective force-unload preserves the warm model that is about to be reused."""
        resources = Resources(
            [
                {
                    "id": "gpu",
                    "type": "cuda",
                    "limits": {"VRAM": 20},
                    "alive_time": 300,
                    "provider": "ollama_local",
                }
            ]
        )
        reqs = {"gpu": {"VRAM": 8}}

        await resources.release_for(reqs, consumer_id="prev-a", model_id="model-a")
        await resources.release_for({"gpu": {"VRAM": 4}}, consumer_id="prev-b", model_id="model-b")

        unloaded: list[str] = []

        async def _fake_unload(_res, model_id, _provider_id, _full_config):
            unloaded.append(model_id)
            return True

        resources._call_provider_unload = _fake_unload  # type: ignore[method-assign]

        await resources.force_unload_for(reqs, full_config={}, keep_model_id="model-a")

        remaining = resources.get("gpu").get_active_soft_consumers()
        self.assertEqual(unloaded, ["model-b"])
        self.assertEqual([entry["model_id"] for entry in remaining], ["model-a"])

    async def test_force_unload_uses_soft_consumer_provider(self) -> None:
        """Uses the provider that owns an idle model instead of the legacy resource default."""
        resources = Resources(
            [{"id": "gpu", "type": "cuda", "limits": {"VRAM": 20}, "alive_time": 300, "provider": "ollama_local"}]
        )
        await resources.release_for(
            {"gpu": {"VRAM": 8}},
            consumer_id="prev",
            model_id="shared-model",
            provider_id="llama_local",
        )

        unloaded: list[tuple[str, str]] = []

        async def _fake_unload(_res, model_id, provider_id, _full_config):
            unloaded.append((model_id, provider_id))
            return True

        resources._call_provider_unload = _fake_unload  # type: ignore[method-assign]
        await resources.force_unload_for({"gpu": {"VRAM": 8}}, full_config={})

        self.assertEqual(unloaded, [("shared-model", "llama_local")])


if __name__ == "__main__":
    unittest.main(verbosity=2)