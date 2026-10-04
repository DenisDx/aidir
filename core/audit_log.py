"""Append-only audit journals with a rebuildable SQLite lookup index."""
from __future__ import annotations

import json
import base64
import binascii
import copy
import hashlib
import logging
import os
import queue
import re
import shutil
import sqlite3
import threading
import time
import uuid
from collections import deque
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

_ROOT = Path(__file__).parent.parent
_DEFAULT_DIRECTORY = _ROOT / "logs" / "audit"
_JOURNALS = {
    "client_request": "raw_client_requests",
    "client_response": "raw_client_responses",
    "llm_request": "raw_llm_requests",
    "llm_response": "raw_llm_responses",
    "rejected_request": "rejected_requests",
    "task": "tasks",
    "task_reconciliation": "tasks",
}
_SECRET_METADATA_KEYS = {"authorization", "cookie", "set-cookie", "token", "api_key", "api-key", "password", "secret"}
_DATA_URI_RE = re.compile(r"^data:((?:image|audio|video)/[a-z0-9.+-]+);base64,([a-z0-9+/=\s]+)$", re.IGNORECASE)


class AuditBodySpool:
    """Incrementally write one stream body with stable byte metadata."""

    def __init__(self, path: Path) -> None:
        """Open the temporary spool file at the given path."""
        self.path = path
        self._handle = path.open("wb")
        self._digest = hashlib.sha256()
        self.bytes = 0
        self.closed = False

    def write(self, data: bytes) -> None:
        """Append bytes and update the stream metadata."""
        if self.closed:
            raise ValueError("Audit spool is already closed")
        self._handle.write(data)
        self._digest.update(data)
        self.bytes += len(data)

    def close(self) -> None:
        """Flush and close the spool once before finalization."""
        if self.closed:
            return
        self._handle.flush()
        self._handle.close()
        self.closed = True

    @property
    def sha256(self) -> str:
        """Return the incremental digest for bytes written to the spool."""
        return self._digest.hexdigest()

    def open(self, *args, **kwargs):
        """Expose the underlying path during the endpoint migration window."""
        return self.path.open(*args, **kwargs)

    def exists(self) -> bool:
        """Return whether the temporary spool file still exists."""
        return self.path.exists()

    def __fspath__(self) -> str:
        """Provide a path-like interface for filesystem maintenance helpers."""
        return str(self.path)


