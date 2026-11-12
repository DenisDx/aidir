"""Tests of the usage recording at the LLM-call finalization."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace

from core.task import Task
from core.worker import BaseWorker
from core.usage_store import UsageStore


class _Config:
    """Provide the configuration the worker reads."""

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
    """Provide the core state the worker reads at the finalization."""

    def __init__(self, store: UsageStore, config: dict) -> None:
        self.usage_store = store
        self.config = _Config(config)
        self.audit_log = None
        self.envid_registry = None
        self.queue = None


class _Worker(BaseWorker):
    """Expose the call bookkeeping of the base worker."""

    id = "call_openai"
    task_type = "agent"


class TestUsageRecording(unittest.TestCase):
    """Verify that a finalized call reaches the usage store."""

    def setUp(self) -> None:
        """Open a store and a worker bound to it."""
        self.directory = tempfile.mkdtemp(prefix="aidir-usage-worker-")
        self.store = UsageStore(os.path.join(self.directory, "usage.sqlite"))
        self.store.open()
        self.config = {
            "models": {
                "providers": {
                    "p1": {
                        "api": "openai",
                        "models": [
                            {
                                "id": "m1",
                                "alias": "alias-1",
                                "billing": {
                                    "prices": {
                                        "prefill_per_million": 2.0,
                                        "cached_prefill_per_million": 0.5,
                                        "generation_per_million": 4.0,
                                    }
                                },
                            }
                        ],
                    }
                }
            }
        }
        self.worker = _Worker()
        self.worker._core = _Core(self.store, self.config)

    def tearDown(self) -> None:
        """Close the store and remove the directory."""
        self.store.close()
        for name in os.listdir(self.directory):
            os.unlink(os.path.join(self.directory, name))
        os.rmdir(self.directory)

    def _finalize(self, task: Task, entry: dict, **kwargs) -> None:
        """Finalize one call entry of a task."""
        asyncio.run(self.worker._finalize_llm_call(task, entry, **kwargs))

    def test_a_successful_call_is_recorded_with_its_cost(self) -> None:
        """The call reaches the store with its tokens and its computed cost."""
        task = Task(payload={"model": "m1"}, config={"request_kind": "chat"})
        entry = {
            "call_index": 1,
            "started_at": "2026-11-12T10:00:00.000+00:00",
            "provider_id": "p1",
            "model": "m1",
            "request_kind": "chat",
            "stream": False,
        }
        self._finalize(
            task,
            entry,
            status="ok",
            http_status=200,
            response={
                "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 500_000},
                "message": {"content": "hello"},
                "done_reason": "stop",
            },
        )
        rows = self.store._connection.execute(
            "SELECT calls, prompt_tokens, completion_tokens, computed_cost, model_alias FROM usage_hourly"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["calls"], 1)
        self.assertEqual(rows[0]["prompt_tokens"], 1_000_000)
        self.assertEqual(rows[0]["completion_tokens"], 500_000)
        self.assertEqual(rows[0]["computed_cost"], 1_000_000 * 2 + 500_000 * 4)
        self.assertEqual(rows[0]["model_alias"], "alias-1")

    def test_a_failed_call_is_recorded_without_tokens(self) -> None:
        """A failed call adds its class and no tokens."""
        task = Task(payload={"model": "m1"}, config={"request_kind": "chat"})
        entry = {
            "call_index": 1,
            "started_at": "2026-11-12T10:00:00.000+00:00",
            "provider_id": "p1",
            "model": "m1",
            "request_kind": "chat",
            "stream": False,
        }
        self._finalize(
            task,
            entry,
            status="http_error",
            http_status=500,
            error_code="UPSTREAM_ERROR",
            response_body=b'{"error":{"message":"boom"}}',
        )
        row = self.store._connection.execute(
            "SELECT calls, failures, prompt_tokens, http_5xx FROM usage_hourly"
        ).fetchone()
        self.assertEqual(row["calls"], 1)
        self.assertEqual(row["failures"], 1)
        self.assertEqual(row["prompt_tokens"], 0)
        self.assertEqual(row["http_5xx"], 1)

    def test_a_call_through_a_smart_alias_is_recorded_twice(self) -> None:
        """The alias and the concrete model both carry the call."""
        task = Task(
            payload={"model": "fast"},
            config={
                "request_kind": "chat",
                "route": {
                    "selection": "smart_route",
                    "requested_provider": "smart",
                    "requested_alias": "fast",
                    "resolved_provider": "p1",
                    "resolved_model": "m1",
                    "resolved_protocol": "chat-completions",
                },
            },
        )
        entry = {
            "call_index": 1,
            "started_at": "2026-11-12T10:00:00.000+00:00",
            "provider_id": "p1",
            "model": "m1",
            "request_kind": "chat",
            "stream": False,
        }
        self._finalize(
            task,
            entry,
            status="ok",
            http_status=200,
            response={
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                "message": {"content": "hello"},
                "done_reason": "stop",
            },
        )
        rows = self.store._connection.execute(
            "SELECT provider_id, model_id, model_kind, calls FROM usage_hourly"
        ).fetchall()
        kinds = {(row["provider_id"], row["model_id"], row["model_kind"]) for row in rows}
        self.assertIn(("p1", "m1", "concrete"), kinds)
        self.assertIn(("smart", "fast", "smart"), kinds)

    def test_the_envid_is_only_kept_when_it_is_known(self) -> None:
        """An unknown envid is not stored as an envid."""
        task = Task(payload={"model": "m1", "envid": "made-up"}, config={"request_kind": "chat"})
        entry = {
            "call_index": 1,
            "started_at": "2026-11-12T10:00:00.000+00:00",
            "provider_id": "p1",
            "model": "m1",
            "request_kind": "chat",
            "stream": False,
        }
        self._finalize(
            task,
            entry,
            status="ok",
            http_status=200,
            response={"usage": {"prompt_tokens": 10, "completion_tokens": 2}, "message": {"content": "x"}},
        )
        rows = self.store._connection.execute("SELECT envid FROM usage_envid").fetchall()
        self.assertEqual([row["envid"] for row in rows], [""])

    def test_a_known_envid_is_stored(self) -> None:
        """A registered envid reaches its own row."""
        registry = SimpleNamespace(get=lambda envid: SimpleNamespace(id=envid) if envid == "test-12345" else None)
        self.worker._core.envid_registry = registry
        task = Task(payload={"model": "m1", "envid": "test-12345"}, config={"request_kind": "chat"})
        entry = {
            "call_index": 1,
            "started_at": "2026-11-12T10:00:00.000+00:00",
            "provider_id": "p1",
            "model": "m1",
            "request_kind": "chat",
            "stream": False,
        }
        self._finalize(
            task,
            entry,
            status="ok",
            http_status=200,
            response={"usage": {"prompt_tokens": 10, "completion_tokens": 2}, "message": {"content": "x"}},
        )
        rows = self.store._connection.execute("SELECT envid FROM usage_envid").fetchall()
        self.assertEqual([row["envid"] for row in rows], ["test-12345"])

    def test_a_call_without_a_store_changes_nothing(self) -> None:
        """The worker works without a usage store."""
        self.worker._core.usage_store = None
        task = Task(payload={"model": "m1"}, config={"request_kind": "chat"})
        entry = {
            "call_index": 1,
            "started_at": "2026-11-12T10:00:00.000+00:00",
            "provider_id": "p1",
            "model": "m1",
            "request_kind": "chat",
            "stream": False,
        }
        self._finalize(task, entry, status="ok", http_status=200, response={"usage": {"prompt_tokens": 1}})
        self.assertEqual(self.store.debug_dump()["hourly"], 0)

    def test_a_streamed_call_keeps_the_counters_of_its_chunk(self) -> None:
        """A streamed answer is metered with the counters of its usage chunk."""
        task = Task(payload={"model": "m1"}, config={"request_kind": "chat"})
        entry = {
            "call_index": 1,
            "started_at": "2026-11-12T10:00:00.000+00:00",
            "provider_id": "p1",
            "model": "m1",
            "request_kind": "chat",
            "stream": True,
        }
        self._finalize(
            task,
            entry,
            status="ok",
            http_status=200,
            response={
                "final": {"choices": [{"delta": {}}], "done_reason": "stop"},
                "usage_chunk": {
                    "usage": {
                        "prompt_tokens": 104454,
                        "completion_tokens": 201,
                        "prompt_tokens_details": {"cached_tokens": 103138},
                    },
                    "timings": {"prompt_ms": 6050.7, "predicted_ms": 2005.3},
                },
            },
        )
        row = self.store._connection.execute(
            "SELECT prompt_tokens, cached_prompt_tokens, completion_tokens, prefill_ms_sum, generation_ms_sum"
            " FROM usage_hourly"
        ).fetchone()
        self.assertEqual(row["prompt_tokens"], 104454)
        self.assertEqual(row["cached_prompt_tokens"], 103138)
        self.assertEqual(row["completion_tokens"], 201)
        self.assertEqual(row["prefill_ms_sum"], 6050)
        self.assertEqual(row["generation_ms_sum"], 2005)


if __name__ == "__main__":
    unittest.main()
