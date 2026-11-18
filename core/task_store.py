"""Task persistence layout: small metadata in a hash, large bodies on their own key.

Every task is still addressed by ``<instance>:task:<id>``. The hash keeps the
small fields the dashboard, the queue and cron read. The four fields that carry
request/response bodies are moved to ``<instance>:taskbody:<id>`` as one
compressed string value. Separating them serves two purposes:

* a body is a single string value, so an SSD-tiering data store (Dragonfly) can
  move it to disk while the keys and the metadata stay in RAM;
* the JSON bodies compress well, which reduces both RAM and disk usage.

The layout is backward compatible: a hash without the ``body_format`` marker is
read as the legacy inline layout, so tasks written before this module still
work and are converted by :func:`offload_terminal_task_bodies` on the next pass.
"""
from __future__ import annotations

import base64
import json
import zlib
from typing import Any, Iterable

from core import log

# Fields that carry request/response bodies. Everything else stays in the hash.
BODY_FIELDS: tuple[str, ...] = (
    "payload",
    "result",
    "upstream_response",
    "llm_call_history",
)

# Marker stored in the metadata hash. Empty/absent means the legacy inline
# layout; "0" means the task has no body; the other values select the codec.
BODY_FORMAT_NONE = "0"
BODY_FORMAT_ZLIB = "z1"
BODY_FORMAT_JSON = "j1"

DEFAULT_COMPRESSION_LEVEL = 6


def task_key(namespace: str, task_id: str) -> str:
    """Redis key of the task metadata hash."""
    return f"{namespace}:task:{task_id}"


def body_key(namespace: str, task_id: str) -> str:
    """Redis key of the task body blob."""
    return f"{namespace}:taskbody:{task_id}"


def encode_body(fields: dict[str, str], *, level: int = DEFAULT_COMPRESSION_LEVEL) -> tuple[str, str]:
    """Serialize body fields into ``(marker, text)`` for one Redis string value.

    The bodies are already JSON strings, so they are embedded in a JSON object
    without another parse/encode round trip. ``zlib`` is used because it is part
    of the standard library and compresses chat transcripts several times.
    """
    document = {name: value for name, value in fields.items() if value not in (None, "")}
    if not document:
        return BODY_FORMAT_NONE, ""
    text = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    level = max(0, min(9, int(level)))
    if level > 0:
        compressed = zlib.compress(text.encode("utf-8"), level)
        encoded = base64.b64encode(compressed).decode("ascii")
        if len(encoded) < len(text):
            return BODY_FORMAT_ZLIB, encoded
    return BODY_FORMAT_JSON, text


def decode_body(marker: str, text: str) -> dict[str, str]:
    """Return body fields from a stored body value.

    An unknown marker or a damaged value yields an empty mapping: the task
    metadata stays readable and the body is reported as missing instead of
    breaking the whole task.
    """
    if not text:
        return {}
    try:
        if marker == BODY_FORMAT_ZLIB:
            raw = zlib.decompress(base64.b64decode(text.encode("ascii")))
            document = json.loads(raw.decode("utf-8"))
        elif marker == BODY_FORMAT_JSON:
            document = json.loads(text)
        else:
            return {}
    except Exception as exc:
        log("system", "warning", f"task body cannot be decoded ({marker}): {exc}")
        return {}
    if not isinstance(document, dict):
        return {}
    return {str(name): str(value) for name, value in document.items() if isinstance(value, str)}


