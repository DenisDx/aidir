"""Routing-policy tests for prefix affinity and resource waits."""
from __future__ import annotations

import unittest

from core.kv_affinity import AnchorPolicy, KvAffinityStore, ModelReadinessTracker
from core.scheduler import Scheduler
from core.smart_router import SmartRouter
from core.task_types.task_agent import Task_agent


class RouterHarness:
    def __init__(self) -> None:
        self.now = [0.0]
        self.queues = {("p1", "m1"): 0, ("p2", "m2"): 0}
        self.admissions = {
            ("p1", "m1"): self._admission("ready", loaded=True),
            ("p2", "m2"): self._admission("ready", loaded=True),
        }
        self.probe_ok = {"p1": True, "p2": True}
        self.store = KvAffinityStore(
            policy=AnchorPolicy(short_context_bytes=1000000, medium_context_bytes=1000001,
                                long_context_bytes=1000002, short_fraction=1.0),
            clock=lambda: self.now[0],
        )
        self.tracker = ModelReadinessTracker(clock=lambda: self.now[0])
        self.router = SmartRouter(
            endpoint_id="test",
            default_worker_id="worker",
            find_provider_model_cfg=self._model_cfg,
            provider_api=lambda provider: "ollama",
            resolve_model_resource_requirements=lambda provider, model: {"gpu": {"VRAM": 1000}},
            get_local_queue_state=self._local_queue,
            check_resource_available=lambda requirements: True,
            check_resource_available_after_unload=lambda requirements: True,
            check_resource_available_for_reuse=lambda requirements, model, provider: True,
            probe_remote_model_queue_state=self._remote_probe,
            probe_ollama_model_availability=self._ollama_probe,
            resolve_probe_timeout_ms=lambda item: 100,
            resolve_worker_id_for_route=lambda worker, route: worker,
            kv_affinity=self.store,
            readiness=self.tracker,
            affinity_config={
                "max_queue_diff_len": 8,
                "max_queue_diff_part": 3.0,
                "resource_wait_timeout_seconds": 60,
                "cold_model_wait_timeout_seconds": 0,
                "resource_blocked_ratio": 0.5,
                "prefer_affinity_over_immediate": True,
            },
            get_model_queue_state=self._model_queue,
            evaluate_model_admission=self._admission_for,
        )

    @staticmethod
    def _admission(reason: str, *, loaded: bool) -> dict:
        return {
            "admission": reason,
            "admissible_now": reason == "ready",
            "admissible_after_unload": True,
            "model_loaded": loaded,
            "active_count": 0,
            "max_concurrent_requests": 1,
            "session_resources": {},
            "free": {"gpu": {"VRAM": 1000}},
            "needed": {"gpu": {"VRAM": 1000}},
            "resource_ready": reason == "ready",
            "resource_ready_after_unload": True,
        }

    def _model_cfg(self, provider: str, model: str) -> dict | None:
        if provider == "smart" and model == "smart1":
            return {
                "id": "smart1",
                "alias": "smart1",
                "type": "first_available",
                "items": [
                    {"provider": "p1", "model": "m1", "fallback_prio": 10},
                    {"provider": "p2", "model": "m2", "fallback_prio": 1},
                ],
            }
        if provider in {"p1", "p2"}:
            return {"id": model, "cache_group": model}
        return None

    async def _local_queue(self, requirements: dict, priority: int) -> dict | None:
        return {
            "queued_count_total": sum(self.queues.values()),
            "queued_count_below_priority": 0,
            "priority_counts": [],
        }

    async def _model_queue(self, provider: str, model: str, priority: int) -> dict | None:
        return {
            "queued_count_total": self.queues.get((provider, model), 0),
            "queued_count_below_priority": 0,
            "priority_counts": [],
        }

    async def _remote_probe(self, *args, **kwargs) -> dict | None:
        return None

    async def _ollama_probe(self, provider: str, model: str, **kwargs) -> bool:
        return self.probe_ok.get(provider, True)

    def _admission_for(self, requirements: dict, model: str, provider: str) -> dict | None:
        return dict(self.admissions.get((provider, model)) or self._admission("ready", loaded=True))

    def set_admission(self, provider: str, model: str, reason: str, *, loaded: bool) -> None:
        self.admissions[(provider, model)] = self._admission(reason, loaded=loaded)

    def route(self) -> dict:
        return {
            "requested_model": "smart1",
            "requested_alias": "smart1",
            "resolved_provider": "smart",
            "resolved_model": "smart1",
        }

    @staticmethod
    def payload(content: str = "hello") -> dict:
        return {
            "model": "smart1",
            "messages": [
                {"role": "system", "content": "rules"},
                {"role": "user", "content": content},
            ],
        }


class AffinityRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_request_selects_normal_candidate_and_seeds_affinity(self):
        harness = RouterHarness()
        resolution = await harness.router.resolve_route(harness.route(), request_payload=harness.payload())
        self.assertEqual((resolution.resolved_provider, resolution.resolved_model), ("p1", "m1"))
        self.assertEqual(resolution.route["selection_reason"], "immediate")
        self.assertTrue(resolution.route["kv_affinity"]["hit"] is False)

    async def test_affinity_waits_on_a_short_queue_instead_of_moving(self):
        harness = RouterHarness()
        await harness.router.resolve_route(harness.route(), request_payload=harness.payload())
        harness.set_admission("p1", "m1", "concurrency", loaded=True)
        harness.queues[("p1", "m1")] = 5
        resolution = await harness.router.resolve_route(harness.route(), request_payload=harness.payload())
        self.assertEqual((resolution.resolved_provider, resolution.resolved_model), ("p1", "m1"))
        self.assertEqual(resolution.route["selection_reason"], "affinity_wait_queue")

    async def test_queue_imbalance_diverts_and_moves_the_mapping(self):
        harness = RouterHarness()
        await harness.router.resolve_route(harness.route(), request_payload=harness.payload())
        harness.set_admission("p1", "m1", "concurrency", loaded=True)
        harness.queues[("p1", "m1")] = 10
        harness.queues[("p2", "m2")] = 0
        resolution = await harness.router.resolve_route(harness.route(), request_payload=harness.payload())
        self.assertEqual((resolution.resolved_provider, resolution.resolved_model), ("p2", "m2"))
        self.assertEqual(resolution.route["selection_reason"], "affinity_queue_diverted")
        # The next request follows the new mapping even if p1 is free again.
        harness.set_admission("p1", "m1", "ready", loaded=True)
        harness.queues[("p1", "m1")] = 0
        harness.set_admission("p2", "m2", "concurrency", loaded=True)
        harness.queues[("p2", "m2")] = 5
        resolution = await harness.router.resolve_route(harness.route(), request_payload=harness.payload())
        self.assertEqual((resolution.resolved_provider, resolution.resolved_model), ("p2", "m2"))

    async def test_warm_resource_block_waits_and_then_times_out(self):
        harness = RouterHarness()
        await harness.router.resolve_route(harness.route(), request_payload=harness.payload())
        harness.set_admission("p1", "m1", "session_memory", loaded=True)
        resolution = await harness.router.resolve_route(harness.route(), request_payload=harness.payload())
        self.assertEqual((resolution.resolved_provider, resolution.resolved_model), ("p1", "m1"))
        self.assertEqual(resolution.route["selection_reason"], "affinity_wait_resource")
        harness.now[0] = 61.0
        resolution = await harness.router.resolve_route(harness.route(), request_payload=harness.payload())
        self.assertEqual((resolution.resolved_provider, resolution.resolved_model), ("p2", "m2"))
        self.assertEqual(resolution.route["selection_reason"], "affinity_resource_timeout")

    async def test_cold_model_is_abandoned_immediately(self):
        harness = RouterHarness()
        await harness.router.resolve_route(harness.route(), request_payload=harness.payload())
        harness.set_admission("p1", "m1", "base_memory", loaded=False)
        resolution = await harness.router.resolve_route(harness.route(), request_payload=harness.payload())
        self.assertEqual((resolution.resolved_provider, resolution.resolved_model), ("p2", "m2"))
        self.assertEqual(resolution.route["selection_reason"], "affinity_resource_timeout")

    async def test_probe_failure_drops_affinity(self):
        harness = RouterHarness()
        await harness.router.resolve_route(harness.route(), request_payload=harness.payload())
        harness.probe_ok["p1"] = False
        resolution = await harness.router.resolve_route(harness.route(), request_payload=harness.payload())
        self.assertEqual((resolution.resolved_provider, resolution.resolved_model), ("p2", "m2"))

    async def test_prompt_cache_key_does_not_change_the_identity(self):
        harness = RouterHarness()
        first = harness.payload()
        first["prompt_cache_key"] = "session-a"
        await harness.router.resolve_route(harness.route(), request_payload=first)
        second = harness.payload()
        second["prompt_cache_key"] = "session-b"
        harness.set_admission("p1", "m1", "concurrency", loaded=True)
        harness.queues[("p1", "m1")] = 5
        resolution = await harness.router.resolve_route(harness.route(), request_payload=second)
        self.assertEqual((resolution.resolved_provider, resolution.resolved_model), ("p1", "m1"))
        self.assertEqual(resolution.route["selection_reason"], "affinity_wait_queue")

    async def test_request_alias_does_not_change_the_identity(self):
        harness = RouterHarness()
        await harness.router.resolve_route(harness.route(), request_payload=harness.payload())
        other_route = harness.route()
        other_route["requested_model"] = "other_alias"
        other_route["requested_alias"] = "other_alias"
        harness.set_admission("p1", "m1", "concurrency", loaded=True)
        harness.queues[("p1", "m1")] = 5
        resolution = await harness.router.resolve_route(other_route, request_payload=harness.payload())
        self.assertEqual((resolution.resolved_provider, resolution.resolved_model), ("p1", "m1"))
        self.assertEqual(resolution.route["selection_reason"], "affinity_wait_queue")


