"""Regression tests for a remote aidir that refuses to run the resolved model.

A remote that cannot run the model is a property of its capacity, not of the
task. The task must be re-routed to another candidate instead of waiting for
its whole queue timeout, and it must fail with a readable reason when no
candidate is left.
"""
from __future__ import annotations

import asyncio
import unittest

from core.kv_affinity import AnchorPolicy, KvAffinityStore
from core.scheduler import Scheduler, _MAX_REMOTE_REFUSALS
from core.task import STATUS_QUEUED, STATUS_RUNNING
from core.task_types.task_agent import Task_agent
from core.worker import WorkerResult


class _Queue:
    """Queue double that records the deferred and the failed tasks."""

    def __init__(self) -> None:
        """Start with an empty bookkeeping."""
        self.deferred: list[Task_agent] = []
        self.failed: list[tuple[str, dict]] = []
        self.added: list[Task_agent] = []

    async def add_task(self, task, is_retry=False) -> None:
        """Record a task that is returned to the queue."""
        self.added.append(task)

    async def mark_failed(self, task, error: dict) -> None:
        """Record a task that failed with a reason."""
        self.failed.append((task.id, error))

    async def mark_running(self, task_id: str, worker_id: str) -> None:
        """Accept the running transition."""

    async def mark_completed(self, task) -> None:
        """Accept a completed task."""

    async def mark_canceled(self, task) -> None:
        """Accept a canceled task."""

    async def get_resource_queue_state(self, requirements, priority=5):
        """Report an empty queue."""
        return {"queued_count_total": 0, "queued_count_below_priority": 0, "priority_counts": []}


class _Worker:
    """Worker double for the scheduler dispatch path."""

    id = "openaix"
    task_type = "agent"
    enabled = True


class _Resources:
    """Resources double that reports the local machine as free."""

    def check_available(self, requirements):
        """Report the local capacity as available."""
        return True

    def check_available_after_unload(self, requirements):
        """Report the local capacity as available after an unload."""
        return True

    def check_available_for_reuse(self, requirements, model_id, provider_id):
        """Report no warm model to reuse."""
        return False


def _config() -> dict:
    """A smart model with a local candidate and a remote aidir candidate."""
    return {
        "models": {
            "providers": {
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
                    "models": [{"id": "m-remote"}],
                },
            }
        }
    }


def _task() -> Task_agent:
    """A task routed to the remote candidate."""
    task = Task_agent(payload={"model": "m-remote", "messages": []})
    task.priority = 5
    task.worker_id = "openaix"
    task.config = {
        "route": {
            "requested_model": "local-only",
            "requested_alias": "local-only",
            "requested_provider": "smart",
            "resolved_provider": "remote_aidir_110",
            "resolved_model": "m-remote",
            "selection": "smart_route",
            "strategy": "first_available",
            "selection_reason": "affinity",
            "candidate_index": 1,
            "fallback_prio": 1,
            "candidate_probes": [],
            "resolved_worker": "openaix",
        }
    }
    return task


def _scheduler(queue: _Queue) -> Scheduler:
    """A scheduler with the smart provider configured."""
    return Scheduler(
        queue=queue,
        workers={"openaix": _Worker()},
        workers_cfg={"openaix": {"provider": "smart"}},
        resources=_Resources(),
        full_config=_config(),
    )


