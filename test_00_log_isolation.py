"""Test isolation: the suite must not write into the logs of the running instance.

The logger writes to the project's `logs/` directory, and the test suite exercises
the code paths that log. The doubles' messages - "close check 0", "first-line" -
therefore ended up in the log of a live instance, where they are indistinguishable
from a real failure for whoever reads it afterwards.

The logger honours `AIDIR_LOG_DIR` (see `core.logger.logs_dir`). This module
points it at a temporary directory for the whole run.

The file name matters: `unittest discover` imports the test modules in sorted
order, and this one sorts first, so the environment is set before any other
module can write a log line.
"""

import atexit
import os
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT_LOGS = ROOT / "logs"

_TEST_LOG_DIR = tempfile.mkdtemp(prefix="aidir-test-logs-")
os.environ["AIDIR_LOG_DIR"] = _TEST_LOG_DIR


@atexit.register
def _remove_test_log_dir() -> None:
    """Remove the temporary log directory when the run ends."""
    shutil.rmtree(_TEST_LOG_DIR, ignore_errors=True)


class LogIsolationTests(unittest.TestCase):
    """The suite must not touch the log directory of the running instance."""

    def test_the_log_directory_is_moved_out_of_the_way(self):
        """The logger writes into the temporary directory, not into the project."""
        from core.logger import logs_dir

        self.assertEqual(logs_dir().resolve(), Path(_TEST_LOG_DIR).resolve())
        self.assertNotEqual(logs_dir().resolve(), PROJECT_LOGS.resolve())

    def test_the_project_log_directory_is_untouched(self):
        """A log line written by the suite lands in the temporary directory."""
        from core import log
        from core.logger import logs_dir

        project_log = PROJECT_LOGS / "system.log"
        before = project_log.stat().st_size if project_log.exists() else 0

        log("system", "error", "the suite must not write into the project logs")

        self.assertTrue((logs_dir() / "system.log").exists())
        after = project_log.stat().st_size if project_log.exists() else 0
        self.assertEqual(before, after, "the suite wrote into the project log directory")


if __name__ == "__main__":
    unittest.main()
