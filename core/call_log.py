"""
Call log utility.
Saves full LLM request/response pairs to logs/<worker_id>_call_log.jsonl.
Can also append raw upstream request/response bodies to logs/<worker_id>_call_raw_log.jsonl.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from core.config import config

_LOGS_DIR = Path(__file__).parent.parent / "logs"


def _append_jsonl_record(path: Path, entry: dict) -> None:
    """Append one JSON object as a single JSONL line."""
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _append_raw_record(path: Path, body: bytes | str) -> None:
    """Append raw body bytes followed only by a trailing newline."""
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    data = body.encode("utf-8") if isinstance(body, str) else body
    with path.open("ab") as f:
        f.write(data)
        f.write(b"\n")


def _resolve_logging_tz():
    """Resolve timezone from logging.timezone config."""
    raw = config.get("logging.timezone", "local")
    name = str(raw or "local").strip()

    if not name:
        return datetime.now().astimezone().tzinfo or timezone.utc

    lowered = name.lower()
    if lowered in ("local", "system"):
        return datetime.now().astimezone().tzinfo or timezone.utc
    if lowered in ("utc", "gmt", "z"):
        return timezone.utc

    try:
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def save_llm_call(worker_id: str, task_id: str, request: dict, response: dict) -> None:
    """Append one LLM call record to logs/<worker_id>_call_log.jsonl."""
    tzinfo = _resolve_logging_tz()
    entry = {
        "ts": datetime.now(tzinfo).isoformat(timespec="milliseconds"),
        "task_id": task_id,
        "request": request,
        "response": response,
    }
    path = _LOGS_DIR / f"{worker_id}_call_log.jsonl"
    _append_jsonl_record(path, entry)


def save_llm_raw_call(worker_id: str, body: bytes | str) -> None:
    """Append one raw request or response body exactly as received, plus newline."""
    path = _LOGS_DIR / f"{worker_id}_call_raw_log.jsonl"
    _append_raw_record(path, body)