class RemoteRefusalTests(unittest.IsolatedAsyncioTestCase):
    """A refusing remote must not park the task in the queue forever."""

    async def test_the_refusal_reason_names_the_remote_capacity(self):
        """The remote's answer is turned into a reason a human can read."""
        scheduler = _scheduler(_Queue())
        task = _task()

        async def fake_probe(provider_id, model_id, *, priority, timeout_ms, incoming_bearer_token=""):
            return {
                "can_run_now": False,
                "admission": "base_memory",
                "free": {"local_machine": {"VRAM": 13795.0}},
                "needed": {"local_machine": {"VRAM": 21500}},
            }

        scheduler._probe_remote_model_queue_state = fake_probe
        ok, kind, reason = await scheduler._remote_route_status(task, "remote_aidir_110", "m-remote")

        self.assertFalse(ok)
        self.assertEqual(kind, "capacity")
        self.assertIn("base_memory", reason)
        self.assertIn("13795", reason)
        self.assertIn("21500", reason)

    async def test_an_unreachable_remote_is_told_apart_from_a_capacity_refusal(self):
        """A remote that does not answer is a transient condition, not a capacity fact."""
        scheduler = _scheduler(_Queue())
        task = _task()

        async def fake_probe(provider_id, model_id, *, priority, timeout_ms, incoming_bearer_token=""):
            return None

        scheduler._probe_remote_model_queue_state = fake_probe
        ok, kind, reason = await scheduler._remote_route_status(task, "remote_aidir_110", "m-remote")

        self.assertFalse(ok)
        self.assertEqual(kind, "unreachable")
        self.assertIn("did not answer", reason)

    async def test_a_refusing_remote_is_excluded_and_the_route_moves(self):
        """The task is re-routed to the local candidate instead of being deferred."""
        queue = _Queue()
        scheduler = _scheduler(queue)
        task = _task()

        async def fake_remote_probe(provider_id, model_id, *, priority, timeout_ms, incoming_bearer_token=""):
            return {"can_run_now": False, "admission": "base_memory"}

        async def fake_ollama_probe(provider_id, model_id, *, timeout_ms, incoming_bearer_token=""):
            return True

        scheduler._probe_remote_model_queue_state = fake_remote_probe
        scheduler._probe_ollama_model_availability = fake_ollama_probe

        deferred: list[Task_agent] = []
        await scheduler._handle_remote_refusal(
            task,
            _Worker(),
            "remote_aidir_110",
            "m-remote",
            "capacity",
            "the remote reports admission=base_memory",
            deferred,
        )

        route = task.config["route"]
        self.assertEqual(route["resolved_provider"], "llama_local")
        self.assertEqual(route["resolved_model"], "m-local")
        self.assertEqual(task.payload["model"], "m-local")
        # The task was popped from the queue, so it has to go back in with the
        # new route; otherwise the re-route is never dispatched.
        self.assertEqual([item.id for item in deferred], [task.id])
        self.assertEqual(task.next_retry_at, 0.0)

    async def test_a_task_with_no_candidate_left_fails_with_a_readable_reason(self):
        """The task fails instead of waiting for its whole queue timeout."""
        queue = _Queue()
        scheduler = _scheduler(queue)
        task = _task()

        async def fake_remote_probe(provider_id, model_id, *, priority, timeout_ms, incoming_bearer_token=""):
            return {"can_run_now": False, "admission": "base_memory"}

        async def fake_ollama_probe(provider_id, model_id, *, timeout_ms, incoming_bearer_token=""):
            return False

        scheduler._probe_remote_model_queue_state = fake_remote_probe
        scheduler._probe_ollama_model_availability = fake_ollama_probe

        deferred: list[Task_agent] = []
        for _ in range(_MAX_REMOTE_REFUSALS):
            await scheduler._handle_remote_refusal(
                task,
                _Worker(),
                "remote_aidir_110",
                "m-remote",
                "capacity",
                "the remote reports admission=base_memory",
                deferred,
            )

        self.assertEqual(len(queue.failed), 1)
        task_id, error = queue.failed[0]
        self.assertEqual(task_id, task.id)
        self.assertEqual(error["code"], "REMOTE_CANNOT_RUN")
        self.assertIn("remote_aidir_110", error["message"])
        self.assertIn("m-remote", error["message"])

    async def test_a_refusal_before_the_limit_defers_the_task_once(self):
        """Before the limit the task waits, but only for a bounded number of rounds."""
        queue = _Queue()
        scheduler = _scheduler(queue)
        task = _task()

        async def fake_remote_probe(provider_id, model_id, *, priority, timeout_ms, incoming_bearer_token=""):
            return {"can_run_now": False, "admission": "base_memory"}

        async def fake_ollama_probe(provider_id, model_id, *, timeout_ms, incoming_bearer_token=""):
            return False

        scheduler._probe_remote_model_queue_state = fake_remote_probe
        scheduler._probe_ollama_model_availability = fake_ollama_probe

        deferred: list[Task_agent] = []
        await scheduler._handle_remote_refusal(
            task,
            _Worker(),
            "remote_aidir_110",
            "m-remote",
            "capacity",
            "the remote reports admission=base_memory",
            deferred,
        )

        self.assertEqual([item.id for item in deferred], [task.id])
        self.assertGreater(task.next_retry_at, 0)
        self.assertEqual(queue.failed, [])

    async def test_an_unreachable_remote_never_fails_the_task(self):
        """A remote that is down is transient: the task waits for it, it is not failed.

        The task must not die because the remote is restarting. It waits, and
        every attempt probes the remote again, so it goes back to it as soon as
        it answers. The queue timeout is the bound.
        """
        queue = _Queue()
        scheduler = _scheduler(queue)
        task = _task()

        async def fake_remote_probe(provider_id, model_id, *, priority, timeout_ms, incoming_bearer_token=""):
            return None

        async def fake_ollama_probe(provider_id, model_id, *, timeout_ms, incoming_bearer_token=""):
            return False

        scheduler._probe_remote_model_queue_state = fake_remote_probe
        scheduler._probe_ollama_model_availability = fake_ollama_probe

        deferred: list[Task_agent] = []
        for _ in range(_MAX_REMOTE_REFUSALS * 3):
            await scheduler._handle_remote_refusal(
                task,
                _Worker(),
                "remote_aidir_110",
                "m-remote",
                "unreachable",
                "the remote aidir did not answer the queue-state probe",
                deferred,
            )

        self.assertEqual(queue.failed, [])
        self.assertEqual({item.id for item in deferred}, {task.id})
        # The candidate was not blocked for good, so the task goes back to it.
        self.assertNotIn(
            ("remote_aidir_110", "m-remote"),
            scheduler._remote_blocked_candidates.get(task.id, set()),
        )

    async def test_an_unreachable_remote_is_skipped_for_one_attempt_only(self):
        """The task is re-routed to a candidate that can take it, and it is dispatched."""
        queue = _Queue()
        scheduler = _scheduler(queue)
        task = _task()

        async def fake_remote_probe(provider_id, model_id, *, priority, timeout_ms, incoming_bearer_token=""):
            return None

        async def fake_ollama_probe(provider_id, model_id, *, timeout_ms, incoming_bearer_token=""):
            return True

        scheduler._probe_remote_model_queue_state = fake_remote_probe
        scheduler._probe_ollama_model_availability = fake_ollama_probe

        deferred: list[Task_agent] = []
        await scheduler._handle_remote_refusal(
            task,
            _Worker(),
            "remote_aidir_110",
            "m-remote",
            "unreachable",
            "the remote aidir did not answer the queue-state probe",
            deferred,
        )

        self.assertEqual(task.config["route"]["resolved_provider"], "llama_local")
        self.assertEqual([item.id for item in deferred], [task.id])
        self.assertEqual(queue.failed, [])



