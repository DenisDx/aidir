"""Regression tests for the exit code of a restart the process asked for itself.

The unit is configured with `Restart=on-failure`. A restart requested through
the WebUI must therefore leave the process with a non-zero code, or the
supervisor takes the clean exit for "the service is fine" and the service stays
down.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import app as core_app


class PlannedRestartExitCodeTests(unittest.TestCase):
    """The planned restart must keep the exit code the supervisor needs."""

    def test_a_planned_restart_exits_with_a_non_zero_code(self):
        """The exception is re-raised, so the process leaves with code 1."""
        with patch.object(core_app, "main", side_effect=core_app._PlannedRestart()):
            with self.assertRaises(SystemExit) as raised:
                core_app.run_entrypoint()
        self.assertEqual(raised.exception.code, 1)

    def test_a_planned_restart_is_not_logged_as_a_crash(self):
        """A planned restart is an event, not a fatal exception."""
        recorded: list[tuple] = []

        with patch.object(core_app, "main", side_effect=core_app._PlannedRestart()), \
             patch.object(core_app, "log", lambda *args, **kwargs: recorded.append(args)), \
             patch.object(core_app, "log_exception", lambda *args, **kwargs: recorded.append(args)):
            with self.assertRaises(SystemExit):
                core_app.run_entrypoint()

        messages = " ".join(" ".join(str(part) for part in entry) for entry in recorded)
        self.assertIn("PLANNED RESTART", messages)
        self.assertNotIn("Fatal crash", messages)

    def test_a_real_crash_is_still_reported_and_re_raised(self):
        """An unexpected failure keeps the fatal log and the non-zero exit."""
        recorded: list[tuple] = []

        with patch.object(core_app, "main", side_effect=RuntimeError("boom")), \
             patch.object(core_app, "log", lambda *args, **kwargs: recorded.append(args)), \
             patch.object(core_app, "log_exception", lambda *args, **kwargs: recorded.append(args)):
            with self.assertRaises(RuntimeError):
                core_app.run_entrypoint()

        messages = " ".join(" ".join(str(part) for part in entry) for entry in recorded)
        self.assertIn("Fatal crash", messages)


class LiveLogSocketTests(unittest.IsolatedAsyncioTestCase):
    """The live log socket must not hold the graceful shutdown back."""

    async def test_the_socket_stops_when_the_service_shuts_down(self):
        """The handler leaves the loop as soon as the restart is requested."""
        from webui.backend import app as webui_app

        class _Core:
            """Core double that reports a restart in progress."""

            def get_runtime_status(self):
                """Report that the service is restarting."""
                return {"restart_requested": True, "accepting_new_tasks": False}

        class _Socket:
            """WebSocket double that accepts and records nothing else."""

            def __init__(self) -> None:
                """Prepare the cookie jar."""
                self.cookies: dict[str, str] = {}
                self.sent: list[str] = []

            async def accept(self) -> None:
                """Accept the connection."""

            async def send_text(self, text: str) -> None:
                """Record a sent line."""

            async def close(self, code: int = 1000) -> None:
                """Close the connection."""

        core = _Core()
        socket = _Socket()
        sent: list[str] = []

        async def fake_session(_core, _token):
            """Accept the connection."""
            return {"login": "tester", "permissions": ["all"]}

        async def fake_tail(_path):
            """Return no context lines."""
            return []

        with tempfile.TemporaryDirectory() as directory:
            log_file = Path(directory) / "all.log"
            log_file.write_text("a line\n", encoding="utf-8")
            with patch.object(webui_app, "_get_session", fake_session), \
                 patch.object(webui_app, "_read_log_tail_lines", fake_tail), \
                 patch.object(webui_app, "_read_appended_log_lines", lambda *args, **kwargs: (None, [])):
                # The handler must return instead of looping forever.
                await asyncio.wait_for(
                    webui_app._stream_log_lines(core, socket, log_file),
                    timeout=2.0,
                )
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
