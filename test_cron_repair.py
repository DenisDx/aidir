"""Regression tests for safe WebUI cron repair behavior."""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from webui.backend import app as webui_app


class TestCronRepair(unittest.TestCase):
    """Validate marker-scoped, non-destructive crontab updates."""

    def test_adds_entry_when_user_has_no_crontab(self) -> None:
        """Installs one canonical entry when crontab reports it does not exist."""
        read = subprocess.CompletedProcess(
            ["crontab", "-l"], 1, stdout="", stderr="no crontab for denis\n"
        )
        write = subprocess.CompletedProcess(["crontab", "-"], 0, stdout="", stderr="")

        with patch("webui.backend.app.subprocess.run", side_effect=[read, write]) as run:
            result = webui_app._repair_user_crontab()

        self.assertEqual(result, {"action": "added"})
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1].kwargs["input"], webui_app._aidir_cron_line() + "\n")

    def test_leaves_existing_canonical_entry_unchanged(self) -> None:
        """Does not write when exactly one canonical marker line already exists."""
        content = f"MAILTO=ops@example.test\n{webui_app._aidir_cron_line()}\n"
        read = subprocess.CompletedProcess(["crontab", "-l"], 0, stdout=content, stderr="")

        with patch("webui.backend.app.subprocess.run", return_value=read) as run:
            result = webui_app._repair_user_crontab()

        self.assertEqual(result, {"action": "unchanged"})
        self.assertEqual(run.call_count, 1)

    def test_repairs_only_the_marked_line(self) -> None:
        """Replaces one malformed marked line while preserving unrelated lines verbatim."""
        content = "SHELL=/bin/bash\n* * * * * broken-command # aidir-cron\n# keep this note\n"
        read = subprocess.CompletedProcess(["crontab", "-l"], 0, stdout=content, stderr="")
        write = subprocess.CompletedProcess(["crontab", "-"], 0, stdout="", stderr="")

        with patch("webui.backend.app.subprocess.run", side_effect=[read, write]) as run:
            result = webui_app._repair_user_crontab()

        self.assertEqual(result, {"action": "repaired"})
        expected = f"SHELL=/bin/bash\n{webui_app._aidir_cron_line()}\n# keep this note\n"
        self.assertEqual(run.call_args_list[1].kwargs["input"], expected)

    def test_rejects_duplicate_markers_without_writing(self) -> None:
        """Refuses ambiguous ownership rather than risking removal of a cron line."""
        content = f"{webui_app._aidir_cron_line()}\n* * * * * old-command # aidir-cron\n"
        read = subprocess.CompletedProcess(["crontab", "-l"], 0, stdout=content, stderr="")

        with patch("webui.backend.app.subprocess.run", return_value=read) as run:
            with self.assertRaisesRegex(webui_app.CronRepairError, "multiple aidir cron entries"):
                webui_app._repair_user_crontab()

        self.assertEqual(run.call_count, 1)

    def test_rejects_unrecognized_read_error_without_writing(self) -> None:
        """Returns an error for crontab read failures other than a missing crontab."""
        read = subprocess.CompletedProcess(["crontab", "-l"], 1, stdout="", stderr="permission denied\n")

        with patch("webui.backend.app.subprocess.run", return_value=read) as run:
            with self.assertRaisesRegex(webui_app.CronRepairError, "Cannot read crontab"):
                webui_app._repair_user_crontab()

        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)