class _DispatchQueue:
    """Queue double that runs the real scheduler loop."""

    def __init__(self, tasks: list[Task_agent]) -> None:
        """Store the tasks in their configured priority order."""
        self._tasks = {task.id: task for task in tasks}
        self._queued = list(tasks)
        self.completed: list[str] = []
        self.failed: list[tuple[str, dict]] = []

    async def pop_next(self, task_type: str) -> str | None:
        """Remove and return the next task of the requested type."""
        for index, task in enumerate(self._queued):
            if task.type == task_type:
                return self._queued.pop(index).id
        return None

    def get_task(self, task_id: str) -> Task_agent | None:
        """Return a queued task by identifier."""
        return self._tasks.get(task_id)

    async def add_task(self, task, is_retry=False) -> None:
        """Return a deferred task to the priority queue."""
        task.status = STATUS_QUEUED
        self._queued.append(task)

    async def mark_running(self, task_id: str, worker_id: str) -> None:
        """Accept the running transition."""
        self._tasks[task_id].status = STATUS_RUNNING

    async def mark_completed(self, task) -> None:
        """Record a successfully executed task."""
        self.completed.append(task.id)

    async def mark_failed(self, task, error: dict) -> None:
        """Record a failed task."""
        self.failed.append((task.id, error))

    async def mark_canceled(self, task) -> None:
        """Accept a canceled task."""

    async def get_resource_queue_state(self, requirements, priority=5):
        """Report an empty queue."""
        return {"queued_count_total": 0, "queued_count_below_priority": 0, "priority_counts": []}