class AuditLog:
    """Write daily audit JSONL journals and maintain a disposable event index."""

    def __init__(
        self,
        directory: str | Path | None = None,
        secret_metadata_keys: list[str] | None = None,
        emergency_max_total_bytes: int = 2147483648,
        max_inline_body_bytes: int = 1048576,
    ) -> None:
        """Create an audit writer rooted at the given directory or the default path."""
        self.directory = Path(directory) if directory is not None else _DEFAULT_DIRECTORY
        self._lock = threading.RLock()
        self._health = {
            "status": "ok", "write_failures": 0, "dropped_events": 0,
            "dropped_recent": 0, "last_drop_at": None, "last_error": None,
            "disk_usage_bytes": 0, "retention_status": "unknown", "last_retention_at": None,
        }
        self._emergency_max_total_bytes = max(0, int(emergency_max_total_bytes))
        self._max_inline_body_bytes = max(0, int(max_inline_body_bytes))
        self._drop_times: deque[float] = deque()
        self._writer_queue: queue.Queue | None = None
        self._writer_thread: threading.Thread | None = None
        self._overflow_policy = "best_effort"
        self._event_receipts: dict[str, dict[str, Any]] = {}
        self._terminal_records: dict[str, dict[str, Any]] = {}
        configured_keys = secret_metadata_keys if isinstance(secret_metadata_keys, list) else []
        self._secret_metadata_keys = _SECRET_METADATA_KEYS | {str(key).lower() for key in configured_keys}
        self._initialize_index()
        self._restore_terminal_reservations()

    def health(self) -> dict[str, Any]:
        """Return a compact audit-storage health snapshot for operational reporting."""
        persisted = self._load_retention_health()
        with self._lock:
            health = dict(self._health)
        health.update(persisted)
        return health

    def start_writer(self, queue_size: int = 256, overflow_policy: str = "best_effort") -> None:
        """Start the bounded background writer used by the running Core service."""
        policy = overflow_policy if overflow_policy in {"best_effort", "durable"} else "best_effort"
        with self._lock:
            if self._writer_thread is not None:
                return
            self._overflow_policy = policy
            self._writer_queue = queue.Queue(maxsize=max(1, int(queue_size)))
            self._writer_thread = threading.Thread(target=self._writer_loop, name="aidir-audit-writer", daemon=True)
            self._writer_thread.start()

    def stop_writer(self, timeout: float = 3.0) -> bool:
        """Request writer shutdown and wait only the configured bounded time."""
        with self._lock:
            writer_queue = self._writer_queue
            writer_thread = self._writer_thread
        if writer_queue is None or writer_thread is None:
            return True
        try:
            writer_queue.put(None, timeout=max(0.0, timeout))
        except queue.Full:
            return False
        writer_thread.join(timeout=max(0.0, timeout))
        if writer_thread.is_alive():
            return False
        with self._lock:
            self._writer_queue = None
            self._writer_thread = None
        return True

    def _writer_loop(self) -> None:
        """Persist queued records serially until shutdown requests a drain."""
        assert self._writer_queue is not None
        while True:
            item = self._writer_queue.get()
            try:
                if item is None:
                    return
                journal_stem, day, record, completion, failures, receipt = item
                try:
                    self._append_record_sync(journal_stem, day, record, receipt)
                except (OSError, sqlite3.Error) as exc:
                    self._record_failure(exc)
                    failures.append(exc)
                    self._complete_receipt(receipt, "failed")
                finally:
                    if completion is not None:
                        completion.set()
            finally:
                self._writer_queue.task_done()

    def record_client_request(self, **fields: Any) -> dict[str, Any]:
        """Append one client-request event and return its completed record."""
        return self.record_raw_event("client_request", **fields)

    def record_client_response(self, **fields: Any) -> dict[str, Any]:
        """Append one client-response event and return its completed record."""
        return self.record_raw_event("client_response", **fields)

    def record_llm_request(self, **fields: Any) -> dict[str, Any]:
        """Append one LLM-request event and return its completed record."""
        return self.record_raw_event("llm_request", **fields)

    def record_llm_response(self, **fields: Any) -> dict[str, Any]:
        """Append one LLM-response event and return its completed record."""
        return self.record_raw_event("llm_response", **fields)

    def record_rejected_request(self, **fields: Any) -> dict[str, Any]:
        """Append one compact pre-task rejection record and return it."""
        return self.record_raw_event("rejected_request", **fields)

    def record_task_terminal(self, **fields: Any) -> dict[str, Any]:
        """Append one terminal task record and return its completed record."""
        task_id = fields.get("task_id")
        if not isinstance(task_id, str):
            return self.record_raw_event("task", **fields)
        record = self._new_record("task", fields)
        with self._lock:
            existing = self._terminal_records.get(task_id) or self.find_task_terminal(task_id)
            if existing is not None:
                return existing
            self._terminal_records[task_id] = record
        try:
            return self._record_prepared_event(record)
        except Exception:
            with self._lock:
                if self._terminal_records.get(task_id) is record:
                    self._terminal_records.pop(task_id, None)
            raise

    def record_task_reconciliation(self, **fields: Any) -> dict[str, Any]:
        """Append late raw-event references for an already terminal task."""
        return self.record_raw_event("task_reconciliation", **fields)

    def list_task_events(self, task_id: str) -> list[dict[str, Any]]:
        """Return indexed event locations for a task in recorded order without bodies."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT event_id, task_id, event_type, journal_path, byte_offset, byte_length, recorded_at
                FROM events WHERE task_id = ? ORDER BY recorded_at, byte_offset""",
                (task_id,),
            ).fetchall()
        events = [
            {"event_id": row[0], "task_id": row[1], "type": row[2], "journal_path": row[3],
             "byte_offset": row[4], "byte_length": row[5], "recorded_at": row[6]}
            for row in rows
        ]
        return [event for event in events if self._partition_visible(event["journal_path"])]

    def recent_client_request_routes(self, limit: int = 1000) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Return distinct endpoint/model routes and task IDs from recent client requests."""
        bounded_limit = max(1, min(int(limit), 1000))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT event_id FROM events WHERE event_type = 'client_request'
                ORDER BY recorded_at DESC, byte_offset DESC LIMIT ?""",
                (bounded_limit,),
            ).fetchall()

        routes: dict[str, dict[str, Any]] = {}
        task_routes: dict[str, str] = {}
        for row in rows:
            event = self.read_event(row[0])
            if not isinstance(event, dict):
                continue
            body = event.get("data")
            endpoint = str(event.get("endpoint") or "").strip()
            model = str(body.get("model") or body.get("name") or "").strip() if isinstance(body, dict) else ""
            task_id = str(event.get("task_id") or "").strip()
            if not endpoint or not model or not task_id:
                continue
            value = f"{endpoint}\x1f{model}"
            route = routes.setdefault(value, {"value": value, "label": f"{model} | {endpoint}", "count": 0})
            route["count"] += 1
            task_routes.setdefault(task_id, value)

        return sorted(routes.values(), key=lambda route: route["label"].lower()), task_routes

    def confirmed_task_events(self, task_id: str) -> list[dict[str, Any]]:
        """Wait for accepted task writes and return only persisted event metadata."""
        with self._lock:
            receipts = [receipt for receipt in self._event_receipts.values() if receipt["task_id"] == task_id]
        for receipt in receipts:
            receipt["completed"].wait()

        events = {event["event_id"]: event for event in self.list_task_events(task_id)}
        with self._lock:
            for receipt in receipts:
                if receipt["status"] != "persisted":
                    continue
                record = receipt["record"]
                events.setdefault(
                    record["event_id"],
                    {
                        "event_id": record["event_id"],
                        "task_id": task_id,
                        "type": record["type"],
                        "recorded_at": record["recorded_at"],
                    },
                )
        return sorted(events.values(), key=lambda event: (event.get("recorded_at", ""), event["event_id"]))

    def find_task_terminal(self, task_id: str) -> dict[str, Any] | None:
        """Return the existing terminal task record when the task was already journaled."""
        for location in self.list_task_events(task_id):
            if location["type"] == "task":
                return self.read_event(location["event_id"])
        return None

    def task_terminal_snapshot(self, task_id: str) -> dict[str, Any] | None:
        """Return one terminal record with all append-only reference reconciliations applied."""
        terminal = self.find_task_terminal(task_id)
        if not isinstance(terminal, dict):
            return None
        references = terminal.get("raw_event_refs") if isinstance(terminal.get("raw_event_refs"), dict) else {}
        snapshot = {**terminal, "raw_event_refs": {key: list(value) for key, value in references.items() if isinstance(value, list)}}
        for location in self.list_task_events(task_id):
            if location["type"] != "task_reconciliation":
                continue
            reconciliation = self.read_event(location["event_id"])
            additions = reconciliation.get("raw_event_refs") if isinstance(reconciliation, dict) else {}
            if not isinstance(additions, dict):
                continue
            for group, event_ids in additions.items():
                if not isinstance(event_ids, list):
                    continue
                group_refs = snapshot["raw_event_refs"].setdefault(group, [])
                group_refs.extend(event_id for event_id in event_ids if event_id not in group_refs)
        return snapshot

    def find_body_file(self, file_id: str) -> dict[str, Any] | None:
        """Return one indexed audit event owning an opaque file ID."""
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT event_id FROM file_references WHERE file_id = ?", (file_id,)).fetchone()
        event = self.read_event(row[0]) if row is not None else None
        if not isinstance(event, dict):
            return None
        body_file = event.get("body_file")
        if isinstance(body_file, dict) and body_file.get("file_id") == file_id:
            return event
        for attachment in event.get("attachments", []):
            if isinstance(attachment, dict) and attachment.get("file_id") == file_id:
                return {**event, "body_file": attachment}
        return None

    def retain_partitions(
        self,
        retention_days: int = 30,
        max_total_bytes: int = 1073741824,
        emergency_max_total_bytes: int = 2147483648,
        *,
        reconcile: bool = True,
        force_emergency: bool = False,
    ) -> list[str]:
        """Delete eligible whole daily partitions and return their local-date names."""
        if reconcile:
            self.reconcile_partitions()
        today = datetime.now().astimezone().date()
        candidates: dict[str, list[Path]] = {}
        pattern = re.compile(r"-([0-9]{4}-[0-9]{2}-[0-9]{2})\.jsonl$")
        for path in self.directory.glob("*.jsonl"):
            match = pattern.search(path.name)
            if match:
                candidates.setdefault(match.group(1), []).append(path)
        removed: list[str] = []
        total = self.disk_usage_bytes()
        for day in sorted(candidates):
            partition_date = datetime.strptime(day, "%Y-%m-%d").date()
            age_eligible = (today - partition_date).days >= max(2, retention_days)
            quota_eligible = total > max_total_bytes and (today - partition_date).days >= 2
            emergency_eligible = (force_emergency or total > emergency_max_total_bytes) and partition_date < today
            if not age_eligible and not quota_eligible and not emergency_eligible:
                continue
            files = candidates[day]
            bytes_removed = self._partition_usage_bytes(day, files)
            self._set_partition_state(day, "deleting")
            self._delete_partition(day, files)
            total -= bytes_removed
            removed.append(day)
            if force_emergency and total <= emergency_max_total_bytes:
                break
        remaining = self.disk_usage_bytes()
        with self._lock:
            self._health["disk_usage_bytes"] = remaining
            self._health["retention_status"] = "error" if remaining > max_total_bytes else "ok"
            self._health["last_retention_at"] = datetime.now().astimezone().isoformat(timespec="milliseconds")
        self._persist_retention_health()
        return removed

    def _partition_usage_bytes(self, day: str, journals: list[Path]) -> int:
        """Return all bytes removed with one daily journal partition and its attachments."""
        attachment_directory = self.directory / "files" / day
        paths = list(journals)
        if attachment_directory.exists():
            paths.extend(path for path in attachment_directory.rglob("*") if path.is_file())
        return sum(path.stat().st_size for path in paths if path.exists())

    def reconcile_partitions(self, max_spool_age_seconds: int = 86400) -> list[str]:
        """Resume interrupted partition deletion and remove stale temporary spools."""
        self.directory.mkdir(parents=True, exist_ok=True)
        recovered: list[str] = []
        with closing(self._connect()) as connection:
            deleting = [row[0] for row in connection.execute("SELECT partition_date FROM partitions WHERE state = 'deleting'")]
        for day in deleting:
            files = list(self.directory.glob(f"*-{day}.jsonl"))
            self._delete_partition(day, files)
            recovered.append(day)
        self.cleanup_stale_spools(max_spool_age_seconds)
        return recovered

    def cleanup_stale_spools(self, max_age_seconds: int = 86400) -> int:
        """Remove abandoned spool files older than the configured recovery age."""
        cutoff = datetime.now().timestamp() - max(0, max_age_seconds)
        removed = 0
        spool_directory = self.directory / ".spool"
        for path in spool_directory.glob("*") if spool_directory.exists() else []:
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def disk_usage_bytes(self) -> int:
        """Return total audit-directory disk usage including files, index, and spools."""
        try:
            return sum(path.stat().st_size for path in self.directory.rglob("*") if path.is_file())
        except OSError:
            return 0

    @staticmethod
    def _partition_day_from_journal(journal_path: str) -> str | None:
        """Extract the local partition date encoded in one audit journal filename."""
        match = re.search(r"-([0-9]{4}-[0-9]{2}-[0-9]{2})\.jsonl$", journal_path)
        return match.group(1) if match else None

    def _partition_visible(self, journal_path: str) -> bool:
        """Return whether a journal's partition is visible to indexed lookup."""
        day = self._partition_day_from_journal(journal_path)
        if day is None:
            return True
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT state FROM partitions WHERE partition_date = ?", (day,)).fetchone()
        return row is None or row[0] != "deleting"

    def _set_partition_state(self, day: str, state: str) -> None:
        """Persist one audit partition lifecycle state before filesystem mutation."""
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT INTO partitions(partition_date, state) VALUES (?, ?) ON CONFLICT(partition_date) DO UPDATE SET state = excluded.state",
                (day, state),
            )
            connection.commit()

    def _delete_partition(self, day: str, files: list[Path]) -> None:
        """Remove one hidden partition's journals, attachments, and index rows."""
        for path in files:
            path.unlink(missing_ok=True)
        shutil.rmtree(self.directory / "files" / day, ignore_errors=True)
        with closing(self._connect()) as connection:
            connection.execute("DELETE FROM events WHERE journal_path LIKE ?", (f"%-{day}.jsonl",))
            connection.execute("UPDATE partitions SET state = 'deleted' WHERE partition_date = ?", (day,))
            connection.commit()

    def record_body_event(
        self,
        event_type: str,
        body: bytes,
        *,
        content_type: str = "",
        **fields: Any,
    ) -> dict[str, Any]:
        """Append an event while preserving a JSON, text, or file-backed body."""
        now = datetime.now().astimezone()
        body_fields = self._body_fields(
            body, content_type, now.date().isoformat(),
            normalize_sse=self._can_normalize_sse(fields),
        )
        return self.record_raw_event(event_type, **fields, **body_fields)

    def open_body_spool(self) -> AuditBodySpool:
        """Create one temporary audit spool path for incremental stream bytes."""
        file_id = str(uuid.uuid4())
        path = self.directory / ".spool" / file_id
        path.parent.mkdir(parents=True, exist_ok=True)
        return AuditBodySpool(path)

    def finalize_body_spool(self, event_type: str, spool: AuditBodySpool | Path, *, content_type: str, **fields: Any) -> dict[str, Any]:
        """Classify and store a completed spool using the normal body policy."""
        if isinstance(spool, AuditBodySpool):
            spool.close()
            spool_path = spool.path
            body_size = spool.bytes
            body_sha256 = spool.sha256
        else:
            spool_path = spool
            body_size, body_sha256 = self._file_metadata(spool_path)

        normalized_content_type = content_type.split(";", 1)[0].strip().lower()
        if body_size <= self._max_inline_body_bytes and not self._must_store_body_as_file(normalized_content_type):
            body = spool_path.read_bytes()
            body_fields = self._body_fields(
                body, content_type, datetime.now().astimezone().date().isoformat(),
                normalize_sse=self._can_normalize_sse(fields),
            )
            event = self.record_raw_event(event_type, **fields, **body_fields)
            spool_path.unlink(missing_ok=True)
            return event

        now = datetime.now().astimezone()
        file_id = str(uuid.uuid4())
        relative_path = Path("files") / now.date().isoformat() / file_id
        destination = self.directory / relative_path
        with self._lock:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(spool_path, destination)
        decoded_fields: dict[str, Any] = {}
        if normalized_content_type == "text/event-stream" and self._can_normalize_sse(fields):
            try:
                decoded = destination.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                decoded = None
            if decoded is not None:
                decoded_fields = self._sse_json_fields(decoded, now.date().isoformat())
        return self.record_raw_event(
            event_type,
            **fields,
            body_storage="file",
            body_file={
                "file_id": file_id,
                "relative_path": str(relative_path),
                "content_type": content_type,
                "bytes": body_size,
                "sha256": body_sha256,
            },
            body_bytes=body_size,
            body_sha256=body_sha256,
            body_format=self._body_format_for_content_type(normalized_content_type),
            **decoded_fields,
        )

    @staticmethod
    def _file_metadata(path: Path) -> tuple[int, str]:
        """Return byte length and SHA-256 for a file without loading it wholly."""
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return path.stat().st_size, digest.hexdigest()

    def record_raw_event(self, event_type: str, **fields: Any) -> dict[str, Any]:
        """Append one event of the given type and index its JSONL byte range."""
        return self._record_prepared_event(self._new_record(event_type, fields))

    def _new_record(self, event_type: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        """Build one redacted audit record before it enters the writer."""
        if event_type not in _JOURNALS:
            raise ValueError(f"Unsupported audit event type: {event_type}")

        now = datetime.now().astimezone()
        record = {
            "schema_version": 1,
            "event_id": str(uuid.uuid4()),
            "recorded_at": now.isoformat(timespec="milliseconds"),
            "type": event_type,
            **{
                key: value if key == "data" else self._redact_metadata(value)
                for key, value in fields.items()
            },
        }
        record.setdefault("task_id", None)
        return record

    def _record_prepared_event(self, record: dict[str, Any]) -> dict[str, Any]:
        """Register one receipt and submit its prepared record to the writer."""
        receipt = {
            "record": record,
            "task_id": record.get("task_id"),
            "status": "pending",
            "completed": threading.Event(),
        }
        with self._lock:
            self._event_receipts[record["event_id"]] = receipt
        try:
            return self._append_record(_JOURNALS[record["type"]], record["recorded_at"][:10], record, receipt)
        except Exception:
            self._complete_receipt(receipt, "failed")
            raise

    def _redact_metadata(self, value: Any) -> Any:
        """Redact credential-like metadata while leaving event bodies untouched."""
        if isinstance(value, dict):
            return {
                key: "[REDACTED]" if str(key).lower() in self._secret_metadata_keys else self._redact_metadata(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._redact_metadata(item) for item in value]
        return value

    def find_event(self, event_id: str) -> dict[str, Any] | None:
        """Return one indexed event's journal location or None when it is absent."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT event_id, task_id, event_type, journal_path, byte_offset,
                       byte_length, recorded_at
                FROM events WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        if not self._partition_visible(row[3]):
            return None
        return {
            "event_id": row[0],
            "task_id": row[1],
            "type": row[2],
            "journal_path": row[3],
            "byte_offset": row[4],
            "byte_length": row[5],
            "recorded_at": row[6],
        }

    def read_event(self, event_id: str) -> dict[str, Any] | None:
        """Read one indexed JSONL event by byte offset or return None when absent."""
        location = self.find_event(event_id)
        if location is None:
            return None
        path = self.directory / location["journal_path"]
        try:
            with path.open("rb") as handle:
                handle.seek(location["byte_offset"])
                encoded = handle.read(location["byte_length"])
            return json.loads(encoded)
        except (OSError, json.JSONDecodeError):
            return None

    def rebuild_index(self) -> int:
        """Rebuild the disposable SQLite index from all valid audit JSONL records."""
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as connection:
                connection.execute("DELETE FROM events")
                connection.execute("DELETE FROM file_references")
                rebuilt = 0
                for journal_path in sorted(self.directory.glob("*.jsonl")):
                    rebuilt += self._index_journal(connection, journal_path)
                connection.commit()
                self._restore_terminal_reservations()
                return rebuilt

    def _restore_terminal_reservations(self) -> None:
        """Restore terminal task reservations from append-only task journals."""
        restored: dict[str, dict[str, Any]] = {}
        for journal_path in sorted(self.directory.glob("tasks-*.jsonl")):
            try:
                with journal_path.open("rb") as handle:
                    for encoded in handle:
                        try:
                            record = json.loads(encoded)
                        except json.JSONDecodeError:
                            continue
                        task_id = record.get("task_id") if isinstance(record, dict) else None
                        if record.get("type") == "task" and isinstance(task_id, str):
                            restored.setdefault(task_id, record)
            except OSError:
                continue
        with self._lock:
            self._terminal_records.update(restored)

    def _append_record(self, journal_stem: str, day: str, record: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
        """Queue or synchronously write one completed JSONL record."""
        with self._lock:
            writer_queue = self._writer_queue
            policy = self._overflow_policy
        if writer_queue is None:
            try:
                return self._append_record_sync(journal_stem, day, record, receipt)
            except (OSError, sqlite3.Error) as exc:
                self._record_failure(exc)
                self._complete_receipt(receipt, "failed")
                raise

        completion = threading.Event() if policy == "durable" else None
        failures: list[BaseException] = []
        item = (journal_stem, day, record, completion, failures, receipt)
        try:
            if policy == "durable":
                writer_queue.put(item)
                completion.wait()
                if failures:
                    raise failures[0]
            else:
                writer_queue.put_nowait(item)
        except queue.Full:
            self._record_drop(receipt)
        return record

    def _append_record_sync(self, journal_stem: str, day: str, record: dict[str, Any], receipt: dict[str, Any] | None = None) -> dict[str, Any]:
        """Write one completed JSONL record and commit its index entry afterwards."""
        encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        journal_path = self.directory / f"{journal_stem}-{day}.jsonl"
        relative_path = journal_path.name

        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            with journal_path.open("ab") as handle:
                offset = handle.tell()
                handle.write(encoded)
                handle.flush()

            with closing(self._connect()) as connection:
                self._insert_index(connection, record, relative_path, offset, len(encoded))
                connection.commit()
        if receipt is not None:
            self._complete_receipt(receipt, "persisted")
        return record

    def _complete_receipt(self, receipt: dict[str, Any], status: str) -> None:
        """Publish a final writer outcome for one submitted audit event."""
        with self._lock:
            receipt["status"] = status
            task_id = receipt["record"].get("task_id")
            if receipt["record"].get("type") == "task" and status != "persisted" and isinstance(task_id, str):
                if self._terminal_records.get(task_id) is receipt["record"]:
                    self._terminal_records.pop(task_id, None)
            receipt["completed"].set()

    def _record_drop(self, receipt: dict[str, Any]) -> None:
        """Record a best-effort queue overflow without delaying task execution."""
        now = time.monotonic()
        with self._lock:
            self._drop_times.append(now)
            while self._drop_times and now - self._drop_times[0] > 300:
                self._drop_times.popleft()
            self._health["dropped_events"] += 1
            self._health["dropped_recent"] = len(self._drop_times)
            self._health["last_drop_at"] = datetime.now().astimezone().isoformat(timespec="milliseconds")
            self._health["status"] = "error" if len(self._drop_times) >= 10 else "warning"
        logging.getLogger(__name__).warning("Audit writer queue overflow; event was dropped")
        self._complete_receipt(receipt, "dropped")

    def _record_failure(self, exc: BaseException) -> None:
        """Mark audit storage unhealthy after an I/O or SQLite writer failure."""
        with self._lock:
            self._health["status"] = "error"
            self._health["write_failures"] += 1
            self._health["last_error"] = f"{exc.__class__.__name__}: {exc}"
        if isinstance(exc, OSError) and exc.errno == getattr(os, "ENOSPC", 28):
            try:
                self.retain_partitions(
                    max_total_bytes=self._emergency_max_total_bytes,
                    emergency_max_total_bytes=self._emergency_max_total_bytes,
                    force_emergency=True,
                )
            except OSError:
                pass

    def _body_fields(self, body: bytes, content_type: str, day: str, *, normalize_sse: bool = True) -> dict[str, Any]:
        """Return audit fields preserving JSON, UTF-8 text, or binary data exactly."""
        fields = {
            "body_bytes": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest(),
        }
        normalized_content_type = content_type.split(";", 1)[0].strip().lower()
        body_format = self._body_format_for_content_type(normalized_content_type)
        if self._must_store_body_as_file(normalized_content_type):
            return fields | {"body_format": body_format} | self._store_body_file(body, normalized_content_type, day)

        try:
            decoded = body.decode("utf-8")
        except UnicodeDecodeError:
            return fields | {"body_format": "binary"} | self._store_body_file(body, normalized_content_type, day)

        if normalized_content_type == "text/event-stream":
            sse_fields = self._sse_json_fields(decoded, day) if normalize_sse else {}
            if sse_fields:
                return fields | {"body_storage": "inline", "body_format": "sse"} | sse_fields
            return fields | {
                "body_storage": "inline",
                "data": decoded,
                "data_encoding": "utf-8",
                "body_format": "sse",
            }

        try:
            data = json.loads(decoded)
        except json.JSONDecodeError:
            return fields | {
                "body_storage": "inline",
                "data": decoded,
                "data_encoding": "utf-8",
                "body_format": body_format,
            }
        attachments = self._extract_data_uri_attachments(data, day)
        return fields | {
            "body_storage": "inline",
            "data": data,
            "data_encoding": "json",
            "body_format": "json",
            **({"attachments": attachments} if attachments else {}),
        }

    @staticmethod
    def _body_format_for_content_type(content_type: str) -> str:
        """Classify a stored body without inspecting or changing its data."""
        if content_type == "text/event-stream":
            return "sse"
        if content_type == "application/json" or content_type.endswith("+json"):
            return "json"
        if content_type.startswith(("image/", "audio/", "video/")) or content_type in {
            "application/octet-stream",
            "application/pdf",
        }:
            return "binary"
        return "text"

    @staticmethod
    def _can_normalize_sse(fields: Mapping[str, Any]) -> bool:
        """Allow structured SSE only for responses without a terminal failure."""
        http = fields.get("http")
        status_code = http.get("status_code") if isinstance(http, Mapping) else None
        http_error = isinstance(status_code, int) and status_code >= 400
        terminal_status = str(fields.get("terminal_status") or "").lower()
        return terminal_status not in {
            "failed", "cancelled", "timeout", "http_error", "invalid_json", "exception",
        } and not fields.get("error_code") and not http_error

    def _sse_json_fields(self, body: str, day: str) -> dict[str, Any]:
        """Return decoded JSON audit fields only when the entire SSE stream is valid."""
        parsed, payloads = self._parse_json_sse(body)
        if not parsed:
            return {}
        data = self._assemble_sse_response(payloads)
        if data is None:
            return {}
        attachments = self._extract_data_uri_attachments(data, day)
        return {
            "data": data,
            "data_encoding": "json",
            "sse_chunk_count": len(payloads),
            **({"attachments": attachments} if attachments else {}),
        }

    @staticmethod
    def _merge_stream_value(current: Any, incoming: Any, *, concatenate: bool = False) -> Any:
        """Combine a streamed value while preserving the final executor semantics."""
        if incoming is None:
            return current
        if concatenate and isinstance(current, str) and isinstance(incoming, str):
            return current + incoming
        if isinstance(current, dict) and isinstance(incoming, dict):
            merged = dict(current)
            for key, value in incoming.items():
                merged[key] = AuditLog._merge_stream_value(merged.get(key), value)
            return merged
        return copy.deepcopy(incoming)

    @classmethod
    def _assemble_sse_response(cls, payloads: list[Any]) -> dict[str, Any] | None:
        """Assemble valid OpenAI chat-completion SSE chunks into one final JSON response."""
        if len(payloads) == 1 and isinstance(payloads[0], dict):
            payload = payloads[0]
            if not isinstance(payload.get("choices"), list):
                return payload

        if not payloads or not all(isinstance(payload, dict) for payload in payloads):
            return None

        response: dict[str, Any] = {}
        choices: dict[int, dict[str, Any]] = {}
        saw_choice = False
        for payload in payloads:
            raw_choices = payload.get("choices")
            if raw_choices is not None and not isinstance(raw_choices, list):
                return None
            if raw_choices is not None:
                for raw_choice in raw_choices:
                    if not isinstance(raw_choice, dict) or not isinstance(raw_choice.get("index"), int):
                        return None
                    delta = raw_choice.get("delta")
                    if delta is not None and not isinstance(delta, dict):
                        return None
                    if delta is None and "message" in raw_choice:
                        return None
                    saw_choice = True
                    index = raw_choice["index"]
                    choice = choices.setdefault(index, {"index": index, "message": {}})
                    message = choice["message"]
                    for key, value in (delta or {}).items():
                        if key in {"content", "reasoning", "reasoning_content"}:
                            message[key] = cls._merge_stream_value(message.get(key, ""), value, concatenate=True)
                        elif key == "tool_calls":
                            if not isinstance(value, list):
                                return None
                            message[key] = cls._merge_tool_call_deltas(message.get(key), value)
                            if message[key] is None:
                                return None
                        else:
                            message[key] = cls._merge_stream_value(message.get(key), value)
                    for key, value in raw_choice.items():
                        if key not in {"index", "delta", "finish_reason"} and value is not None:
                            choice[key] = cls._merge_stream_value(choice.get(key), value)
                    if raw_choice.get("finish_reason") is not None:
                        choice["finish_reason"] = raw_choice["finish_reason"]

            for key, value in payload.items():
                if key in {"choices", "usage"} or value is None:
                    continue
                response[key] = cls._merge_stream_value(response.get(key), value)
            if isinstance(payload.get("usage"), dict):
                response["usage"] = cls._merge_stream_value(response.get("usage", {}), payload["usage"])

        if not saw_choice:
            return None
        response["object"] = "chat.completion"
        response["choices"] = [choices[index] for index in sorted(choices)]
        return response

    @classmethod
    def _merge_tool_call_deltas(cls, current: Any, incoming: list[Any]) -> list[dict[str, Any]] | None:
        """Join OpenAI tool-call deltas by their stream index."""
        calls = copy.deepcopy(current) if isinstance(current, list) else []
        if not all(isinstance(call, dict) for call in calls):
            return None
        for raw_call in incoming:
            if not isinstance(raw_call, dict) or not isinstance(raw_call.get("index"), int):
                return None
            index = raw_call["index"]
            while len(calls) <= index:
                calls.append({"index": len(calls), "function": {}})
            call = calls[index]
            for key, value in raw_call.items():
                if key == "function":
                    if not isinstance(value, dict):
                        return None
                    function = call.setdefault("function", {})
                    if not isinstance(function, dict):
                        return None
                    for function_key, function_value in value.items():
                        function[function_key] = cls._merge_stream_value(
                            function.get(function_key, ""),
                            function_value,
                            concatenate=function_key == "arguments",
                        )
                elif key != "index":
                    call[key] = cls._merge_stream_value(call.get(key), value)
        return calls

    @staticmethod
    def _reject_json_constant(value: str) -> None:
        """Reject non-standard JSON constants rather than recording invalid structured JSON."""
        raise ValueError(f"Invalid JSON constant: {value}")

    @classmethod
    def _parse_json_sse(cls, body: str) -> tuple[bool, list[Any]]:
        """Decode all complete SSE JSON payloads, rejecting malformed or error-bearing streams."""
        payloads: list[Any] = []
        terminated = False
        data_lines: list[str] = []
        event_type = ""
        lines = re.split(r"\r\n|\r|\n", body.removeprefix("\ufeff"))
        if lines[-1] == "":
            lines.pop()
        for line in lines:
            if line:
                if line.startswith(":"):
                    continue
                name, separator, value = line.partition(":")
                if separator and value.startswith(" "):
                    value = value[1:]
                if name == "data":
                    data_lines.append(value)
                elif name == "event":
                    event_type = value
                continue
            if event_type.lower() == "error":
                return False, []
            if not data_lines:
                event_type = ""
                continue
            payload = "\n".join(data_lines)
            data_lines = []
            event_type = ""
            if payload == "[DONE]":
                if terminated:
                    return False, []
                terminated = True
                continue
            if terminated:
                return False, []
            try:
                data = json.loads(payload, parse_constant=cls._reject_json_constant)
            except ValueError:
                return False, []
            if isinstance(data, dict) and ("error" in data or data.get("type") == "error"):
                return False, []
            payloads.append(data)
        if data_lines or event_type or not payloads:
            return False, []
        return True, payloads

    def _extract_data_uri_attachments(self, value: Any, day: str) -> list[dict[str, Any]]:
        """Store recognized media data URIs as attachments while preserving the source JSON value."""
        attachments: list[dict[str, Any]] = []

        def _visit(item: Any) -> None:
            if isinstance(item, dict):
                for nested in item.values():
                    _visit(nested)
                return
            if isinstance(item, list):
                for nested in item:
                    _visit(nested)
                return
            if not isinstance(item, str):
                return
            match = _DATA_URI_RE.match(item)
            if match is None:
                return
            try:
                decoded = base64.b64decode(match.group(2), validate=True)
            except (ValueError, binascii.Error):
                return
            attachments.append(self._store_body_file(decoded, match.group(1).lower(), day)["body_file"])

        _visit(value)
        return attachments

    @staticmethod
    def _must_store_body_as_file(content_type: str) -> bool:
        """Return whether the declared media type requires file-backed storage."""
        return content_type.startswith(("image/", "audio/", "video/")) or content_type in {
            "application/octet-stream",
            "application/pdf",
        }

    def _store_body_file(self, body: bytes, content_type: str, day: str) -> dict[str, Any]:
        """Store one opaque body under its local-date partition and return its reference."""
        file_id = str(uuid.uuid4())
        relative_path = Path("files") / day / file_id
        path = self.directory / relative_path
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        return {
            "body_storage": "file",
            "body_file": {
                "file_id": file_id,
                "relative_path": str(relative_path),
                "content_type": content_type or "application/octet-stream",
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            },
        }

    def _initialize_index(self) -> None:
        """Create the SQLite index schema when it does not already exist."""
        self.directory.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT,
                    event_type TEXT NOT NULL,
                    file_id TEXT,
                    journal_path TEXT NOT NULL,
                    byte_offset INTEGER NOT NULL,
                    byte_length INTEGER NOT NULL,
                    recorded_at TEXT NOT NULL
                )
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(events)")}
            if "file_id" not in columns:
                connection.execute("ALTER TABLE events ADD COLUMN file_id TEXT")
            connection.execute("CREATE INDEX IF NOT EXISTS events_task_id_idx ON events(task_id)")
            connection.execute("CREATE INDEX IF NOT EXISTS events_file_id_idx ON events(file_id)")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS file_references (file_id TEXT PRIMARY KEY, event_id TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS partitions (partition_date TEXT PRIMARY KEY, state TEXT NOT NULL)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_health (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    disk_usage_bytes INTEGER NOT NULL,
                    retention_status TEXT NOT NULL,
                    last_retention_at TEXT
                )
                """
            )
            connection.commit()

    def _load_retention_health(self) -> dict[str, Any]:
        """Read the process-independent retention state from the audit sidecar."""
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT disk_usage_bytes, retention_status, last_retention_at FROM audit_health WHERE id = 1"
                ).fetchone()
        except sqlite3.Error:
            return {}
        if row is None:
            return {}
        return {
            "disk_usage_bytes": int(row[0]),
            "retention_status": row[1],
            "last_retention_at": row[2],
        }

    def _persist_retention_health(self) -> None:
        """Store the latest retention result for cron and Core status readers."""
        with self._lock:
            values = (
                int(self._health["disk_usage_bytes"]),
                str(self._health["retention_status"]),
                self._health["last_retention_at"],
            )
        try:
            with closing(self._connect()) as connection:
                connection.execute(
                    """
                    INSERT INTO audit_health(id, disk_usage_bytes, retention_status, last_retention_at)
                    VALUES (1, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        disk_usage_bytes = excluded.disk_usage_bytes,
                        retention_status = excluded.retention_status,
                        last_retention_at = excluded.last_retention_at
                    """,
                    values,
                )
                connection.commit()
        except sqlite3.Error as exc:
            self._record_failure(exc)

    def _connect(self) -> sqlite3.Connection:
        """Open one short-lived SQLite connection for an index operation."""
        connection = sqlite3.connect(self.directory / "audit-index.sqlite")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _index_journal(self, connection: sqlite3.Connection, journal_path: Path) -> int:
        """Index valid records in one JSONL file and return their count."""
        count = 0
        with journal_path.open("rb") as handle:
            while True:
                offset = handle.tell()
                encoded = handle.readline()
                if not encoded:
                    return count
                try:
                    record = json.loads(encoded)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, Mapping) or not isinstance(record.get("event_id"), str):
                    continue
                self._insert_index(connection, record, journal_path.name, offset, len(encoded))
                count += 1

    @staticmethod
    def _insert_index(
        connection: sqlite3.Connection,
        record: Mapping[str, Any],
        journal_path: str,
        byte_offset: int,
        byte_length: int,
    ) -> None:
        """Insert or replace one event-location row in the SQLite index."""
        connection.execute(
            """
            INSERT OR REPLACE INTO events(
                event_id, task_id, event_type, file_id, journal_path, byte_offset,
                byte_length, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["event_id"],
                record.get("task_id"),
                record.get("type", "unknown"),
                (record.get("body_file") or {}).get("file_id") if isinstance(record.get("body_file"), Mapping) else None,
                journal_path,
                byte_offset,
                byte_length,
                record.get("recorded_at", ""),
            ),
        )
        connection.execute("DELETE FROM file_references WHERE event_id = ?", (record["event_id"],))
        files = []
        body_file = record.get("body_file")
        if isinstance(body_file, Mapping):
            files.append(body_file)
        attachments = record.get("attachments")
        if isinstance(attachments, list):
            files.extend(item for item in attachments if isinstance(item, Mapping))
        for file_ref in files:
            file_id = file_ref.get("file_id")
            if isinstance(file_id, str):
                connection.execute(
                    "INSERT OR REPLACE INTO file_references(file_id, event_id) VALUES (?, ?)",
                    (file_id, record["event_id"]),
                )