"""Regression tests for lifecycle hook loading and scheduler control."""
from __future__ import annotations

import tempfile
import unittest
import importlib.util
from pathlib import Path
from unittest.mock import patch

from core.hooks import HookManager
from core.scheduler import Scheduler
from core.task import Task
from core.worker import WorkerResult


class _LocalServerManager:
    """Provide the local-server methods used by hook tests."""

    def owned_records(self):
        """Return no owned providers."""
        return {}


class _Queue:
    """Record scheduler queue transitions for assertions."""

    def __init__(self) -> None:
        """Initialize transition records."""
        self.transitions: list[str] = []

    async def mark_running(self, task_id, worker_id):
        """Record a running transition."""
        self.transitions.append("running")

    async def mark_completed(self, task):
        """Record a completed transition."""
        self.transitions.append("completed")

    async def mark_canceled(self, task):
        """Record a cancelled transition."""
        self.transitions.append("canceled")

    async def add_task(self, task, *, is_retry=False):
        """Record a requeue transition."""
        self.transitions.append(f"requeue:{is_retry}")


class _Worker:
    """Return one fixed successful result."""

    id = "test-worker"

    async def execute(self, task, emit_chunk=None):
        """Return a completed OpenAI-compatible result."""
        return WorkerResult(ok=True, data={"choices": [{"message": {"content": ""}}]})


class _RecoveryHooks:
    """Record recovery hook actions."""

    def __init__(self) -> None:
        """Initialize action records."""
        self.restarts: list[str] = []
        self.retries: list[str] = []

    def is_owned_local_llama_cpp(self, provider_id):
        """Treat the configured test provider as owned."""
        return provider_id == "llama"

    async def restart_local_llama_cpp(self, provider_id):
        """Record one provider restart."""
        self.restarts.append(provider_id)

    async def retry_task(self, task, provider_id):
        """Record one task retry."""
        self.retries.append(provider_id)


class HookManagerTests(unittest.IsolatedAsyncioTestCase):
    """Verify hook discovery and lifecycle control."""

    async def test_disabled_hook_is_not_imported(self) -> None:
        """Keep disabled hook modules inactive."""
        with tempfile.TemporaryDirectory() as directory:
            hooks_dir = Path(directory) / "hooks"
            hooks_dir.mkdir()
            (hooks_dir / "disabled.py").write_text(
                "ENABLED = False\nraise RuntimeError('must not import')\n",
                encoding="utf-8",
            )
            manager = HookManager(directory, _LocalServerManager(), {})
            await manager.load()
            self.assertEqual(manager._handlers["task_created"], [])

    async def test_enabled_hook_receives_event(self) -> None:
        """Load an enabled hook and dispatch its registered event."""
        with tempfile.TemporaryDirectory() as directory:
            hooks_dir = Path(directory) / "hooks"
            hooks_dir.mkdir()
            marker = Path(directory) / "marker"
            (hooks_dir / "enabled.py").write_text(
                "ENABLED = True\n"
                "from pathlib import Path\n"
                "async def handler(task):\n"
                f"    Path({str(marker)!r}).write_text(task.id)\n"
                "def register(hooks):\n"
                "    hooks.on('task_created', handler)\n",
                encoding="utf-8",
            )
            manager = HookManager(directory, _LocalServerManager(), {})
            await manager.load()
            task = Task(type="agent", id="task-1")
            await manager.emit("task_created", task)
            self.assertEqual(marker.read_text(encoding="utf-8"), "task-1")

    async def test_hook_retry_requeues_without_completion(self) -> None:
        """Suppress first completion when a hook requests one retry."""
        queue = _Queue()
        hooks = HookManager(".", _LocalServerManager(), {})

        async def request_retry(task, response):
            """Request a retry for the completed response."""
            await hooks.retry_task(task)

        hooks.on("llm_response_complete", request_retry)
        scheduler = Scheduler(queue, {"test-worker": _Worker()}, hooks=hooks)
        task = Task(type="agent", id="task-2")
        await scheduler._run_task(task, _Worker())

        self.assertEqual(queue.transitions, ["running", "requeue:True"])
        self.assertIsNone(task.result)

    async def test_hook_cancel_suppresses_completion(self) -> None:
        """Suppress completion when a hook cancels before inference."""
        queue = _Queue()
        hooks = HookManager(".", _LocalServerManager(), {})

        async def cancel(task, request):
            """Request cancellation before inference."""
            await hooks.cancel_task(task, "test cancellation")

        hooks.on("before_llm_request", cancel)
        scheduler = Scheduler(queue, {"test-worker": _Worker()}, hooks=hooks)
        task = Task(type="agent", id="task-3")
        await scheduler._run_task(task, _Worker())

        self.assertEqual(queue.transitions, ["running", "canceled"])

    async def test_llama_recovery_retries_only_once(self) -> None:
        """Restart and retry one empty slash-reasoning response."""
        path = Path("hooks/llama_cpp_empty_response_recovery.py")
        spec = importlib.util.spec_from_file_location("test_recovery_hook", path)
        module = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(module)
        hooks = _RecoveryHooks()
        module._hooks = hooks
        task = Task(type="agent", id="task-4", route_provider_id="llama")
        response = {"choices": [{"message": {"content": "", "reasoning_content": "///"}}]}

        with patch.object(module, "log") as logger:
            await module.on_llm_response_complete(task, response)
            await module.on_llm_response_complete(task, response)

        self.assertEqual(hooks.restarts, ["llama"])
        self.assertEqual(hooks.retries, ["llama"])
        self.assertTrue(task.hook_metadata[module._ATTEMPT_KEY])
        logger.assert_called_once_with(
            "system",
            "info",
            "llama_cpp_empty_response_recovery restarted llama for task task-4",
        )


if __name__ == "__main__":
    unittest.main()
