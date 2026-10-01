"""
Call log utility for compact operational LLM summaries.
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
    """Append one compact LLM operational summary without raw request or response bodies."""
    tzinfo = _resolve_logging_tz()
    entry = {
        "ts": datetime.now(tzinfo).isoformat(timespec="milliseconds"),
        "task_id": task_id,
        "model": request.get("model") if isinstance(request, dict) else None,
        "stream": bool(request.get("stream")) if isinstance(request, dict) else False,
        "response_keys": sorted(response) if isinstance(response, dict) else [],
    }
    path = _LOGS_DIR / f"{worker_id}_call_log.jsonl"
    _append_jsonl_record(path, entry)


__all__ = ["save_llm_call"]
