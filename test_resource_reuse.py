"""Regression tests for warm-model reuse without unnecessary unload."""

from __future__ import annotations

import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock

from core.resource import Resource
from core.resources import Resources


class _ActivityRedis:
    """Fake Redis for activity-record restoration and deletion."""

    def __init__(self, data: dict[str, str]) -> None:
        """Store activity records keyed by Redis key."""
        self.data = data
        self.deleted: list[str] = []

    async def scan(self, cursor, match, count):
        """Return every seeded activity key in one page."""
        return 0, list(self.data)

    async def get(self, key):
        """Return one activity record."""
        return self.data.get(key)

    async def delete(self, key):
        """Delete one activity record."""
        self.deleted.append(key)
        self.data.pop(key, None)

    async def set(self, key, value, ex=None):
        """Persist one activity record for the cron keep-alive tests."""
        self.data[key] = value


class TestResourceReuse(unittest.IsolatedAsyncioTestCase):
    """Validate that repeated requests can reuse the same warm model."""

    async def test_observed_free_memory_can_be_recovered_by_unloading_soft_consumers(self) -> None:
        """Plan an unload when observed free VRAM is low only because idle models hold it."""
        resource = Resource(
            "gpu", "cuda", {"VRAM": 22}, alive_time=300,
            availability={"command": "nvidia-smi", "metric": "VRAM"},
        )
        resource.record_observed_availability("command", {"VRAM": 5})
        resource.add_soft_consumer({"VRAM": 10}, "idle-model", "ollama_local")

        self.assertFalse(resource.is_available({"VRAM": 12}))
        self.assertTrue(resource.is_available_after_unload({"VRAM": 12}))
        self.assertFalse(resource.is_available_after_unload({"VRAM": 16}))

    async def test_activity_restore_rebuilds_soft_consumer_after_restart(self) -> None:
        """Restore an idle Ollama model from its persisted keep-alive activity record."""
        now = time.time()
        key = "aidir:resource:gpu:activity:ollama_local:model-a"
        redis = _ActivityRedis({
            key: json.dumps({
                "model_id": "model-a",
                "resource_id": "gpu",
                "provider": "ollama_local",
                "released_at": now - 60,
                "last_used_at": now - 60,
                "last_ping_at": now - 60,
                "keep_alive": 5000,
                "keep_alive_period": 250,
            }),
        })
        resources = Resources([{"id": "gpu", "type": "cuda", "limits": {"VRAM": 22}, "alive_time": 300, "keep_alive": 5000}])
        resources.set_redis(redis, "aidir")
        full_config = {"models": {"providers": {"ollama_local": {
            "api": "ollama",
            "models": [{"id": "model-a", "resources": {"gpu": {"VRAM": 10}}}],
        }}}}

        restored = await resources.restore_activity_consumers(full_config)

        self.assertEqual(restored, 1)
        snapshot = resources.get("gpu").snapshot()
        self.assertEqual(snapshot["soft_used"]["VRAM"], 10)
        self.assertEqual(snapshot["soft_consumers"][0]["model_id"], "model-a")
        self.assertGreater(snapshot["soft_consumers"][0]["expires_in"], 0)

    async def test_force_unload_clears_keep_alive_activity(self) -> None:
        """Do not let cron resurrect a model that was explicitly unloaded."""
        now = time.time()
        key = "aidir:resource:gpu:activity:ollama_local:model-a"
        redis = _ActivityRedis({
            key: json.dumps({
                "model_id": "model-a",
                "resource_id": "gpu",
                "provider": "ollama_local",
                "released_at": now,
                "last_used_at": now,
                "keep_alive": 5000,
                "keep_alive_period": 250,
            }),
        })
        resources = Resources([{"id": "gpu", "type": "cuda", "limits": {"VRAM": 22}, "alive_time": 300, "keep_alive": 5000}])
        resources.set_redis(redis, "aidir")
        resources.set_full_config({"models": {"providers": {"ollama_local": {"api": "ollama"}}}})
        resources.get("gpu").add_soft_consumer({"VRAM": 10}, "model-a", "ollama_local")
        resources._call_provider_unload = AsyncMock(return_value=True)

        unloaded = await resources.force_unload_for({"gpu": {"VRAM": 10}})

        self.assertTrue(unloaded)
        self.assertEqual(redis.deleted, [key])
        self.assertEqual(resources.get("gpu").snapshot()["soft_consumers"], [])

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

    async def test_atomic_reservation_reuses_matching_warm_model(self) -> None:
        """Reserves a matching warm model when ordinary capacity is unavailable."""
        resources = Resources([{"id": "gpu", "type": "cuda", "limits": {"VRAM": 10}, "alive_time": 300}])
        requirements = {"gpu": {"VRAM": 8}}
        await resources.release_for(
            requirements,
            consumer_id="previous",
            model_id="model-a",
            provider_id="llama_local",
        )

        reserved = await resources.reserve_if_available_or_reuse(
            requirements,
            consumer_id="next",
            model_id="model-a",
            provider_id="llama_local",
        )

        self.assertTrue(reserved)
        self.assertEqual(resources.get("gpu").snapshot()["used"]["VRAM"], 8)

    async def test_resource_reuse_still_respects_other_soft_consumers(self) -> None:
        """Refuses same-model reuse when other warm models still exceed the resource limit."""
        resource = Resource("gpu", "cuda", {"VRAM": 10}, alive_time=300)

        await resource.release({"VRAM": 8}, consumer_id="prev-a", model_id="model-a")
        await resource.release({"VRAM": 4}, consumer_id="prev-b", model_id="model-b")

        self.assertFalse(resource.is_available_for_reuse({"VRAM": 8}, "model-a"))

    async def test_disabled_resource_is_unavailable_for_new_work(self) -> None:
        """Prevents both normal allocation and warm-model reuse while use is disabled."""
        resource = Resource("gpu", "cuda", {"VRAM": 10}, alive_time=300)
        await resource.release({"VRAM": 8}, consumer_id="prev", model_id="model-a")
        resource.use = False

        self.assertFalse(resource.is_available({"VRAM": 1}))
        self.assertFalse(resource.is_available_for_reuse({"VRAM": 8}, "model-a"))
        self.assertFalse(resource.is_available_after_unload({"VRAM": 1}))
        self.assertFalse(resource.snapshot()["use"])

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

    async def test_force_release_unloads_idle_models_without_touching_active_tasks(self) -> None:
        """Releases every idle model on a resource but refuses while a task holds it."""
        resources = Resources(
            [{"id": "gpu", "type": "cuda", "limits": {"VRAM": 20}, "alive_time": 300}]
        )
        await resources.release_for({"gpu": {"VRAM": 8}}, consumer_id="prev", model_id="model-a")
        unloaded: list[str] = []

        async def _fake_unload(_res, model_id, _provider_id, _full_config):
            unloaded.append(model_id)
            return True

        resources._call_provider_unload = _fake_unload  # type: ignore[method-assign]
        result = await resources.force_release("gpu")

        self.assertEqual(result, {"released": True, "active_consumers": [], "unloaded_models": ["model-a"]})
        self.assertEqual(unloaded, ["model-a"])
        self.assertEqual(resources.get("gpu").get_active_soft_consumers(), [])

        await resources.reserve_blind_for({"gpu": {"VRAM": 8}}, consumer_id="active")
        blocked = await resources.force_release("gpu")

        self.assertEqual(blocked, {"released": False, "active_consumers": ["active"], "unloaded_models": []})

    async def test_atomic_admission_never_partially_reserves_resources(self) -> None:
        """Reject a multi-resource request without reserving its otherwise available resource."""
        resources = Resources([
            {"id": "gpu_a", "type": "cuda", "limits": {"VRAM": 10}},
            {"id": "gpu_b", "type": "cuda", "limits": {"VRAM": 4}},
        ])

        reserved = await resources.reserve_if_available(
            {"gpu_a": {"VRAM": 5}, "gpu_b": {"VRAM": 5}},
            consumer_id="task",
        )

        self.assertFalse(reserved)
        self.assertEqual(resources.get("gpu_a").used["VRAM"], 0)
        self.assertEqual(resources.get("gpu_b").used["VRAM"], 0)

    async def test_atomic_admission_serializes_competing_reservations(self) -> None:
        """Allow only one concurrent request when both need more than half of one resource."""
        resources = Resources([{"id": "gpu", "type": "cuda", "limits": {"VRAM": 10}}])

        first, second = await asyncio.gather(
            resources.reserve_if_available({"gpu": {"VRAM": 6}}, consumer_id="first"),
            resources.reserve_if_available({"gpu": {"VRAM": 6}}, consumer_id="second"),
        )

        self.assertCountEqual([first, second], [True, False])
        self.assertEqual(resources.get("gpu").used["VRAM"], 6)

    async def test_command_observation_allows_reuse_of_a_warm_model(self) -> None:
        """Allow a matching warm model despite low currently free observed memory."""
        resource = Resource(
            "gpu",
            "cuda",
            {"VRAM": 22},
            alive_time=300,
            availability={"command": "nvidia-smi", "metric": "VRAM"},
        )
        await resource.release({"VRAM": 19}, consumer_id="previous", model_id="qwen")
        resource.record_observed_availability("command", {"VRAM": 3})

        self.assertFalse(resource.is_available({"VRAM": 19}))
        self.assertTrue(resource.is_available_for_reuse({"VRAM": 19}, "qwen"))


if __name__ == "__main__":
    unittest.main(verbosity=2)