if __name__ == "__main__":
    unittest.main()


class _SchedulerQueue:
    def __init__(self) -> None:
        self.states = {
            '{"local":{"VRAM":19500}}': 0,
            '{"remote":{"VRAM":19500}}': 1,
        }

    async def get_resource_queue_state(self, requirements, priority=5):
        key = __import__("json").dumps(requirements or {}, sort_keys=True, separators=(",", ":"))
        return {
            "queued_count_total": self.states.get(key, 0),
            "queued_count_below_priority": 0,
            "priority_counts": [],
        }

    async def add_task(self, task, is_retry=False):
        self.added = task


class _SchedulerResources:
    def check_available(self, requirements):
        return True

    def check_available_after_unload(self, requirements):
        return True

    def check_available_for_reuse(self, requirements, model_id, provider_id):
        return False


class _SchedulerWorker:
    id = "openaix"
    task_type = "agent"
    enabled = True


class SchedulerAffinityTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_keeps_an_affined_candidate_over_an_immediate_alternative(self):
        store = KvAffinityStore(
            policy=AnchorPolicy(short_fraction=1.0),
        )
        tracker = ModelReadinessTracker()
        payload = {
            "model": "gemma-remote",
            "messages": [
                {"role": "system", "content": "rules"},
                {"role": "user", "content": "one"},
            ],
        }
        fingerprint = store.fingerprint(
            {"model": "smart_gemma-4-26B", "messages": payload["messages"]},
        )
        store.remember(fingerprint, provider="ollama_remote", model="gemma-remote", cache_group="gemma-remote")

        scheduler = Scheduler(
            queue=_SchedulerQueue(),
            workers={"openaix": _SchedulerWorker()},
            workers_cfg={"openaix": {"provider": "smart"}},
            resources=_SchedulerResources(),
            full_config={
                "models": {
                    "providers": {
                        "smart": {
                            "api": "smart",
                            "models": [
                                {
                                    "id": "smart_gemma-4-26B",
                                    "alias": "smart_gemma-4-26B",
                                    "type": "first_available",
                                    "items": [
                                        {"provider": "ollama_local", "model": "gemma-local", "fallback_prio": 10},
                                        {"provider": "ollama_remote", "model": "gemma-remote", "fallback_prio": 1},
                                    ],
                                }
                            ],
                        },
                        "ollama_local": {
                            "api": "ollama",
                            "baseUrl": "http://127.0.0.1:11434",
                            "models": [{"id": "gemma-local", "resources": {"local": {"VRAM": 19500}}}],
                        },
                        "ollama_remote": {
                            "api": "ollama",
                            "baseUrl": "http://192.168.1.110:11434",
                            "models": [{"id": "gemma-remote", "resources": {"remote": {"VRAM": 19500}}}],
                        },
                    }
                }
            },
            kv_affinity=store,
            model_readiness=tracker,
            affinity_config={"max_queue_diff_len": 8, "max_queue_diff_part": 3.0},
        )

        async def fake_probe(provider_id, model_id, *, timeout_ms, incoming_bearer_token=""):
            return True

        scheduler._probe_ollama_model_availability = fake_probe
        task = Task_agent(payload=payload, stream=False)
        task.priority = 5
        task.worker_id = "openaix"
        task.resource_requirements = {"remote": {"VRAM": 19500}}
        task.config = {
            "route": {
                "requested_model": "smart_gemma-4-26B",
                "requested_alias": "smart_gemma-4-26B",
                "requested_provider": "smart",
                "resolved_provider": "ollama_remote",
                "resolved_model": "gemma-remote",
                "selection": "smart_route",
                "strategy": "first_available",
                "selection_reason": "busy_fallback",
                "candidate_index": 1,
                "fallback_prio": 1,
                "candidate_probes": [],
                "resolved_worker": "openaix",
            }
        }

        await scheduler._refresh_smart_route_for_dispatch(task, "openaix")

        self.assertEqual(task.config["route"]["resolved_provider"], "ollama_remote")
        self.assertEqual(task.config["route"]["resolved_model"], "gemma-remote")
        self.assertEqual(task.config["route"]["selection_reason"], "affinity_wait_queue")


    async def test_execution_failure_forgets_the_failed_candidate_and_moves_the_prefix(self):
        store = KvAffinityStore(policy=AnchorPolicy(short_fraction=1.0))
        tracker = ModelReadinessTracker()
        messages = [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "one"},
        ]
        fingerprint = store.fingerprint(
            {"model": "smart_gemma-4-26B", "messages": messages},
        )
        store.remember(fingerprint, provider="ollama_remote", model="gemma-remote", cache_group="gemma-remote")

        queue = _SchedulerQueue()
        scheduler = Scheduler(
            queue=queue,
            workers={"openaix": _SchedulerWorker()},
            workers_cfg={"openaix": {"provider": "smart"}},
            resources=_SchedulerResources(),
            full_config={
                "models": {
                    "providers": {
                        "smart": {
                            "api": "smart",
                            "models": [
                                {
                                    "id": "smart_gemma-4-26B",
                                    "alias": "smart_gemma-4-26B",
                                    "type": "first_available",
                                    "items": [
                                        {"provider": "ollama_local", "model": "gemma-local", "fallback_prio": 10},
                                        {"provider": "ollama_remote", "model": "gemma-remote", "fallback_prio": 1},
                                    ],
                                }
                            ],
                        },
                        "ollama_local": {
                            "api": "ollama",
                            "baseUrl": "http://127.0.0.1:11434",
                            "models": [{"id": "gemma-local", "resources": {"local": {"VRAM": 19500}}}],
                        },
                        "ollama_remote": {
                            "api": "ollama",
                            "baseUrl": "http://192.168.1.110:11434",
                            "models": [{"id": "gemma-remote", "resources": {"remote": {"VRAM": 19500}}}],
                        },
                    }
                }
            },
            kv_affinity=store,
            model_readiness=tracker,
            affinity_config={"max_queue_diff_len": 8, "max_queue_diff_part": 3.0},
        )

        async def fake_probe(provider_id, model_id, *, timeout_ms, incoming_bearer_token=""):
            return True

        scheduler._probe_ollama_model_availability = fake_probe
        task = Task_agent(payload={"model": "gemma-remote", "messages": messages}, stream=False)
        task.priority = 5
        task.worker_id = "openaix"
        task.config = {
            "route": {
                "requested_model": "smart_gemma-4-26B",
                "requested_alias": "smart_gemma-4-26B",
                "requested_provider": "smart",
                "resolved_provider": "ollama_remote",
                "resolved_model": "gemma-remote",
                "selection": "smart_route",
                "strategy": "first_available",
                "selection_reason": "affinity",
                "candidate_index": 1,
                "fallback_prio": 1,
                "candidate_probes": [],
                "resolved_worker": "openaix",
            }
        }

        moved = await scheduler._fallback_smart_route_after_execution_failure(
            task, "openaix", {"code": "UPSTREAM_ERROR"}
        )
        self.assertTrue(moved)
        self.assertEqual(task.config["route"]["resolved_provider"], "ollama_local")
        record = store.lookup(fingerprint)
        self.assertIsNotNone(record)
        self.assertEqual(record["provider"], "ollama_local")

        moved = await scheduler._fallback_smart_route_after_execution_failure(
            task, "openaix", {"code": "LLAMA_CPP_START_FAILED"}
        )
        self.assertTrue(moved)
        self.assertEqual(task.config["route"]["resolved_provider"], "ollama_remote")
