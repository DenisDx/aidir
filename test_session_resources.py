"""Tests for per-model parallel admission and session resource accounting."""
from __future__ import annotations

import unittest

from core.resources import Resources


def _config(max_concurrent: int | None = None, session: int = 1200, base: int = 19000) -> dict:
    model = {
        "id": "m",
        "name": "m",
        "resources": {"gpu": {"VRAM": base}},
        "sessionResources": {"gpu": {"VRAM": session}},
    }
    if max_concurrent is not None:
        model["maxConcurrentRequests"] = max_concurrent
    return {"models": {"providers": {"p": {"api": "ollama", "models": [model]}}}}


def _resources(config: dict) -> Resources:
    resources = Resources([
        {"id": "gpu", "type": "cuda", "limits": {"VRAM": 22000}, "units": {"VRAM": "MiB"}, "alive_time": 300},
    ])
    resources.set_full_config(config)
    return resources


class SessionResourcesTests(unittest.IsolatedAsyncioTestCase):
    async def test_base_is_reserved_once_and_sessions_are_incremental(self):
        resources = _resources(_config(max_concurrent=2))
        base = {"gpu": {"VRAM": 19000}}
        first = resources.evaluate_model_admission(base, "m", "p")
        self.assertEqual(first["admission"], "ready")
        self.assertFalse(first["model_loaded"])
        self.assertEqual(first["needed"], {"gpu": {"VRAM": 20200}})

        held = await resources.reserve_model_start(base, "m", "p", "t1")
        self.assertTrue(held["admissible_now"])
        self.assertEqual(resources.model_active_count("p", "m"), 1)

        second = resources.evaluate_model_admission(base, "m", "p")
        self.assertTrue(second["model_loaded"])
        self.assertEqual(second["needed"], {"gpu": {"VRAM": 1200}})
        held2 = await resources.reserve_model_start(base, "m", "p", "t2")
        self.assertTrue(held2["admissible_now"])
        self.assertEqual(resources.model_active_count("p", "m"), 2)

        third = resources.evaluate_model_admission(base, "m", "p")
        self.assertEqual(third["admission"], "concurrency")

        await resources.release_model_session(
            held2["session_requirements"], "m", "p", "t2",
            base_requirements=held2["base_requirements"],
        )
        self.assertEqual(resources.model_active_count("p", "m"), 1)
        await resources.release_model_session(
            held["session_requirements"], "m", "p", "t1",
            base_requirements=held["base_requirements"],
        )
        self.assertEqual(resources.model_active_count("p", "m"), 0)
        warm = resources.evaluate_model_admission(base, "m", "p")
        self.assertTrue(warm["model_loaded"])
        self.assertEqual(warm["admission"], "ready")

    async def test_a_warm_model_needs_only_room_for_its_session(self):
        resources = _resources(_config(max_concurrent=4))
        base = {"gpu": {"VRAM": 19000}}
        await resources.reserve_model_start(base, "m", "p", "t1")
        # 3000 MiB is occupied by the model itself; 1200 MiB still fits.
        admission = resources.evaluate_model_admission(base, "m", "p")
        self.assertEqual(admission["admission"], "ready")
        self.assertTrue(admission["admissible_now"])

    async def test_memory_exhaustion_reports_session_memory(self):
        resources = _resources(_config(max_concurrent=4, session=2000))
        base = {"gpu": {"VRAM": 19000}}
        await resources.reserve_model_start(base, "m", "p", "t1")
        admission = resources.evaluate_model_admission(base, "m", "p")
        self.assertEqual(admission["admission"], "session_memory")
        self.assertFalse(admission["admissible_now"])
        self.assertEqual(resources.memory_slots("m", "p"), 0)

    async def test_legacy_single_request_mode_uses_concurrency_not_memory(self):
        resources = _resources(_config(max_concurrent=None))
        base = {"gpu": {"VRAM": 19000}}
        self.assertEqual(resources.evaluate_model_admission(base, "m", "p")["admission"], "ready")
        resources.mark_model_started("p", "m")
        admission = resources.evaluate_model_admission(base, "m", "p")
        self.assertEqual(admission["admission"], "concurrency")
        resources.mark_model_finished("p", "m")

    def test_parallel_mode_requires_session_resources_for_local_resources(self):
        config = _config(max_concurrent=2)
        model = config["models"]["providers"]["p"]["models"][0]
        model.pop("sessionResources")
        errors = _resources(config).validate_model_parallel_config()
        self.assertTrue(errors)
        self.assertIn("sessionResources", errors[0])

    async def test_force_unload_invalidates_the_routing_affinity(self):
        resources = _resources(_config(max_concurrent=4))
        base = {"gpu": {"VRAM": 19000}}
        held = await resources.reserve_model_start(base, "m", "p", "t1")
        await resources.release_model_session(
            held["session_requirements"], "m", "p", "t1",
            base_requirements=held["base_requirements"],
        )
        invalidated: list[tuple[str, str]] = []
        resources.set_candidate_invalidator(lambda provider, model: invalidated.append((provider, model)))

        async def fake_unload(resource, model_id, provider_id, full_config):
            return True

        resources._call_provider_unload = fake_unload
        released = await resources.force_unload_for({"gpu": {}}, _config(max_concurrent=4))
        self.assertTrue(released)
        self.assertIn(("p", "m"), invalidated)


if __name__ == "__main__":
    unittest.main()
