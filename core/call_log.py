"""
Call log utility.
Saves full LLM request/response pairs to logs/<worker_id>_call_log.jsonl.
Each line is one JSON record.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from core.config import config

_LOGS_DIR = Path(__file__).parent.parent / "logs"


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
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    tzinfo = _resolve_logging_tz()
    entry = {
        "ts": datetime.now(tzinfo).isoformat(timespec="milliseconds"),
        "task_id": task_id,
        "request": request,
        "response": response,
    }
    path = _LOGS_DIR / f"{worker_id}_call_log.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
