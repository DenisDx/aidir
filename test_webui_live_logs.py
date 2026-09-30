"""Regression tests for WebUI live log streaming."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from webui.backend.app import (
    _list_log_files,
    _new_log_tail_cursor,
    _read_log_tail_lines,
    _read_appended_log_lines,
    _resolve_log_file,
)


class TestWebUiLiveLogs(unittest.TestCase):
    """Validate log growth handling for live WebSocket subscribers."""

    def test_log_file_discovery_allows_only_supported_log_types(self) -> None:
        """Lists log and JSONL files while rejecting arbitrary file access."""
        with tempfile.TemporaryDirectory() as directory:
            logs_dir = Path(directory)
            (logs_dir / "worker.log").touch()
            (logs_dir / "openaix_call_log.jsonl").touch()
            (logs_dir / "secrets.txt").touch()

            with patch("webui.backend.app._LOGS_DIR", logs_dir):
                self.assertEqual(
                    _list_log_files(),
                    ["openaix_call_log.jsonl", "worker.log"],
                )
                self.assertEqual(_resolve_log_file("worker.log"), logs_dir / "worker.log")
                with self.assertRaises(HTTPException):
                    _resolve_log_file("secrets.txt")

    def test_initial_tail_keeps_only_complete_recent_lines(self) -> None:
        """Returns the requested recent lines without a partial leading record."""
        with tempfile.TemporaryDirectory() as directory:
            log_file = Path(directory) / "worker.log"
            log_file.write_bytes(b"partial-prefix" + b"\nfirst\nsecond\nthird\n")

            lines = _read_log_tail_lines(log_file, max_lines=2)

            self.assertEqual(lines, ["second", "third"])

    def test_streaming_resumes_after_log_is_truncated(self) -> None:
        """Resets a stale offset after trimming and reads later llama.cpp output."""
        with tempfile.TemporaryDirectory() as directory:
            log_file = Path(directory) / "local_llama_cpp.log"
            log_file.write_text("old output\n" * 200, encoding="utf-8")
            cursor = _new_log_tail_cursor(log_file)

            log_file.write_text("retained output\n", encoding="utf-8")
            cursor, lines = _read_appended_log_lines(log_file, cursor)

            self.assertEqual(cursor.offset, log_file.stat().st_size)
            self.assertEqual(lines, [])

            with log_file.open("a", encoding="utf-8") as handle:
                handle.write("I slot print_timing: progress = 0.25\n")

            cursor, lines = _read_appended_log_lines(log_file, cursor)

            self.assertEqual(cursor.offset, log_file.stat().st_size)
            self.assertEqual(lines, ["I slot print_timing: progress = 0.25"])

    def test_streaming_buffers_incomplete_log_record(self) -> None:
        """Waits for a newline before delivering a concurrently written record."""
        with tempfile.TemporaryDirectory() as directory:
            log_file = Path(directory) / "local_llama_cpp.log"
            log_file.touch()
            cursor = _new_log_tail_cursor(log_file)

            with log_file.open("ab") as handle:
                handle.write(b"I slot print_timing: progress")
            cursor, lines = _read_appended_log_lines(log_file, cursor)

            self.assertEqual(lines, [])

            with log_file.open("ab") as handle:
                handle.write(b" = 0.25\n")
            cursor, lines = _read_appended_log_lines(log_file, cursor)

            self.assertEqual(lines, ["I slot print_timing: progress = 0.25"])

    def test_streaming_resumes_after_log_file_is_replaced(self) -> None:
        """Resets after atomic rotation and streams lines written to the replacement."""
        with tempfile.TemporaryDirectory() as directory:
            log_file = Path(directory) / "worker.log"
            log_file.write_text("old output\n", encoding="utf-8")
            cursor = _new_log_tail_cursor(log_file)

            replacement = Path(directory) / "worker.log.next"
            replacement.write_text("retained output\n", encoding="utf-8")
            replacement.replace(log_file)
            cursor, lines = _read_appended_log_lines(log_file, cursor)

            self.assertEqual(lines, [])
            with log_file.open("a", encoding="utf-8") as handle:
                handle.write("new worker output\n")

            cursor, lines = _read_appended_log_lines(log_file, cursor)

            self.assertEqual(cursor.offset, log_file.stat().st_size)
            self.assertEqual(lines, ["new worker output"])

    def test_streaming_reads_file_created_after_subscription(self) -> None:
        """Streams initial lines when a missing log file is created later."""
        with tempfile.TemporaryDirectory() as directory:
            log_file = Path(directory) / "new.log"
            cursor = _new_log_tail_cursor(log_file)

            log_file.write_text("first output\n", encoding="utf-8")
            cursor, lines = _read_appended_log_lines(log_file, cursor)

            self.assertEqual(cursor.offset, log_file.stat().st_size)
            self.assertEqual(lines, ["first output"])