class _DispatchResources:
    """Resources double that reports the local machine as free."""

    def check_available(self, requirements):
        """Report the local capacity as available."""
        return True

    def check_available_after_unload(self, requirements):
        """Report the local capacity as available after an unload."""
        return True

    def check_available_for_reuse(self, requirements, model_id, provider_id):
        """Report no warm model to reuse."""
        return False

    def local_admission_requirements(self, requirements):
        """Return the local requirements."""
        return requirements or {}

    def evaluate_model_admission(self, *args, **kwargs):
        """Report no parallel admission."""
        return None

    def model_parallel_config(self, *args, **kwargs):
        """Report a single concurrent request."""
        return 1, None

    async def reserve_if_available_or_reuse(self, requirements, **kwargs):
        """Accept the reservation."""
        return True

    async def release_for(self, *args, **kwargs):
        """Accept the release."""

    def mark_model_started(self, *args, **kwargs):
        """Accept the model start."""

    def mark_model_finished(self, *args, **kwargs):
        """Accept the model finish."""


class _DispatchWorker:
    """Worker double that always succeeds."""

    id = "openaix"
    task_type = "agent"
    enabled = True

    async def execute(self, task, emit_chunk=None):
        """Return a successful result."""
        return WorkerResult(ok=True, data={"done": True})


class SchedulerDispatchTests(unittest.IsolatedAsyncioTestCase):
    """The whole dispatch path, with an affinity that pins the task to the remote."""

    async def test_the_task_runs_locally_instead_of_waiting_for_the_remote(self):
        """The refused remote is replaced by the warm local candidate and the task completes."""
        store = KvAffinityStore(policy=AnchorPolicy(short_fraction=1.0))
        payload = {
            "model": "m-remote",
            "messages": [
                {"role": "system", "content": "rules"},
                {"role": "user", "content": "one"},
            ],
        }
        task = _task()
        task.payload = payload
        fingerprint = store.fingerprint(
            "local-only",
            {"model": "local-only", "messages": payload["messages"]},
        )
        store.remember(fingerprint, provider="remote_aidir_110", model="m-remote", cache_group="m-remote")

        queue = _DispatchQueue([task])
        scheduler = Scheduler(
            queue=queue,
            workers={"openaix": _DispatchWorker()},
            workers_cfg={"openaix": {"provider": "smart"}},
            resources=_DispatchResources(),
            full_config=_config(),
            kv_affinity=store,
            affinity_config={"max_queue_diff_len": 8, "max_queue_diff_part": 3.0},
        )

        probes = {"remote": 0}

        async def fake_remote_probe(provider_id, model_id, *, priority, timeout_ms, incoming_bearer_token=""):
            probes["remote"] += 1
            return {
                "can_run_now": False,
                "admission": "base_memory",
                "free": {"local_machine": {"VRAM": 13795.0}},
                "needed": {"local_machine": {"VRAM": 21500}},
            }

        async def fake_ollama_probe(provider_id, model_id, *, timeout_ms, incoming_bearer_token=""):
            return True

        scheduler._probe_remote_model_queue_state = fake_remote_probe
        scheduler._probe_ollama_model_availability = fake_ollama_probe

        runner = asyncio.create_task(scheduler.run())
        try:
            for _ in range(200):
                await asyncio.sleep(0.05)
                if queue.completed or queue.failed:
                    break
        finally:
            scheduler._running = False
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass

        self.assertEqual(queue.completed, [task.id])
        self.assertEqual(queue.failed, [])
        self.assertEqual(probes["remote"], 1)
        self.assertEqual(task.config["route"]["resolved_provider"], "llama_local")
        self.assertEqual(task.config["route"]["resolved_model"], "m-local")


if __name__ == "__main__":
    unittest.main()
