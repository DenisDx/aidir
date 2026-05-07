"""
Call log utility.
Saves full LLM request/response pairs to logs/<worker_id>_call_log.jsonl.
Each line is one JSON record.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

_LOGS_DIR = Path(__file__).parent.parent / "logs"


def save_llm_call(worker_id: str, task_id: str, request: dict, response: dict) -> None:
    """Append one LLM call record to logs/<worker_id>_call_log.jsonl."""
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "task_id": task_id,
        "request": request,
        "response": response,
    }
    path = _LOGS_DIR / f"{worker_id}_call_log.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
