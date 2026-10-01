"""Regression coverage for bounded SIGTERM shutdown helpers."""
from __future__ import annotations

import asyncio
import tempfile
import unittest

from core.app import _SERVICE_TASK_SHUTDOWN_TIMEOUT_SECONDS, _build_server_config, _wait_for_service_tasks
from core.app import Core
from core.audit_log import AuditLog


class ShutdownLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """Verify shutdown helpers cannot wait forever on inactive service work."""

    async def test_stuck_service_task_is_canceled_after_deadline(self):
        """Cancel a non-terminating server task after the configured stop deadline."""
        blocker = asyncio.Event()

        async def stuck_server() -> None:
            """Wait forever until shutdown cancels the task."""
            await blocker.wait()

        task = asyncio.create_task(stuck_server(), name="service:stuck")
        stopped_cleanly = await _wait_for_service_tasks([task], timeout=0.01)

        self.assertFalse(stopped_cleanly)
        self.assertTrue(task.done())
        self.assertTrue(task.cancelled())

    async def test_audit_writer_stops_with_success_status(self):
        """Return a completion status after draining an idle audit writer."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            audit_log = AuditLog(temporary_directory)
            audit_log.start_writer()
            self.assertTrue(audit_log.stop_writer(timeout=1.0))

    async def test_server_config_bounds_stream_connection_shutdown(self):
        """Give Uvicorn time to cancel connection tasks before outer cancellation."""
        config = _build_server_config(lambda scope, receive, send: None, "127.0.0.1", 0)

        self.assertEqual(config.timeout_graceful_shutdown, 5.0)
        self.assertGreater(_SERVICE_TASK_SHUTDOWN_TIMEOUT_SECONDS, config.timeout_graceful_shutdown)

    async def test_sigterm_drains_active_tasks_for_five_seconds_before_canceling(self):
        """Give active work a short drain window before cancellation."""
        class _Scheduler:
            """Capture the shutdown operations requested by Core."""

            def __init__(self):
                """Initialize one active fake task."""
                self.active_tasks = 1
                self.cancel_calls = 0
                self.wait_calls = 0

            def active_task_labels(self):
                """Return one active task label."""
                return ["task:active:worker"] if self.active_tasks else []

            def active_task_count(self):
                """Return the current fake active-task count."""
                return self.active_tasks

            def notify_new_task(self):
                """Accept the restart wake-up notification."""

            async def wait_for_active_tasks(self, timeout):
                """Record the bounded signal drain and leave the task active."""
                self.wait_calls += 1
                self.wait_timeout = timeout
                return False

            async def cancel_active_tasks(self, timeout):
                """Cancel the active fake task."""
                self.cancel_calls += 1
                self.active_tasks = 0
                return 1

            def stop(self):
                """Accept the scheduler stop request."""

        core = Core(manage_local_servers=False)
        scheduler = _Scheduler()
        core.scheduler = scheduler

        report = await core.graceful_shutdown(source="signal")

        self.assertEqual(scheduler.cancel_calls, 1)
        self.assertEqual(scheduler.wait_calls, 1)
        self.assertEqual(scheduler.wait_timeout, 5.0)
        self.assertEqual(report.active_tasks, 0)


if __name__ == "__main__":
    unittest.main()