import unittest

from core.scheduler import Scheduler
from core.task_types.task_agent import Task_agent


class TestSchedulerTimeoutDiagnostics(unittest.TestCase):
    """Regression checks for scheduler timeout diagnostics."""

    def test_run_timeout_source_uses_request_timeout_override(self) -> None:
        """Request-level timeout override should be reflected in diagnostics."""
        task = Task_agent(payload={"timeout": 42}, stream=False)
        task.run_timeout = 42

        self.assertEqual(Scheduler._run_timeout_source(task), "payload.timeout")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)