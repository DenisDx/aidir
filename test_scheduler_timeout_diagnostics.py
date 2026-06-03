import unittest
from datetime import datetime, timedelta, timezone

from core.endpoint import BaseEndpoint
from core.scheduler import Scheduler
from core.task_types.task_agent import Task_agent


class _TimeoutTestEndpoint(BaseEndpoint):
    """Tiny endpoint stub exposing BaseEndpoint timeout helpers for tests."""

    def create_app(self, core):
        return None

    async def initialize(self, core):
        return None


class TestSchedulerTimeoutDiagnostics(unittest.TestCase):
    """Regression checks for scheduler timeout diagnostics."""

    def test_run_timeout_source_uses_config_timeout_by_default(self) -> None:
        """Default task timeout should point to tasks.run_timeout config source."""
        task = Task_agent(payload={}, stream=False)
        task.run_timeout = 300

        self.assertEqual(
            Scheduler._run_timeout_source(task),
            "tasks.run_timeout / TASK_RUN_TIMEOUT_SECONDS",
        )

    def test_build_run_timeout_message_includes_elapsed_limit_and_source(self) -> None:
        """Diagnostic timeout message should include elapsed time, limit, and source."""
        task = Task_agent(payload={}, stream=False)
        task.run_timeout = 300

        self.assertEqual(
            Scheduler._build_run_timeout_message(task, 301.234),
            (
                "Task run timeout exceeded after 301.23s "
                "(limit=300s, parameter=task.run_timeout, source=tasks.run_timeout / "
                "TASK_RUN_TIMEOUT_SECONDS)"
            ),
        )

    def test_queue_timeout_source_uses_config_timeout(self) -> None:
        """Queue timeout diagnostics should point to tasks.queue_timeout config source."""
        task = Task_agent(payload={}, stream=False)
        task.queue_timeout = 120

        self.assertEqual(
            Scheduler._queue_timeout_source(task),
            "tasks.queue_timeout / TASK_QUEUE_TIMEOUT_SECONDS",
        )

    def test_build_queue_timeout_message_includes_elapsed_limit_and_source(self) -> None:
        """Queue-timeout message should include elapsed time, limit, and source."""
        task = Task_agent(payload={}, stream=False)
        task.queue_timeout = 120

        self.assertEqual(
            Scheduler._build_queue_timeout_message(task, 121.234),
            (
                "Task queue timeout exceeded after 121.23s "
                "(limit=120s, parameter=task.queue_timeout, source=tasks.queue_timeout / "
                "TASK_QUEUE_TIMEOUT_SECONDS)"
            ),
        )

    def test_queue_timeout_expired_only_before_first_execution(self) -> None:
        """Queue timeout applies only before first transition to running."""
        task = Task_agent(payload={}, stream=False)
        task.queue_timeout = 120
        task.created_at = datetime.now(timezone.utc) - timedelta(seconds=121)

        self.assertTrue(Scheduler._queue_timeout_expired(task))

        task.started_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertFalse(Scheduler._queue_timeout_expired(task))

    def test_endpoint_timeout_phase_switches_from_queue_to_run(self) -> None:
        """Endpoint waiting logic should switch from queue timeout to run timeout after start."""
        endpoint = _TimeoutTestEndpoint()
        task = Task_agent(payload={}, stream=False)
        task.created_at = datetime.now(timezone.utc) - timedelta(seconds=110)
        task.queue_timeout = 120
        task.run_timeout = 300

        phase, remaining = endpoint._task_timeout_phase(task)
        self.assertEqual(phase, "queue")
        self.assertIsNotNone(remaining)
        self.assertLess(remaining, 11)

        task.started_at = datetime.now(timezone.utc) - timedelta(seconds=30)
        phase, remaining = endpoint._task_timeout_phase(task)
        self.assertEqual(phase, "run")
        self.assertIsNotNone(remaining)
        self.assertGreater(remaining, 260)


if __name__ == "__main__":
    unittest.main(verbosity=2)