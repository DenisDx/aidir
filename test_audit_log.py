"""Regression tests for the append-only audit writer and its SQLite index."""
from __future__ import annotations

import json
import errno
import hashlib
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
from core.queue_manager import QueueManager
from core.task import STATUS_COMPLETED, Task


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
            single_json_sse_event = audit_log.record_body_event(
                "client_response",
                b'data: {"ok":true}\n\ndata: [DONE]\n\n',
                content_type="text/event-stream",
            )
            multi_json_sse_event = audit_log.record_body_event(
                "client_response",
                (
                    b'data: {"id":"completion-1","object":"chat.completion.chunk","model":"model",'
                    b'"choices":[{"index":0,"delta":{"role":"assistant","content":"part "},"finish_reason":null}]}\n\n'
                    b'data: {"id":"completion-1","object":"chat.completion.chunk","model":"model",'
                    b'"choices":[{"index":0,"delta":{"content":"two","reasoning_content":"trace"},"finish_reason":"stop"}],'
                    b'"usage":{"completion_tokens":2}}\n\n'
                    b'data: [DONE]\n\n'
                ),
                content_type="text/event-stream",
            )
            binary_event = audit_log.record_body_event(
                "client_response",
                b"\x89PNG\r\n\x1a\n",
                content_type="image/png",
            )

            self.assertEqual(json_event["data"], {"model": "example"})
            self.assertEqual(json_event["data_encoding"], "json")
            self.assertEqual(json_event["body_format"], "json")
            self.assertEqual(text_event["data"], "data: partial\n\n")
            self.assertEqual(text_event["data_encoding"], "utf-8")
            self.assertEqual(text_event["body_format"], "sse")
            self.assertEqual(single_json_sse_event["data"], {"ok": True})
            self.assertEqual(single_json_sse_event["data_encoding"], "json")
            self.assertEqual(single_json_sse_event["body_format"], "sse")
            self.assertEqual(single_json_sse_event["sse_chunk_count"], 1)
            self.assertEqual(multi_json_sse_event["data"]["object"], "chat.completion")
            self.assertEqual(multi_json_sse_event["data"]["choices"][0]["message"], {
                "role": "assistant", "content": "part two", "reasoning_content": "trace",
            })
            self.assertEqual(multi_json_sse_event["data"]["choices"][0]["finish_reason"], "stop")
            self.assertEqual(multi_json_sse_event["data"]["usage"], {"completion_tokens": 2})
            self.assertEqual(multi_json_sse_event["data_encoding"], "json")
            self.assertEqual(multi_json_sse_event["body_format"], "sse")
            self.assertEqual(multi_json_sse_event["sse_chunk_count"], 2)
            self.assertEqual(binary_event["body_storage"], "file")
            body_path = Path(temporary_directory) / binary_event["body_file"]["relative_path"]
            self.assertEqual(body_path.read_bytes(), b"\x89PNG\r\n\x1a\n")

    def test_finalized_spool_uses_body_policy_and_inline_limit(self):
        """Classify streamed JSON, text, binary, and large text without retaining all bytes."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            audit_log = AuditLog(temporary_directory, max_inline_body_bytes=8)
            json_spool = audit_log.open_body_spool()
            json_spool.write(b'{"ok":1}')
            json_event = audit_log.finalize_body_spool("llm_response", json_spool, content_type="application/json")

            text_spool = audit_log.open_body_spool()
            text_spool.write(b"hello")
            text_event = audit_log.finalize_body_spool("llm_response", text_spool, content_type="text/plain")

            inline_audit_log = AuditLog(Path(temporary_directory) / "inline")
            sse_spool = inline_audit_log.open_body_spool()
            sse_spool.write(b'data: {"ok":true}\n\ndata: [DONE]\n\n')
            sse_event = inline_audit_log.finalize_body_spool("client_response", sse_spool, content_type="text/event-stream")

            binary_spool = audit_log.open_body_spool()
            binary_spool.write(b"\x89PNG")
            binary_event = audit_log.finalize_body_spool("llm_response", binary_spool, content_type="image/png")

            large_spool = audit_log.open_body_spool()
            large_spool.write(b"too-large")
            large_event = audit_log.finalize_body_spool("llm_response", large_spool, content_type="text/plain")

            self.assertEqual(json_event["data"], {"ok": 1})
            self.assertEqual(text_event["data"], "hello")
            self.assertEqual(sse_event["data"], {"ok": True})
            self.assertEqual(sse_event["data_encoding"], "json")
            self.assertEqual(sse_event["body_format"], "sse")
            self.assertEqual(binary_event["body_storage"], "file")
            self.assertEqual(large_event["body_storage"], "file")
            self.assertFalse(json_spool.exists())
            self.assertFalse(text_spool.exists())

    def test_completed_sse_decodes_all_json_chunks_without_changing_body_metadata(self):
        """Finalize complete SSE into ordered JSON values with exact original byte metadata."""
        values = [
            {"id": "completion-1", "object": "chat.completion.chunk", "created": 3, "model": "model", "vendor": {"a": 1}, "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "tr"}, "finish_reason": None}]},
            {"id": "completion-1", "object": "chat.completion.chunk", "model": "model", "choices": [{"index": 0, "delta": {"reasoning_content": "ace", "content": "answer"}, "finish_reason": "length"}]},
            {"id": "completion-1", "object": "chat.completion.chunk", "choices": [], "usage": {"completion_tokens_details": {"reasoning_tokens": 7}}},
        ]
        body = b"\xef\xbb\xbf: keepalive\r\n\r\nevent: message\r\nid: event-1\r\nretry: 1000\r\n"
        body += b"".join(b"data: " + line.encode() + b"\r\n" for line in json.dumps(values[0], indent=2).splitlines()) + b"\r\n"
        body += b"".join(b"data: " + json.dumps(value).encode() + b"\r\n\r\n" for value in values[1:])
        body += b"data: [DONE]\r\n\r\n: final comment\r\n\r\n"
        with tempfile.TemporaryDirectory() as directory:
            audit_log = AuditLog(directory)
            spool = audit_log.open_body_spool()
            for offset in range(0, len(body), 7):
                spool.write(body[offset:offset + 7])
            event = audit_log.finalize_body_spool(
                "llm_response", spool, content_type="text/event-stream; charset=utf-8",
                task_id="task-sse", terminal_status="ok",
            )
            self.assertEqual(event["data"], {
                "id": "completion-1",
                "object": "chat.completion",
                "created": 3,
                "model": "model",
                "vendor": {"a": 1},
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "reasoning_content": "trace", "content": "answer"},
                    "finish_reason": "length",
                }],
                "usage": {"completion_tokens_details": {"reasoning_tokens": 7}},
            })
            self.assertEqual(event["data_encoding"], "json")
            self.assertEqual(event["body_format"], "sse")
            self.assertEqual(event["sse_chunk_count"], 3)
            self.assertEqual(event["body_bytes"], len(body))
            self.assertEqual(event["body_sha256"], hashlib.sha256(body).hexdigest())
            self.assertEqual(audit_log.read_event(event["event_id"]), event)
            self.assertFalse(spool.exists())
            journal = next(audit_log.directory.glob("raw_llm_responses-*.jsonl"))
            self.assertEqual(json.loads(journal.read_text())["data"], event["data"])

    def test_sse_assembly_supports_all_line_delimiters_without_done(self):
        """Assemble OpenAI chunks without requiring the OpenAI-specific DONE marker."""
        values = [
            {"id": "completion-1", "choices": [{"index": 0, "delta": {"content": "hel"}, "finish_reason": None}]},
            {"id": "completion-1", "choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": "stop"}]},
        ]
        with tempfile.TemporaryDirectory() as directory:
            audit_log = AuditLog(directory)
            for delimiter in ("\n", "\r\n", "\r"):
                body = "".join(f"data: {json.dumps(value)}{delimiter}{delimiter}" for value in values).encode()
                with self.subTest(delimiter=repr(delimiter)):
                    event = audit_log.record_body_event("client_response", body, content_type="text/event-stream")
                    self.assertEqual(event["data"]["choices"][0]["message"]["content"], "hello")
                    self.assertEqual(event["data"]["choices"][0]["finish_reason"], "stop")
                    self.assertEqual(event["sse_chunk_count"], len(values))
                    self.assertEqual(event["body_format"], "sse")

    def test_sse_assembly_preserves_choices_usage_and_tool_call_arguments(self):
        """Reconstruct all standard OpenAI streaming fields rather than preserving chunk objects."""
        chunks = [
            {
                "id": "completion-1", "object": "chat.completion.chunk", "created": 4, "model": "model",
                "choices": [
                    {"index": 1, "delta": {"role": "assistant", "content": "B"}, "finish_reason": None},
                    {"index": 0, "delta": {"role": "assistant", "tool_calls": [{"index": 0, "id": "call-1", "type": "function", "function": {"name": "search", "arguments": '{"q":"'}}]}, "finish_reason": None},
                ],
            },
            {
                "id": "completion-1", "object": "chat.completion.chunk", "system_fingerprint": "fp",
                "choices": [
                    {"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'test"}'}}]}, "finish_reason": "tool_calls"},
                    {"index": 1, "delta": {"content": " answer"}, "finish_reason": "stop", "logprobs": {"content": []}},
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens_details": {"reasoning_tokens": 1}},
            },
        ]
        body = b"".join(b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks) + b"data: [DONE]\n\n"
        with tempfile.TemporaryDirectory() as directory:
            event = AuditLog(directory).record_body_event("llm_response", body, content_type="text/event-stream")
        self.assertEqual(event["data"], {
            "id": "completion-1",
            "object": "chat.completion",
            "created": 4,
            "model": "model",
            "system_fingerprint": "fp",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "tool_calls": [{
                            "index": 0, "id": "call-1", "type": "function",
                            "function": {"name": "search", "arguments": '{"q":"test"}'},
                        }],
                    },
                    "finish_reason": "tool_calls",
                },
                {
                    "index": 1,
                    "message": {"role": "assistant", "content": "B answer"},
                    "logprobs": {"content": []},
                    "finish_reason": "stop",
                },
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens_details": {"reasoning_tokens": 1}},
        })
        self.assertEqual(event["sse_chunk_count"], 2)

    def test_invalid_or_error_bearing_sse_remains_exact_text(self):
        """Do not partially decode a stream that contains malformed JSON, errors, or incomplete events."""
        prefix = 'data: {"ok":true}\n\n'
        bodies = [
            prefix + 'data: {broken}\n\n',
            prefix + 'data: {"error":{"message":"failed"}}\n\n',
            prefix + 'data: {"type":"error","message":"failed"}\n\n',
            prefix + 'event: error\ndata: {"message":"failed"}\n\n',
            prefix + 'data: {"value":NaN}\n\n',
            prefix + 'data: {"value":Infinity}\n\n',
            prefix + 'data: {"incomplete":true}',
            prefix + 'data: {"incomplete":true}\n',
            prefix + 'data: [DONE]\n\ndata: {"too_late":true}\n\n',
            prefix + 'data: [DONE]\n\ndata: [DONE]\n\n',
            '{"not":"SSE"}',
        ]
        with tempfile.TemporaryDirectory() as directory:
            audit_log = AuditLog(directory)
            for body in bodies:
                with self.subTest(body=body):
                    event = audit_log.record_body_event("llm_response", body.encode(), content_type="text/event-stream")
                    self.assertEqual(event["data"], body)
                    self.assertEqual(event["data_encoding"], "utf-8")
                    self.assertEqual(event["body_format"], "sse")
                    self.assertNotIn("sse_chunk_count", event)

    def test_failed_and_cancelled_sse_stays_raw_even_with_valid_json_events(self):
        """Keep interrupted or failed stream evidence raw rather than making it look successful."""
        body = b'data: {"content":"partial"}\n\n'
        with tempfile.TemporaryDirectory() as directory:
            audit_log = AuditLog(directory)
            for status in ("failed", "cancelled", "invalid_json", "http_error"):
                with self.subTest(status=status):
                    event = audit_log.record_body_event("llm_response", body, content_type="text/event-stream", terminal_status=status)
                    self.assertEqual(event["data"], body.decode())
                    self.assertEqual(event["data_encoding"], "utf-8")
                    self.assertEqual(event["body_format"], "sse")
            for fields in ({"terminal_status": "ok", "error_code": "UPSTREAM_ERROR"}, {"http": {"status_code": 400}}):
                with self.subTest(fields=fields):
                    event = audit_log.record_body_event("llm_response", body, content_type="text/event-stream", **fields)
                    self.assertEqual(event["data"], body.decode())
                    self.assertEqual(event["data_encoding"], "utf-8")
            running = audit_log.record_body_event(
                "client_response",
                b'data: {"id":"completion-1","choices":[{"index":0,"delta":{"content":"complete"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
                content_type="text/event-stream",
                terminal_status="running",
            )
            self.assertEqual(running["data"]["choices"][0]["message"]["content"], "complete")
            self.assertEqual(running["data_encoding"], "json")

    def test_large_completed_sse_keeps_raw_file_and_decoded_json(self):
        """Expose decoded SSE data even above the inline threshold while retaining original bytes."""
        values = [
            {"id": "completion-1", "choices": [{"index": 0, "delta": {"content": "x" * 100}, "finish_reason": None}]},
            {"id": "completion-1", "choices": [{"index": 0, "delta": {"content": "y" * 100}, "finish_reason": "stop"}]},
        ]
        body = b"".join(b"data: " + json.dumps(value).encode() + b"\n\n" for value in values) + b"data: [DONE]\n\n"
        with tempfile.TemporaryDirectory() as directory:
            audit_log = AuditLog(directory, max_inline_body_bytes=8)
            spool = audit_log.open_body_spool()
            spool.write(body)
            event = audit_log.finalize_body_spool("client_response", spool, content_type="text/event-stream", terminal_status="completed")
            self.assertEqual(event["data"]["choices"][0]["message"]["content"], "x" * 100 + "y" * 100)
            self.assertEqual(event["data_encoding"], "json")
            self.assertEqual(event["body_format"], "sse")
            self.assertEqual(event["sse_chunk_count"], 2)
            self.assertEqual(event["body_storage"], "file")
            self.assertEqual((audit_log.directory / event["body_file"]["relative_path"]).read_bytes(), body)
            self.assertEqual(audit_log.find_body_file(event["body_file"]["file_id"])["event_id"], event["event_id"])
            self.assertFalse(spool.exists())
            failed_spool = audit_log.open_body_spool()
            failed_spool.write(body)
            failed = audit_log.finalize_body_spool("llm_response", failed_spool, content_type="text/event-stream", terminal_status="cancelled")
            self.assertNotIn("data", failed)
            self.assertNotIn("sse_chunk_count", failed)
            self.assertEqual(failed["body_format"], "sse")
            self.assertEqual((audit_log.directory / failed["body_file"]["relative_path"]).read_bytes(), body)

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

    def test_concurrent_terminal_writes_append_one_task_record(self):
        """Reserve a task terminal record before concurrent writers can append duplicates."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            audit_log = AuditLog(temporary_directory)
            barrier = threading.Barrier(8)
            events: list[dict] = []

            def write_terminal() -> None:
                barrier.wait()
                events.append(audit_log.record_task_terminal(task_id="task-1", status="completed", task={}, raw_event_refs={}))

            writers = [threading.Thread(target=write_terminal) for _ in range(8)]
            for writer in writers:
                writer.start()
            for writer in writers:
                writer.join(timeout=2)
                self.assertFalse(writer.is_alive())

            journals = list(audit_log.directory.glob("tasks-*.jsonl"))
            records = [json.loads(line) for journal in journals for line in journal.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual({event["event_id"] for event in events}, {records[0]["event_id"]})

            audit_log.rebuild_index()
            restarted = AuditLog(temporary_directory)
            self.assertEqual(restarted.record_task_terminal(task_id="task-1", status="failed", task={}, raw_event_refs={})["event_id"], records[0]["event_id"])

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
            stale.close()
            recent.close()
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

    def test_terminal_record_waits_for_confirmed_task_event_receipts(self):
        """Reference accepted queued events only after the writer has persisted them."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            audit_log = AuditLog(temporary_directory)
            queue_manager = QueueManager(None, audit_log=audit_log)
            task = Task(id="task-1", type="agent")
            task.status = STATUS_COMPLETED
            started = threading.Event()
            release = threading.Event()
            finished = threading.Event()
            original_append = audit_log._append_record_sync

            def blocked_append(*args, **kwargs):
                started.set()
                release.wait(timeout=2)
                return original_append(*args, **kwargs)

            audit_log.start_writer(queue_size=4, overflow_policy="best_effort")
            try:
                with patch.object(audit_log, "_append_record_sync", side_effect=blocked_append):
                    audit_log.record_client_request(task_id=task.id, data={"sequence": 1})
                    self.assertTrue(started.wait(timeout=1))
                    audit_log.record_llm_response(task_id=task.id, data={"sequence": 2})
                    terminal_thread = threading.Thread(target=lambda: (queue_manager._record_terminal_audit(task), finished.set()))
                    terminal_thread.start()
                    self.assertFalse(finished.wait(timeout=0.05))
                    release.set()
                    terminal_thread.join(timeout=2)
                    self.assertTrue(finished.is_set())

                audit_log.stop_writer()
                snapshot = audit_log.task_terminal_snapshot(task.id)
                self.assertEqual(len(snapshot["raw_event_refs"]["client_request"]), 1)
                self.assertEqual(len(snapshot["raw_event_refs"]["llm_responses"]), 1)
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

    def test_retention_health_is_visible_to_another_audit_log_instance(self):
        """Expose cron retention results through the Core-owned audit writer instance."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            retention_writer = AuditLog(temporary_directory)
            retention_writer.retain_partitions(max_total_bytes=10**9)

            core_writer = AuditLog(temporary_directory)
            health = core_writer.health()
            self.assertEqual(health["retention_status"], "ok")
            self.assertIsNotNone(health["last_retention_at"])
            self.assertEqual(health["disk_usage_bytes"], retention_writer.health()["disk_usage_bytes"])

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

    def test_forced_emergency_retention_stops_after_reaching_target(self):
        """Avoid deleting later closed partitions after one deletion meets the emergency target."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            audit_log = AuditLog(temporary_directory)
            today = datetime.now().astimezone().date()
            first_day = (today - timedelta(days=3)).isoformat()
            second_day = (today - timedelta(days=2)).isoformat()
            first_journal = audit_log.directory / f"raw_client_requests-{first_day}.jsonl"
            second_journal = audit_log.directory / f"raw_client_requests-{second_day}.jsonl"
            first_journal.write_bytes(b"first\n")
            second_journal.write_bytes(b"second\n")

            with patch.object(audit_log, "disk_usage_bytes", side_effect=[100, 50]), \
                 patch.object(audit_log, "_partition_usage_bytes", return_value=50):
                removed = audit_log.retain_partitions(
                    retention_days=365,
                    max_total_bytes=50,
                    emergency_max_total_bytes=50,
                    force_emergency=True,
                )

            self.assertEqual(removed, [first_day])
            self.assertFalse(first_journal.exists())
            self.assertTrue(second_journal.exists())

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