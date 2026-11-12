"""Tests of the usage API, the usage block of the model list, and the monitoring routes."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.endpoints.endpoint_openaix import Endpoint_openaix
from core.resources import Resources
from core.usage_api import mount_usage_api, resolve_period
from core.usage_store import UsageStore


def _event(**overrides) -> dict:
    """Return a usage event of one finished call."""
    event = {
        "usage_event_id": "task-1:1",
        "provider_id": "p1",
        "model_id": "m1",
        "model_alias": "alias-1",
        "alias_provider_id": "",
        "alias_model_id": "",
        "protocol": "chat-completions",
        "request_kind": "chat",
        "status": "ok",
        "status_group": "success",
        "scope": "external",
        "client_request": True,
        "total_ms": 1000,
        "ttft_ms": 200,
        "prefill_ms": 300,
        "generation_ms": 500,
        "prompt_tokens": 1000,
        "cached_prompt_tokens": 250,
        "completion_tokens": 200,
        "reasoning_tokens": 50,
        "answer_tokens": 150,
        "tool_calls": 2,
        "reported_cost_micros": 400,
        "computed_cost_micros": 380,
        "allowance_micros": 250,
        "list_cost_micros": 400,
        "effective_money_micros": 250,
        "cache_savings_micros": 30,
        "envid": "",
    }
    event.update(overrides)
    return event


class _Config:
    """Provide dotted configuration lookup for the endpoint tests."""

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

    def raw(self) -> dict:
        """Return the whole configuration."""
        return self._values


class _Core:
    """Provide the state read by the usage routes and the monitoring routes."""

    def __init__(self, store: UsageStore | None = None) -> None:
        self.config = _Config(
            {
                "instance": "test-aidir",
                "models": {
                    "providers": {
                        "p1": {
                            "api": "openai",
                            "models": [{"id": "m1", "alias": "alias-1"}],
                        }
                    }
                },
            }
        )
        self.queue = SimpleNamespace(list_tasks=lambda: [])
        self.scheduler = SimpleNamespace(active_task_count=lambda: 0)
        self.workers = {}
        self.resources = Resources([])
        self.started_at = 1000.0
        self.usage_store = store
        self.envid_registry = None


class UsageApiTestCase(unittest.TestCase):
    """Provide a usage store with one recorded call."""

    def setUp(self) -> None:
        """Open the store and record one call."""
        self.directory = tempfile.mkdtemp(prefix="aidir-usage-api-")
        self.store = UsageStore(os.path.join(self.directory, "usage.sqlite"))
        self.store.open()
        self.store.record_call(_event())

    def tearDown(self) -> None:
        """Close the store and remove the directory."""
        self.store.close()
        for name in os.listdir(self.directory):
            os.unlink(os.path.join(self.directory, name))
        os.rmdir(self.directory)

    def _client(self) -> TestClient:
        """Return a client of the usage API."""
        app = FastAPI()
        core = _Core(self.store)
        mount_usage_api(app, core, require_session=lambda: {"login": "tester"})
        return TestClient(app)


class TestUsageRoutes(UsageApiTestCase):
    """Verify the usage routes of the WebUI API."""

    def test_the_summary_reports_the_totals(self) -> None:
        """The summary answers with the counters of the period."""
        with self._client() as client:
            response = client.get("/api/usage/summary?period=24h")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["available"])
        self.assertEqual(payload["calls"], 1)
        self.assertEqual(payload["tokens"]["prompt"], 1000)
        self.assertEqual(payload["tokens"]["cached_prompt"], 250)
        self.assertEqual(payload["cache"]["hit_rate"], 0.25)
        self.assertEqual(payload["cost"]["computed"], 380)
        self.assertEqual(len(payload["models"]), 1)

    def test_the_default_period_is_the_last_day(self) -> None:
        """The period bar opens on the last 24 hours."""
        with self._client() as client:
            response = client.get("/api/usage/summary")
        payload = response.json()
        self.assertEqual(payload["period"]["label"], "24h")

    def test_the_scope_selects_the_call_population(self) -> None:
        """The scope selects the external or the internal calls."""
        self.store.record_call(
            _event(usage_event_id="task-1:2", scope="internal", client_request=False)
        )
        with self._client() as client:
            self.assertEqual(client.get("/api/usage/summary?scope=external").json()["calls"], 1)
            self.assertEqual(client.get("/api/usage/summary?scope=internal").json()["calls"], 1)
            self.assertEqual(client.get("/api/usage/summary?scope=all").json()["calls"], 2)

    def test_the_models_route_returns_one_row_per_model(self) -> None:
        """The model rows carry the counters of the model."""
        with self._client() as client:
            response = client.get("/api/usage/models?sort=cost")
        payload = response.json()
        self.assertEqual(len(payload["models"]), 1)
        row = payload["models"][0]
        self.assertEqual(row["model"], "m1")
        self.assertEqual(row["calls"], 1)
        self.assertEqual(row["computed_cost"], 380)

    def test_the_series_route_returns_the_buckets(self) -> None:
        """The series answers with the non-empty buckets."""
        with self._client() as client:
            response = client.get("/api/usage/series?interval=hour")
        payload = response.json()
        self.assertEqual(len(payload["buckets"]), 1)
        self.assertEqual(payload["buckets"][0]["calls"], 1)

    def test_the_speeds_route_returns_the_context_buckets(self) -> None:
        """The speed profiles are grouped by the context bucket."""
        with self._client() as client:
            response = client.get("/api/usage/speeds")
        payload = response.json()
        self.assertEqual(len(payload["speeds"]), 1)
        self.assertEqual(payload["speeds"][0]["context_bucket"], "0-10k")
        self.assertEqual(payload["speeds"][0]["generation_tokens_per_second"], 400.0)

    def test_the_allowance_route_reports_the_provider_allowance(self) -> None:
        """The allowance of the provider is reported apart from the cost."""
        with self._client() as client:
            response = client.get("/api/usage/allowance")
        payload = response.json()
        self.assertEqual(payload["allowance"][0]["provider"], "p1")
        self.assertEqual(payload["allowance"][0]["allowance"], 250)

    def test_the_health_route_reports_the_store(self) -> None:
        """The health route answers with the state of the store."""
        with self._client() as client:
            response = client.get("/api/usage/health")
        payload = response.json()
        self.assertTrue(payload["healthy"])
        self.assertEqual(payload["failed_writes"], 0)

    def test_the_limits_route_and_the_reset(self) -> None:
        """A rule can be reset, and the reset clears its counters."""
        from core.billing import parse_limit_rules

        self.store.set_rules(
            parse_limit_rules(
                [{"id": "r1", "period": "5h", "mode": "rolling", "max_generation_tokens": 10, "action": "block"}],
                scope_type="model",
                scope_id="p1:m1",
            )
        )
        self.store.record_call(_event(usage_event_id="task-1:2", completion_tokens=200))
        with self._client() as client:
            limits = client.get("/api/usage/limits").json()
            self.assertEqual(limits["limits"][0]["id"], "r1")
            self.assertTrue(limits["limits"][0]["blocked"])
            reset = client.post("/api/usage/limits/reset", json={"rule_id": "r1", "counter": "all"})
            self.assertTrue(reset.json()["reset"])
            limits = client.get("/api/usage/limits").json()
            self.assertFalse(limits["limits"][0]["blocked"])

    def test_a_reset_without_a_rule_id_is_refused(self) -> None:
        """The reset route needs the rule id."""
        with self._client() as client:
            response = client.post("/api/usage/limits/reset", json={"counter": "all"})
        self.assertEqual(response.status_code, 400)


class TestUnavailableStore(unittest.TestCase):
    """Verify the behaviour when the store is not available."""

    def test_the_routes_answer_without_data(self) -> None:
        """A missing store never turns into an error of the page."""
        app = FastAPI()
        mount_usage_api(app, _Core(None), require_session=lambda: {"login": "tester"})
        with TestClient(app) as client:
            response = client.get("/api/usage/summary")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["available"])


class TestPeriodResolution(unittest.TestCase):
    """Verify the resolution of the period presets."""

    def test_the_presets(self) -> None:
        """Every preset resolves to its own window."""
        now = datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)
        start, end, label = resolve_period("today", tz=timezone.utc, now=now)
        self.assertEqual(label, "today")
        self.assertEqual(start, datetime(2026, 11, 12, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 11, 13, 0, 0, tzinfo=timezone.utc))

        start, end, label = resolve_period("24h", tz=timezone.utc, now=now)
        self.assertEqual(label, "24h")
        self.assertEqual(end - start, timedelta(hours=24))

        start, end, label = resolve_period("month", tz=timezone.utc, now=now)
        self.assertEqual(start, datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc))

    def test_the_explicit_timestamps_win(self) -> None:
        """An explicit range overrides the preset."""
        start, end, label = resolve_period(
            "today",
            "2026-01-01T00:00:00+00:00",
            "2026-01-02T00:00:00+00:00",
            tz=timezone.utc,
        )
        self.assertEqual(label, "custom")
        self.assertEqual(end - start, timedelta(days=1))


class TestMonitoringRoutes(UsageApiTestCase):
    """Verify the usage part of the stats and metrics routes."""

    def _client(self) -> TestClient:
        """Return a client of the OpenAIx endpoint."""
        endpoint = Endpoint_openaix({"id": "openaix"})
        return TestClient(endpoint.create_app(_Core(self.store)))

    def test_the_stats_route_carries_the_usage(self) -> None:
        """The stats snapshot carries the usage of the last day."""
        with self._client() as client:
            response = client.get("/stats")
        payload = response.json()
        self.assertEqual(payload["usage"]["calls"], 1)
        self.assertEqual(payload["usage"]["prompt_tokens"], 1000)

    def test_the_metrics_route_carries_the_model_counters(self) -> None:
        """The metrics expose the per-model counters."""
        with self._client() as client:
            response = client.get("/metrics")
        body = response.text
        self.assertIn('aidir_model_calls_total{provider="p1",model="m1",status="success"} 1\n', body)
        self.assertIn('aidir_model_tokens_total{provider="p1",model="m1",kind="prompt"} 1000\n', body)
        self.assertIn('aidir_model_cost_usd{provider="p1",model="m1",kind="computed"} 380\n', body)
        self.assertIn('aidir_model_cache_hit_ratio{provider="p1",model="m1"} 0.25\n', body)

    def test_the_model_list_carries_the_usage_block(self) -> None:
        """The model list answers with a usage block per model."""
        with self._client() as client:
            response = client.get("/v1/models")
        payload = response.json()
        self.assertEqual(len(payload["data"]), 1)
        usage = payload["data"][0]["aidir"].get("usage")
        self.assertIsNotNone(usage)
        self.assertEqual(usage["calls"], 1)
        self.assertEqual(usage["tokens"]["prompt"], 1000)

    def test_an_unauthenticated_caller_never_sees_the_cost(self) -> None:
        """The cost and the allowance need an endpoint user token."""
        with self._client() as client:
            response = client.get("/v1/models")
        usage = response.json()["data"][0]["aidir"]["usage"]
        self.assertNotIn("cost", usage)
        self.assertEqual(usage["speed"]["ttft_ms_avg"], 200)


class TestBudgetRefusal(UsageApiTestCase):
    """Verify the refusal of a model that exceeded its budget."""

    def _endpoint_client(self) -> TestClient:
        """Return a client of the OpenAIx endpoint."""
        endpoint = Endpoint_openaix({"id": "openaix"})
        return TestClient(endpoint.create_app(_Core(self.store)))

    def test_a_blocked_model_is_refused_with_429(self) -> None:
        """A direct request to a blocked model is refused with the budget error."""
        from core.billing import parse_limit_rules

        self.store.set_rules(
            parse_limit_rules(
                [{"id": "r1", "period": "5h", "mode": "rolling", "max_generation_tokens": 10, "action": "block"}],
                scope_type="model",
                scope_id="p1:m1",
            )
        )
        self.store.record_call(_event(usage_event_id="task-1:2", completion_tokens=200))
        with self._endpoint_client() as client:
            response = client.post(
                "/v1/chat/completions",
                json={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertEqual(response.status_code, 429)
        payload = response.json()
        self.assertEqual(payload["error"]["code"], "BUDGET_EXCEEDED")
        self.assertEqual(payload["error"]["type"], "insufficient_quota")

    def test_a_model_without_a_limit_is_served(self) -> None:
        """A model without an exceeded limit is not refused."""
        with self._endpoint_client() as client:
            response = client.post(
                "/v1/chat/completions",
                json={"model": "m1", "messages": [{"role": "user", "content": "hi"}]},
            )
        self.assertNotEqual(response.status_code, 429)


class TestBudgetAwareRouting(unittest.IsolatedAsyncioTestCase):
    """Verify that an over-budget candidate leaves the routing."""

    def _router(self, blocked: set[tuple[str, str]]) -> "object":
        """Build a router that blocks the given candidates."""
        from core.smart_router import SmartRouter

        def model_cfg(provider: str, model: str) -> dict | None:
            if provider == "smart" and model == "fast":
                return {"id": "fast", "type": "first_available", "items": [{"provider": "p1", "model": "m1"}]}
            if provider == "p1":
                return {"id": model}
            return None

        async def queue(*args, **kwargs):
            return {"queued_count_total": 0, "queued_count_below_priority": 0}

        async def remote(*args, **kwargs):
            return None

        async def probe(*args, **kwargs):
            return True

        return SmartRouter(
            endpoint_id="test",
            default_worker_id="worker",
            find_provider_model_cfg=model_cfg,
            provider_api=lambda provider: "ollama",
            resolve_model_resource_requirements=lambda provider, model: {"gpu": {"VRAM": 1000}},
            get_local_queue_state=queue,
            check_resource_available=lambda requirements: True,
            check_resource_available_after_unload=lambda requirements: True,
            check_resource_available_for_reuse=lambda requirements, model, provider: True,
            probe_remote_model_queue_state=remote,
            probe_ollama_model_availability=probe,
            resolve_probe_timeout_ms=lambda item: 100,
            resolve_worker_id_for_route=lambda worker, route: worker,
            get_model_queue_state=queue,
            evaluate_model_admission=lambda requirements, model, provider: {
                "admission": "ready",
                "admissible_now": True,
                "admissible_after_unload": True,
                "model_loaded": True,
                "active_count": 0,
                "max_concurrent_requests": 1,
                "free": {"gpu": {"VRAM": 1000}},
                "needed": {"gpu": {"VRAM": 1000}},
                "resource_ready": True,
                "resource_ready_after_unload": True,
            },
            is_candidate_budget_blocked=lambda provider, model: (provider, model) in blocked,
        )

    async def test_an_over_budget_candidate_is_not_selected(self) -> None:
        """The routing refuses the alias when every candidate is over budget."""
        from core.smart_router import SmartRouteError

        router = self._router({("p1", "m1")})
        with self.assertRaises(SmartRouteError) as caught:
            await router.resolve_route(
                {"requested_model": "fast", "resolved_provider": "smart", "resolved_model": "fast"},
                request_payload={"messages": [{"role": "user", "content": "hello"}]},
            )
        self.assertEqual(caught.exception.code, "BUDGET_EXCEEDED")
        self.assertEqual(caught.exception.status_code, 429)

    async def test_a_candidate_with_budget_is_selected(self) -> None:
        """A candidate inside its budget is still selected."""
        router = self._router(set())
        resolution = await router.resolve_route(
            {"requested_model": "fast", "resolved_provider": "smart", "resolved_model": "fast"},
            request_payload={"messages": [{"role": "user", "content": "hello"}]},
        )
        self.assertEqual(resolution.resolved_provider, "p1")
        self.assertEqual(resolution.resolved_model, "m1")


if __name__ == "__main__":
    unittest.main()