def split_task_fields(full_hash: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Split a flat task hash into metadata and body fields."""
    metadata: dict[str, str] = {}
    body: dict[str, str] = {}
    for name, value in full_hash.items():
        if name in BODY_FIELDS:
            if value not in (None, ""):
                body[name] = str(value)
        else:
            metadata[name] = value
    return metadata, body


class TaskStore:
    """Read and write tasks in the split metadata/body layout."""

    def __init__(
        self,
        redis_client,
        namespace: str = "aidir",
        *,
        compression_level: int = DEFAULT_COMPRESSION_LEVEL,
        enabled: bool = True,
    ) -> None:
        self._redis = redis_client
        self._ns = namespace
        self._level = int(compression_level)
        self._enabled = bool(enabled)

    def task_key(self, task_id: str) -> str:
        """Redis key of one task metadata hash."""
        return task_key(self._ns, task_id)

    def body_key(self, task_id: str) -> str:
        """Redis key of one task body blob."""
        return body_key(self._ns, task_id)

    async def load(self, task_id: str) -> dict[str, str] | None:
        """Load one task, merging its body when the split layout is present."""
        try:
            raw = await self._redis.hgetall(self.task_key(task_id))
        except Exception as exc:
            log("system", "warning", f"task {task_id} cannot be read: {exc}")
            return None
        if not raw:
            return None
        marker = str(raw.get("body_format") or "")
        if marker and marker != BODY_FORMAT_NONE:
            try:
                blob = await self._redis.get(self.body_key(task_id))
            except Exception as exc:
                log("system", "warning", f"task {task_id} body cannot be read: {exc}")
                blob = None
            if blob:
                raw.update(decode_body(marker, blob))
        return raw

    async def save_terminal(self, task_id: str, full_hash: dict[str, str]) -> None:
        """Persist a terminal task: metadata in the hash, bodies in one blob.

        Terminal tasks are the accumulation that matters. Writing the split
        layout here keeps queued and running tasks in the legacy inline form,
        which the scheduler reads from memory anyway.
        """
        metadata, body = split_task_fields(full_hash)
        if not self._enabled:
            metadata["body_format"] = BODY_FORMAT_NONE
            pipe = self._redis.pipeline(transaction=False)
            pipe.hset(self.task_key(task_id), mapping=metadata)
            pipe.hdel(self.task_key(task_id), *BODY_FIELDS)
            pipe.delete(self.body_key(task_id))
            await pipe.execute()
            return

        marker, encoded = encode_body(body, level=self._level)
        metadata["body_format"] = marker
        pipe = self._redis.pipeline(transaction=False)
        pipe.hset(self.task_key(task_id), mapping=metadata)
        pipe.hdel(self.task_key(task_id), *BODY_FIELDS)
        if marker == BODY_FORMAT_NONE:
            pipe.delete(self.body_key(task_id))
        else:
            pipe.set(self.body_key(task_id), encoded)
        await pipe.execute()

    async def delete(self, task_id: str, *, pipe=None) -> None:
        """Delete both the metadata hash and the body blob.

        With a pipeline the deletion is only scheduled; the caller executes it
        together with its own commands.
        """
        if pipe is not None:
            pipe.delete(self.task_key(task_id), self.body_key(task_id))
            return
        await self._redis.delete(self.task_key(task_id), self.body_key(task_id))

    def reset_inline(self, task_id: str, *, pipe) -> None:
        """Schedule removal of a split body before a task is written inline again.

        A retried task goes back to the queued state and is serialized in full.
        Without this, the body marker and the old body blob would survive and
        shadow the fresh inline fields on the next read.
        """
        pipe.delete(self.body_key(task_id))
        pipe.hdel(self.task_key(task_id), "body_format")

    async def load_payloads(self, task_ids: Iterable[str]) -> dict[str, str]:
        """Return the raw payload JSON for each id, from the body or legacy hash.

        The WebUI list needs only the payload of the visible page, so this is
        one pipeline of two commands per task: the split body and the legacy
        inline field. Missing values come back as an empty string.
        """
        ids = [str(task_id) for task_id in task_ids if task_id]
        if not ids:
            return {}
        # One HMGET per task is enough for the legacy inline layout, which is
        # what most rows still use. Only tasks whose payload lives in the body
        # blob need a second read.
        pipe = self._redis.pipeline(transaction=False)
        for task_id in ids:
            pipe.hmget(self.task_key(task_id), "payload")
        try:
            rows = await pipe.execute()
        except Exception as exc:
            log("system", "warning", f"task payloads cannot be read: {exc}")
            return {task_id: "" for task_id in ids}

        payloads: dict[str, str] = {}
        missing: list[str] = []
        for index, task_id in enumerate(ids):
            row = rows[index] if index < len(rows) else None
            value = row[0] if isinstance(row, (list, tuple)) and row else None
            payloads[task_id] = value or ""
            if not value:
                missing.append(task_id)

        if missing:
            pipe = self._redis.pipeline(transaction=False)
            for task_id in missing:
                pipe.get(self.body_key(task_id))
            try:
                blobs = await pipe.execute()
            except Exception as exc:
                log("system", "warning", f"task bodies cannot be read: {exc}")
                blobs = []
            for index, task_id in enumerate(missing):
                blob = blobs[index] if index < len(blobs) else None
                if not blob:
                    continue
                marker = BODY_FORMAT_ZLIB if _looks_base64(blob) else BODY_FORMAT_JSON
                payloads[task_id] = decode_body(marker, blob).get("payload", "")
        return payloads


def _looks_base64(value: str) -> bool:
    """Cheap heuristic for the stored zlib+base64 body.

    Bodies written by this module are base64 text, while a plain JSON document
    starts with ``{``. This keeps the reader from fetching the marker for every
    list row.
    """
    return bool(value) and value[0] not in "{["


async def offload_terminal_task_bodies(
    redis_client,
    namespace: str = "aidir",
    *,
    limit: int = 500,
    compression_level: int = DEFAULT_COMPRESSION_LEVEL,
    enabled: bool = True,
) -> dict[str, int]:
    """Move inline bodies of terminal tasks to the split layout.

    The cursor is kept in Redis so a large backlog is drained across cron
    cycles instead of blocking one pass. A full pass restarts from the
    beginning; already converted tasks are skipped by their marker.
    """
    store = TaskStore(
        redis_client,
        namespace,
        compression_level=compression_level,
        enabled=enabled,
    )
    cursor_key = f"{namespace}:cron:body_offload_cursor"
    try:
        cursor = int(await redis_client.get(cursor_key) or 0)
    except Exception:
        cursor = 0

    processed = 0
    offloaded = 0
    skipped = 0
    while processed < limit:
        try:
            cursor, keys = await redis_client.scan(
                cursor, match=f"{namespace}:task:*", count=max(50, min(limit, 500))
            )
        except Exception as exc:
            log("system", "warning", f"body offload scan failed: {exc}")
            break
        for raw_key in keys:
            if processed >= limit:
                break
            key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
            task_id = key.rsplit(":", 1)[-1]
            try:
                status, marker = await redis_client.hmget(key, "status", "body_format")
                marker = str(marker or "")
                if marker or status not in ("completed", "failed", "canceled"):
                    skipped += 1
                    continue
                raw = await redis_client.hgetall(key)
                if not raw:
                    skipped += 1
                    continue
                await store.save_terminal(task_id, raw)
                offloaded += 1
            except Exception as exc:
                log("system", "warning", f"body offload skipped {key}: {exc}")
                skipped += 1
            processed += 1
        if cursor == 0:
            break

    try:
        if cursor:
            await redis_client.set(cursor_key, cursor)
        else:
            await redis_client.delete(cursor_key)
    except Exception:
        pass

    return {"processed": processed, "offloaded": offloaded, "skipped": skipped, "cursor": cursor}
