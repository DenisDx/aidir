"""Regression tests for the log maintenance.

The wipe and the trim used to run at import time, against the project's `logs/`
directory. That wrote into the log of the running instance on every test run, and
the result was only reported by a print - a failing wipe went unnoticed.

Both now run inside a test, against the log directory the logger actually uses.
"""
import gc
import json
import tempfile
import unittest
import unittest.mock
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.cron import _trim_log_file
from core.logger import CoreLogger, logger, logs_dir


class LogMaintenanceTests(unittest.TestCase):
    """The wipe and the trim must keep the fresh records and drop the old ones."""

    def test_the_wipe_drops_the_old_lines_and_keeps_the_fresh_ones(self):
        """A line older than the limit is removed; a fresh one stays."""
        now = datetime.now(timezone.utc)
        old = (now - timedelta(hours=2)).isoformat(timespec="milliseconds")
        fresh = (now - timedelta(seconds=10)).isoformat(timespec="milliseconds")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_path = root / "__wipe_test.log"
            jsonl_path = root / "__wipe_test.jsonl"
            log_path.write_text(
                f"{old} [INFO] [system] old line\n{fresh} [INFO] [system] fresh line\n",
                encoding="utf-8",
            )
            jsonl_path.write_text(
                json.dumps({"ts": old, "k": "old"}, ensure_ascii=False) + "\n"
                + json.dumps({"ts": fresh, "k": "fresh"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            with unittest.mock.patch.object(logger, "_cfg", None), \
                 unittest.mock.patch("core.logger.logs_dir", lambda: root):
                logger.wipe_logs(60)

            log_text = log_path.read_text(encoding="utf-8")
            jsonl_text = jsonl_path.read_text(encoding="utf-8")

        self.assertNotIn("old line", log_text)
        self.assertIn("fresh line", log_text)
        self.assertNotIn('"k": "old"', jsonl_text)
        self.assertIn('"k": "fresh"', jsonl_text)

    def test_the_trim_bounds_the_file_and_reports_the_removed_bytes(self):
        """A file above the limit is cut to the limit."""
        fresh = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(
            timespec="milliseconds"
        )
        lines = [
            json.dumps({"ts": fresh, "i": i, "payload": "x" * 30}, ensure_ascii=False)
            for i in range(20)
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "__trim_test.jsonl"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            removed = _trim_log_file(path, 300)
            size = path.stat().st_size

        self.assertGreater(removed, 0)
        self.assertLessEqual(size, 300)


class LoggerResourceTests(unittest.TestCase):
    """The logger must close what it opens."""

    def test_log_append_closes_the_file_explicitly(self):
        """Regression: writing through a temporary leaves the close to the collector.

        `path.open("a").write(line)` is finalised rather than closed, so every
        line raises a ResourceWarning - noise that hides the ones that matter -
        and it is measurably slower than an explicit block.

        The level matters: `log()` drops anything above the configured threshold
        before it opens a file, so a run that logs at `info` under an `error`
        threshold writes nothing and would pass against the old code. Logging at
        `error` guarantees the write happens.
        """
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            local = CoreLogger()
            for i in range(5):
                local.log("system", "error", f"close check {i}")
            gc.collect()

        unclosed = [w for w in caught if issubclass(w.category, ResourceWarning)]
        self.assertEqual(
            unclosed, [],
            "the logger left files to the collector: "
            + "; ".join(str(w.message)[:80] for w in unclosed),
        )


if __name__ == "__main__":
    unittest.main()
