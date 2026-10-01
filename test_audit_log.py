"""Regression tests for the append-only audit writer and its SQLite index."""
from __future__ import annotations

import json
import errno
import os
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from core.audit_log import AuditLog


class AuditLogTests(unittest.TestCase):
    """Verify JSONL audit records remain usable when the index is rebuilt."""

    def test_records_event_and_reads_it_through_index(self):
        """Write one request event and read the identical record by its event ID."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            audit_log = AuditLog(temporary_directory)
            event = audit_log.record_client_request(
                task_id="task-1",
                protocol="openai",
                endpoint="/v1/chat/completions",
                data={"model": "example", "stream": True},
                body_storage="inline",
                data_encoding="json",
            )

            location = audit_log.find_event(event["event_id"])
            self.assertIsNotNone(location)
            self.assertEqual(location["task_id"], "task-1")
            self.assertTrue(location["journal_path"].startswith("raw_client_requests-"))
            self.assertEqual(audit_log.read_event(event["event_id"]), event)

    def test_rebuild_index_recovers_records_and_skips_corrupt_lines(self):
        """Recreate indexed locations from journals without trusting old SQLite rows."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            audit_log = AuditLog(temporary_directory)
            first = audit_log.record_llm_request(task_id="task-1", data={"model": "example"})
            second = audit_log.record_llm_response(task_id="task-1", data="data: done\n\n")

            with closing(audit_log._connect()) as connection:
                connection.execute("DELETE FROM events")
                connection.commit()
            self.assertIsNone(audit_log.find_event(first["event_id"]))

            journal = next(Path(temporary_directory).glob("raw_llm_requests-*.jsonl"))
            with journal.open("ab") as handle:
                handle.write(b"not-json\n")

            self.assertEqual(audit_log.rebuild_index(), 2)
            self.assertEqual(audit_log.read_event(first["event_id"]), first)
            self.assertEqual(audit_log.read_event(second["event_id"]), second)

    def test_rejection_journal_excludes_body_by_caller_contract(self):
        """Write a compact rejected-request record without a request body field."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            audit_log = AuditLog(temporary_directory)
            event = audit_log.record_rejected_request(
                request_id="request-1",
                protocol="openai",
                endpoint="/v1/chat/completions",
                http={"status_code": 413},
                error_code="REQUEST_TOO_LARGE",
                reason="Request body exceeds the configured maximum size",
            )

            self.assertNotIn("data", event)
            journal = next(Path(temporary_directory).glob("rejected_requests-*.jsonl"))
            stored = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(stored["request_id"], "request-1")
            self.assertNotIn("data", stored)

    def test_body_event_preserves_json_text_and_binary_forms(self):
        """Write JSON and text inline while keeping binary bytes in an audit file."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            audit_log = AuditLog(temporary_directory)
            json_event = audit_log.record_body_event(
                "client_request",
                b'{"model":"example"}',
                content_type="application/json",
            )
            text_event = audit_log.record_body_event(
                "llm_response",
                b"data: partial\n\n",
                content_type="text/event-stream",
            )
            binary_event = audit_log.record_body_event(
                "client_response",
                b"\x89PNG\r\n\x1a\n",
                content_type="image/png",
            )

            self.assertEqual(json_event["data"], {"model": "example"})
            self.assertEqual(json_event["data_encoding"], "json")
            self.assertEqual(text_event["data"], "data: partial\n\n")
            self.assertEqual(text_event["data_encoding"], "utf-8")
            self.assertEqual(binary_event["body_storage"], "file")
            body_path = Path(temporary_directory) / binary_event["body_file"]["relative_path"]
            self.assertEqual(body_path.read_bytes(), b"\x89PNG\r\n\x1a\n")

    def test_terminal_snapshot_merges_late_client_response_reconciliation(self):
        """Combine append-only late stream references without mutating the terminal record."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            audit_log = AuditLog(temporary_directory)
            terminal = audit_log.record_task_terminal(
                task_id="task-1",
                status="completed",
                task={},
                raw_event_refs={"client_request": ["request-1"], "client_response": [], "llm_requests": [], "llm_responses": []},
            )
            audit_log.record_task_reconciliation(
                task_id="task-1",
                raw_event_refs={"client_response": ["response-1"]},
            )

            self.assertEqual(audit_log.read_event(terminal["event_id"])["raw_event_refs"]["client_response"], [])
            snapshot = audit_log.task_terminal_snapshot("task-1")
            self.assertEqual(snapshot["raw_event_refs"]["client_request"], ["request-1"])
            self.assertEqual(snapshot["raw_event_refs"]["client_response"], ["response-1"])

    def test_reconciliation_hides_and_completes_interrupted_partition_deletion(self):
        """Hide a deleting partition before recovery removes its journal and index rows."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            audit_log = AuditLog(temporary_directory)
            event = audit_log.record_client_request(task_id="task-1", data={"model": "example"})
            day = audit_log._partition_day_from_journal(audit_log.find_event(event["event_id"])["journal_path"])
            audit_log._set_partition_state(day, "deleting")

            self.assertEqual(audit_log.list_task_events("task-1"), [])
            self.assertIsNone(audit_log.read_event(event["event_id"]))
            self.assertEqual(audit_log.reconcile_partitions(), [day])
            self.assertIsNone(audit_log.find_event(event["event_id"]))

    def test_cleanup_stale_spools_preserves_recent_stream_spools(self):
        """Remove only abandoned spool files outside the configured recovery age."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            audit_log = AuditLog(temporary_directory)
            stale = audit_log.open_body_spool()
            recent = audit_log.open_body_spool()
            os.utime(stale, (time.time() - 120, time.time() - 120))

            self.assertEqual(audit_log.cleanup_stale_spools(60), 1)
            self.assertFalse(stale.exists())
            self.assertTrue(recent.exists())

    def test_writer_failure_marks_audit_health_error(self):
        """Expose an append failure without making the index the source of truth."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            audit_log = AuditLog(temporary_directory)
            with patch("pathlib.Path.open", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    audit_log.record_client_request(task_id="task-1", data={"model": "example"})

            health = audit_log.health()
            self.assertEqual(health["status"], "error")
            self.assertEqual(health["write_failures"], 1)
            self.assertIn("disk full", health["last_error"])

    def test_best_effort_writer_queue_drops_overflow_without_raising(self):
        """Drop an overflow event while retaining task execution and warning health."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            audit_log = AuditLog(temporary_directory)
            started = threading.Event()
            release = threading.Event()
            original_append = audit_log._append_record_sync

            def blocked_append(*args, **kwargs):
                started.set()
                release.wait(timeout=2)
                return original_append(*args, **kwargs)

            audit_log.start_writer(queue_size=1, overflow_policy="best_effort")
            try:
                with patch.object(audit_log, "_append_record_sync", side_effect=blocked_append):
                    audit_log.record_client_request(task_id="task-1", data={"sequence": 1})
                    self.assertTrue(started.wait(timeout=1))
                    audit_log.record_client_request(task_id="task-1", data={"sequence": 2})
                    audit_log.record_client_request(task_id="task-1", data={"sequence": 3})

                health = audit_log.health()
                self.assertEqual(health["status"], "warning")
                self.assertEqual(health["dropped_events"], 1)
            finally:
                release.set()
                audit_log.stop_writer()

    def test_durable_writer_queue_drains_overflow_without_loss(self):
        """Wait for queue capacity under durable policy and persist every submitted event."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            audit_log = AuditLog(temporary_directory)
            started = threading.Event()
            release = threading.Event()
            submitted = threading.Event()
            original_append = audit_log._append_record_sync

            def blocked_append(*args, **kwargs):
                started.set()
                release.wait(timeout=2)
                return original_append(*args, **kwargs)

            audit_log.start_writer(queue_size=1, overflow_policy="durable")
            try:
                with patch.object(audit_log, "_append_record_sync", side_effect=blocked_append):
                    first = threading.Thread(
                        target=lambda: audit_log.record_client_request(task_id="task-1", data={"sequence": 1}),
                    )
                    first.start()
                    self.assertTrue(started.wait(timeout=1))
                    second = threading.Thread(
                        target=lambda: audit_log.record_client_request(task_id="task-1", data={"sequence": 2}),
                    )
                    second.start()
                    producer = threading.Thread(
                        target=lambda: (audit_log.record_client_request(task_id="task-1", data={"sequence": 3}), submitted.set()),
                    )
                    producer.start()
                    self.assertFalse(submitted.wait(timeout=0.05))
                    release.set()
                    first.join(timeout=2)
                    second.join(timeout=2)
                    producer.join(timeout=2)
                    self.assertFalse(first.is_alive())
                    self.assertFalse(second.is_alive())
                    self.assertTrue(submitted.is_set())

                audit_log.stop_writer()
                self.assertEqual(len(audit_log.list_task_events("task-1")), 3)
            finally:
                release.set()
                audit_log.stop_writer()

    def test_quota_retention_counts_attachments_without_deleting_recent_partition(self):
        """Remove an old full partition under quota pressure while preserving yesterday's evidence."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            audit_log = AuditLog(temporary_directory)
            today = datetime.now().astimezone().date()
            old_day = (today - timedelta(days=2)).isoformat()
            recent_day = (today - timedelta(days=1)).isoformat()
            old_journal = audit_log.directory / f"raw_client_requests-{old_day}.jsonl"
            recent_journal = audit_log.directory / f"raw_client_requests-{recent_day}.jsonl"
            old_journal.write_bytes(b"old\n")
            recent_journal.write_bytes(b"recent\n")
            attachment = audit_log.directory / "files" / old_day / "attachment"
            attachment.parent.mkdir(parents=True)
            attachment.write_bytes(b"x" * 4096)

            removed = audit_log.retain_partitions(retention_days=365, max_total_bytes=audit_log.disk_usage_bytes() - 1)

            self.assertIn(old_day, removed)
            self.assertFalse(old_journal.exists())
            self.assertFalse(attachment.exists())
            self.assertTrue(recent_journal.exists())
            self.assertEqual(audit_log.health()["retention_status"], "ok")

    def test_emergency_retention_deletes_closed_but_not_active_partition(self):
        """Emergency quota may remove yesterday's partition but must retain the active local day."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            audit_log = AuditLog(temporary_directory)
            today = datetime.now().astimezone().date()
            closed_day = (today - timedelta(days=1)).isoformat()
            closed_journal = audit_log.directory / f"raw_client_requests-{closed_day}.jsonl"
            active_journal = audit_log.directory / f"raw_client_requests-{today.isoformat()}.jsonl"
            closed_journal.write_bytes(b"closed\n")
            active_journal.write_bytes(b"active\n")

            removed = audit_log.retain_partitions(
                retention_days=365,
                max_total_bytes=10**9,
                emergency_max_total_bytes=0,
            )

            self.assertEqual(removed, [closed_day])
            self.assertFalse(closed_journal.exists())
            self.assertTrue(active_journal.exists())

    def test_enospc_failure_forces_emergency_cleanup_of_closed_partition(self):
        """Free closed audit evidence after an ENOSPC write failure without touching the active day."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            audit_log = AuditLog(temporary_directory, emergency_max_total_bytes=0)
            old_day = (datetime.now().astimezone().date() - timedelta(days=1)).isoformat()
            old_journal = audit_log.directory / f"raw_client_requests-{old_day}.jsonl"
            old_journal.write_bytes(b"old\n")

            with patch("pathlib.Path.open", side_effect=OSError(errno.ENOSPC, "disk full")):
                with self.assertRaises(OSError):
                    audit_log.record_client_request(task_id="task-1", data={"model": "example"})

            self.assertFalse(old_journal.exists())
            self.assertEqual(audit_log.health()["status"], "error")

    def test_data_uri_media_becomes_indexed_attachment_without_json_rewrite(self):
        """Extract recognized media while retaining the original JSON value in data."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            audit_log = AuditLog(temporary_directory)
            original = {"image_url": "data:image/png;base64,aGVsbG8="}
            event = audit_log.record_body_event(
                "client_request",
                json.dumps(original).encode("utf-8"),
                content_type="application/json",
            )

            self.assertEqual(event["data"], original)
            attachment = event["attachments"][0]
            resolved = audit_log.find_body_file(attachment["file_id"])
            self.assertEqual(resolved["body_file"], attachment)
            self.assertEqual((audit_log.directory / attachment["relative_path"]).read_bytes(), b"hello")
            audit_log.rebuild_index()
            self.assertEqual(audit_log.find_body_file(attachment["file_id"])["body_file"], attachment)

    def test_configured_metadata_secret_is_redacted_without_rewriting_data(self):
        """Redact configured envelope metadata while preserving the valid JSON body exactly."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            audit_log = AuditLog(temporary_directory, secret_metadata_keys=["tenant_secret"])
            event = audit_log.record_body_event(
                "client_request",
                b'{"tenant_secret":"body-value"}',
                metadata={"tenant_secret": "metadata-value"},
            )

            self.assertEqual(event["metadata"]["tenant_secret"], "[REDACTED]")
            self.assertEqual(event["data"], {"tenant_secret": "body-value"})