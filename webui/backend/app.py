"""
WebUI backend (FastAPI + asyncio).
Provides:
  - POST /api/auth/login  / POST /api/auth/logout
  - GET  /api/tasks       – current task list
  - GET  /api/status      – system status summary
  - GET  /api/logs        – last N lines of a log file
  - WS   /ws/logs         – live log streaming over WebSocket
  - Static files served from ../frontend/ (for development; nginx serves in prod)
"""
from __future__ import annotations

import asyncio
import heapq
import hmac
import json
import secrets
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from fastapi import (
    Cookie, Depends, FastAPI, HTTPException, Request,
    Response, WebSocket, WebSocketDisconnect, status,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from core import log
from core.error_logging import attach_request_id_middleware, get_or_create_request_id, log_exception

if TYPE_CHECKING:
    from core.app import Core

_FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
_LOGS_DIR     = Path(__file__).parent.parent.parent / "logs"
_SESSION_PREFIX = "aidir:session:"
_ROOT = _LOGS_DIR.parent
_CRON_MARKER = "# aidir-cron"
_LOG_FILE_SUFFIXES = frozenset({".log", ".jsonl"})
_LIVE_LOG_INITIAL_LINES = 200
_LIVE_LOG_INITIAL_BYTES = 256 * 1024


class CronRepairError(RuntimeError):
    """Raised when the user crontab cannot be safely repaired."""


def _aidir_cron_line() -> str:
    """Return the canonical crontab entry for the aidir maintenance cycle."""
    python = shlex.quote(str(_ROOT / "venv" / "bin" / "python"))
    script = shlex.quote(str(_ROOT / "core" / "cron.py"))
    log_file = shlex.quote(str(_LOGS_DIR / "cron.log"))
    return f"* * * * * {python} {script} >> {log_file} 2>&1 {_CRON_MARKER}"


def _cron_error_message(result: subprocess.CompletedProcess[str]) -> str:
    """Return a concise diagnostic from a failed crontab command."""
    return (result.stderr or result.stdout or f"exit code {result.returncode}").strip()


def _repair_user_crontab() -> dict[str, str]:
    """Add or repair exactly one aidir cron entry without modifying unrelated lines."""
    try:
        current = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CronRepairError(f"Cannot read crontab: {exc}") from exc

    if current.returncode == 0:
        content = current.stdout or ""
    elif current.returncode == 1 and "no crontab for" in (current.stderr or "").lower():
        content = ""
    else:
        raise CronRepairError(f"Cannot read crontab: {_cron_error_message(current)}")

    lines = content.splitlines(keepends=True)
    marker_indexes = [
        index
        for index, line in enumerate(lines)
        if line.rstrip("\r\n").rstrip().endswith(_CRON_MARKER)
    ]
    if len(marker_indexes) > 1:
        raise CronRepairError("Cannot repair crontab: multiple aidir cron entries were found")

    canonical = _aidir_cron_line()
    action = "unchanged"
    if marker_indexes:
        index = marker_indexes[0]
        existing = lines[index].rstrip("\r\n")
        if existing.strip() != canonical:
            newline = "\r\n" if lines[index].endswith("\r\n") else "\n"
            lines[index] = f"{canonical}{newline}"
            action = "repaired"
    else:
        if content and not content.endswith(("\n", "\r")):
            lines.append("\n")
        lines.append(f"{canonical}\n")
        action = "added"

    if action == "unchanged":
        return {"action": action}

    replacement = "".join(lines)
    try:
        installed = subprocess.run(
            ["crontab", "-"],
            input=replacement,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CronRepairError(f"Cannot install crontab: {exc}") from exc

    if installed.returncode != 0:
        raise CronRepairError(f"Crontab validation failed; existing crontab was unchanged: {_cron_error_message(installed)}")

    return {"action": action}


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _constant_eq(a: str, b: str) -> bool:
    """Constant-time string comparison (prevents timing attacks)."""
    return hmac.compare_digest(a.encode(), b.encode())


def _parse_dt(value: str | None) -> datetime | None:
    """Parse ISO8601 datetime string into timezone-aware datetime when possible."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _parse_json_field(value: str | None) -> Any:
    """Parse JSON field from Redis task hash and fall back to raw string."""
    if value in (None, ""):
        return None
    try:
        return json.loads(value)
    except Exception:
        return value


def _list_log_files() -> list[str]:
    """Return sorted supported log filenames from the log directory."""
    if not _LOGS_DIR.exists():
        return []
    return sorted(
        path.name
        for path in _LOGS_DIR.iterdir()
        if path.is_file() and path.suffix in _LOG_FILE_SUFFIXES
    )


def _resolve_log_file(file_name: str) -> Path:
    """Resolve a requested log file name safely under the logs directory."""
    requested = (file_name or "").strip()
    if not requested:
        requested = "all.log"
    elif "." not in requested:
        requested = f"{requested}.log"

    if Path(requested).name != requested or Path(requested).suffix not in _LOG_FILE_SUFFIXES:
        raise HTTPException(status_code=400, detail="Invalid log file name")

    resolved = (_LOGS_DIR / requested).resolve()
    logs_root = _LOGS_DIR.resolve()
    if resolved.parent != logs_root:
        raise HTTPException(status_code=400, detail="Invalid log file path")

    return resolved


@dataclass(frozen=True)
class LogTailCursor:
    """Track one log file identity and the byte position already delivered."""

    device: int | None
    inode: int | None
    offset: int
    initialized: bool
    pending: bytes = b""


def _new_log_tail_cursor(log_file: Path) -> LogTailCursor:
    """Create a cursor at the current end of a log file."""
    if not log_file.exists():
        return LogTailCursor(None, None, 0, False)
    stat = log_file.stat()
    return LogTailCursor(stat.st_dev, stat.st_ino, stat.st_size, True)


def _read_log_tail_lines(log_file: Path, max_lines: int = _LIVE_LOG_INITIAL_LINES) -> list[str]:
    """Return a bounded complete-line tail for a newly connected log viewer."""
    if not log_file.exists() or max_lines <= 0:
        return []

    size = log_file.stat().st_size
    start = max(0, size - _LIVE_LOG_INITIAL_BYTES)
    with log_file.open("rb") as handle:
        handle.seek(start)
        data = handle.read()

    if start:
        newline = data.find(b"\n")
        data = data[newline + 1:] if newline >= 0 else b""
    return [line for line in data.decode("utf-8", errors="replace").splitlines() if line][-max_lines:]


def _read_appended_log_lines(
    log_file: Path,
    cursor: LogTailCursor,
) -> tuple[LogTailCursor, list[str]]:
    """Return new lines while handling log creation, replacement, and truncation."""
    if not log_file.exists():
        return cursor, []

    stat = log_file.stat()
    identity = (stat.st_dev, stat.st_ino)
    cursor_identity = (cursor.device, cursor.inode)
    if cursor.initialized and identity != cursor_identity:
        return LogTailCursor(*identity, stat.st_size, True), []

    offset = cursor.offset if cursor.initialized else 0
    if stat.st_size < offset:
        return LogTailCursor(*identity, stat.st_size, True), []
    if stat.st_size == offset:
        return LogTailCursor(*identity, offset, True, cursor.pending), []

    with log_file.open("rb") as handle:
        handle.seek(offset)
        new_data = cursor.pending + handle.read(stat.st_size - offset)
    complete_data, separator, pending = new_data.rpartition(b"\n")
    if not separator:
        return LogTailCursor(*identity, stat.st_size, True, new_data), []

    lines = complete_data.decode("utf-8", errors="replace").splitlines()
    return LogTailCursor(*identity, stat.st_size, True, pending), [line for line in lines if line]


def _find_envid(value: Any, depth: int = 0) -> str:
    """Find first non-empty envid in nested JSON-like dict/list structures."""
    if depth > 5:
        return ""
    if isinstance(value, dict):
        envid = value.get("envid")
        if envid:
            return str(envid)
        for nested in value.values():
            found = _find_envid(nested, depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_envid(nested, depth + 1)
            if found:
                return found
    return ""


def _task_envid(task: dict[str, Any]) -> str:
    """Extract envid from task payload/context, including nested structures."""
    for source_name in ("context", "payload", "parent_context"):
        source = task.get(source_name)
        found = _find_envid(source)
        if found:
            return found
    return ""


def _task_last_operation_at(task: dict[str, Any]) -> datetime | None:
    """Return the latest status transition timestamp for a task."""
    for key in ("updated_at", "finished_at", "started_at", "created_at"):
        dt = _parse_dt(task.get(key))
        if dt is not None:
            return dt
    return None


async def _recent_task_routes(core: "Core") -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Load the last 1000 audited endpoint/model task routes without blocking the API loop."""
    audit_log = getattr(core, "audit_log", None)
    loader = getattr(audit_log, "recent_client_request_routes", None)
    if not callable(loader):
        return [], {}
    return await asyncio.to_thread(loader, 1000)


async def _cron_health(core: "Core") -> dict[str, Any]:
    """Return cron heartbeat freshness for the Dashboard health panel."""
    embedded = bool(core.config.get("cron.embedded", False))
    embedded_active = core.embedded_cron_enabled()
    external_available = shutil.which("crontab") is not None
    max_age = int(core.config.get("webui.health.cron_max_age") or 180)
    key = f"{core.config.get('instance', 'aidir')}:cron:last_success_at"
    raw = await core.redis.get(key)
    try:
        last_success = float(raw)
    except (TypeError, ValueError):
        last_success = 0.0

    age_seconds = max(0, int(time.time() - last_success)) if last_success else None
    return {
        "mode": "embedded" if embedded else "external",
        "embedded_active": embedded_active,
        "external_available": external_available,
        "last_success_at": datetime.fromtimestamp(last_success, timezone.utc).isoformat() if last_success else None,
        "age_seconds": age_seconds,
        "max_age_seconds": max_age,
        "healthy": age_seconds is not None and age_seconds <= max_age,
    }


def _task_to_api_item(task: dict[str, Any]) -> dict[str, Any]:
    """Return a single normalized task shape for WebUI list and detail APIs."""
    last_op = _task_last_operation_at(task)
    item = dict(task)
    item["worker_id"] = item.get("worker_id") or ""
    item["parent_worker"] = item.get("parent_worker") or ""
    item["envid"] = _task_envid(item)
    item["last_operation_at"] = last_op.isoformat() if last_op else None
    return item


def _task_from_hash(task_hash: dict[str, str]) -> dict[str, Any]:
    """Decode Redis task hash into JSON-friendly dict for API responses."""
    task: dict[str, Any] = dict(task_hash)
    task["priority"] = int(task.get("priority") or 0)
    task["llm_call_count"] = int(task.get("llm_call_count") or 0)
    task["queue_timeout"] = int(task.get("queue_timeout") or 0)
    task["run_timeout"] = int(task.get("run_timeout") or 0)
    raw_history = _parse_json_field(task.get("llm_call_history"))
    task["llm_call_history"] = raw_history if isinstance(raw_history, list) else []
    task["external"] = str(task.get("external") or "0") in {"1", "true", "True"}
    for key in ("payload", "result", "error", "parent_context", "config", "context", "resource_requirements"):
        task[key] = _parse_json_field(task.get(key))
    return task


def _task_summary_from_hash(task_hash: dict[str, str]) -> dict[str, Any]:
    """Return search-safe task metadata without decoding raw task bodies or histories."""
    status = str(task_hash.get("status") or "")
    error = _parse_json_field(task_hash.get("error")) if status == "failed" else None
    error_details = ""
    if isinstance(error, dict) and error.get("message") is not None:
        error_details = str(error["message"])[:1000]
    task: dict[str, Any] = {
        key: task_hash.get(key, "")
        for key in (
            "id", "type", "status", "worker_id", "created_at", "updated_at",
            "started_at", "finished_at", "priority", "llm_call_count", "queue_timeout",
            "run_timeout", "external", "request_preview", "route_provider_id", "model_id",
            "envid", "error_code",
        )
    }
    task["error_details"] = error_details
    task["priority"] = int(task["priority"] or 0)
    task["llm_call_count"] = int(task["llm_call_count"] or 0)
    task["queue_timeout"] = int(task["queue_timeout"] or 0)
    task["run_timeout"] = int(task["run_timeout"] or 0)
    task["external"] = str(task["external"] or "0") in {"1", "true", "True"}
    last_op = _task_last_operation_at(task)
    task["last_operation_at"] = last_op.isoformat() if last_op else None
    return task


async def _list_active_tasks(core: "Core") -> list[dict[str, Any]]:
    """Return active Redis-backed tasks, preferring current-process task state."""
    active_statuses = {"created", "queued", "running"}
    namespace = core.config.get("instance", "aidir")
    tasks: dict[str, dict[str, Any]] = {}
    cursor = 0

    while True:
        cursor, keys = await core.redis.scan(cursor, match=f"{namespace}:task:*", count=200)
        if keys:
            pipeline = core.redis.pipeline(transaction=False)
            for key in keys:
                pipeline.hgetall(key)
            hashes = await pipeline.execute()
        else:
            hashes = []

        for task_hash in hashes:
            if not task_hash or str(task_hash.get("status") or "").lower() not in active_statuses:
                continue
            task_id = str(task_hash.get("id") or "")
            if task_id:
                tasks[task_id] = _task_to_api_item(_task_from_hash(task_hash))

        if cursor == 0:
            break

    for task in core.queue.list_tasks():
        if task.status in active_statuses:
            tasks[task.id] = _task_to_api_item(_task_from_hash(task.to_redis_hash()))

    return sorted(
        tasks.values(),
        key=lambda task: task.get("last_operation_at") or task.get("created_at") or "",
        reverse=True,
    )


def _normalize_filter_values(values: list[str] | str | None) -> list[str]:
    """Normalize comma-separated or repeated query values into a flat string list."""
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    for value in values:
        for part in str(value).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _find_user(core: "Core", login: str, password: str) -> dict | None:
    """Return user dict from config if credentials match, else None."""
    users = core.config.get("webui.auth.users") or []
    for user in users:
        if _constant_eq(user.get("login", ""), login) and \
           _constant_eq(user.get("password", ""), password):
            return user
    return None


async def _create_session(core: "Core", user: dict) -> str:
    """Generate a session token and store it in Redis with TTL."""
    token = secrets.token_hex(32)
    ttl = int(core.config.get("webui.auth.session_ttl") or 86400)
    payload = json.dumps({
        "login":       user["login"],
        "permissions": user.get("permissions", []),
        "created_at":  datetime.now(timezone.utc).isoformat(),
    })
    await core.redis.set(f"{_SESSION_PREFIX}{token}", payload, ex=ttl)
    return token


async def _get_session(core: "Core", token: str) -> dict | None:
    """Return session data for token, or None if invalid/expired."""
    if not token:
        return None
    raw = await core.redis.get(f"{_SESSION_PREFIX}{token}")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


async def _require_session(
    request: Request,
    aidir_token: str | None = Cookie(default=None),
) -> dict:
    """FastAPI dependency: extract and validate session from cookie or Bearer header."""
    core: "Core" = request.app.state.core

    # Check Bearer header first, then cookie
    auth_header = request.headers.get("Authorization", "")
    token = ""
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    elif aidir_token:
        token = aidir_token

    session = await _get_session(core, token)
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Not authenticated")
    return session


# ── App factory ───────────────────────────────────────────────────────────────

def create_app(
    core: "Core",
    restart_callback: Callable[[], Awaitable[None]] | None = None,
) -> FastAPI:
    app = FastAPI(title="aidir WebUI", docs_url=None, redoc_url=None)
    attach_request_id_middleware(app)
    app.state.core = core
    app.state.restart_callback = restart_callback

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        """Log unhandled exceptions with traceback and request id."""
        request_id = get_or_create_request_id(request)
        log_exception(
            "webui",
            "unhandled",
            f"Unhandled exception method={request.method} path={request.url.path}",
            exc,
            request_id=request_id,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "request_id": request_id},
            headers={"X-Request-ID": request_id},
        )

    # ── Auth ──────────────────────────────────────────────────────────────────

    @app.post("/api/auth/login")
    async def login(request: Request, response: Response):
        body = await request.json()
        login_val = body.get("login", "")
        password_val = body.get("password", "")

        user = _find_user(core, login_val, password_val)
        if user is None:
            client_ip = request.client.host if request.client else "unknown"
            log(
                "webui",
                "warn",
                f"Failed login attempt for user '{login_val}' from {client_ip}",
                "auth",
            )
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="Invalid credentials")

        token = await _create_session(core, user)
        ttl = int(core.config.get("webui.auth.session_ttl") or 86400)
        response.set_cookie(
            "aidir_token", token,
            max_age=ttl, httponly=True, samesite="strict",
        )
        return {"ok": True, "token": token}

    @app.get("/api/auth/me")
    async def auth_me(session: dict = Depends(_require_session)):
        """Return current session info (login, permissions). Used to restore session on page load."""
        return {"ok": True, "login": session["login"], "permissions": session.get("permissions", [])}

    @app.post("/api/auth/logout")
    async def logout(
        response: Response,
        session: dict = Depends(_require_session),
        aidir_token: str | None = Cookie(default=None),
    ):
        if aidir_token:
            await core.redis.delete(f"{_SESSION_PREFIX}{aidir_token}")
        response.delete_cookie("aidir_token")
        return {"ok": True}

    # ── Tasks ─────────────────────────────────────────────────────────────────

    @app.get("/api/tasks")
    async def get_tasks(session: dict = Depends(_require_session)):
        return {
            "tasks": await _list_active_tasks(core)
        }

    @app.get("/api/tasks/viewer/meta")
    async def task_viewer_meta(session: dict = Depends(_require_session)):
        """Return available task viewer filter values."""
        workers = [
            {"id": wid, "task_type": w.task_type, "enabled": bool(w.enabled)}
            for wid, w in core.workers.items()
        ]
        envids: list[str] = []
        if core.envid_registry is not None:
            envids = sorted(e.id for e in core.envid_registry.all())
        routes, _ = await _recent_task_routes(core)
        return {
            "status_options": ["created", "queued", "running", "completed", "failed", "canceled"],
            "workers": workers,
            "envids": envids,
            "routes": routes,
        }

    @app.get("/api/tasks/viewer/search")
    async def task_viewer_search(request: Request, session: dict = Depends(_require_session)):
        """Search tasks across Redis with status/time/envid/worker filters."""
        query = request.query_params
        statuses = {
            value.lower()
            for value in _normalize_filter_values(query.getlist("status") or query.get("status"))
        }
        workers = set(_normalize_filter_values(query.getlist("worker") or query.get("worker")))
        routes = set(_normalize_filter_values(query.getlist("route") or query.get("route")))
        envid = (query.get("envid") or "").strip()
        created_from = _parse_dt(query.get("created_from"))
        created_to = _parse_dt(query.get("created_to"))
        op_from = _parse_dt(query.get("last_operation_from"))
        op_to = _parse_dt(query.get("last_operation_to"))
        limit = int(query.get("limit") or 300)
        limit = max(1, min(limit, 1000))
        _, task_routes = await _recent_task_routes(core) if routes else ([], {})

        ns = core.config.get("instance", "aidir")
        items: list[tuple[str, int, dict[str, Any]]] = []
        matching_count = 0
        sequence = 0
        cursor = 0
        while True:
            cursor, keys = await core.redis.scan(cursor, match=f"{ns}:task:*", count=200)
            if keys:
                pipeline = core.redis.pipeline(transaction=False)
                for key in keys:
                    pipeline.hgetall(key)
                raw_items = await pipeline.execute()
            else:
                raw_items = []

            for raw in raw_items:
                if not raw:
                    continue

                task_id = str(raw.get("id") or "")
                live_task = core.queue.get_task(task_id) if task_id and core.queue else None
                if live_task is not None:
                    raw = live_task.to_redis_hash()
                task = _task_summary_from_hash(raw)
                task_envid = str(task.get("envid") or "")
                if envid and task_envid != envid:
                    continue
                if statuses and str(task.get("status") or "").lower() not in statuses:
                    continue
                if workers and str(task.get("worker_id") or "") not in workers:
                    continue
                if routes and task_routes.get(task_id) not in routes:
                    continue

                created_at = _parse_dt(task.get("created_at"))
                last_op = _task_last_operation_at(task)

                if created_from and created_at and created_at < created_from:
                    continue
                if created_to and created_at and created_at > created_to:
                    continue
                if op_from and last_op and last_op < op_from:
                    continue
                if op_to and last_op and last_op > op_to:
                    continue

                matching_count += 1
                sequence += 1
                sort_key = task.get("last_operation_at") or task.get("created_at") or ""
                candidate = (sort_key, sequence, task)
                if len(items) < limit:
                    heapq.heappush(items, candidate)
                elif candidate[:2] > items[0][:2]:
                    heapq.heapreplace(items, candidate)

            if cursor == 0:
                break

        items.sort(reverse=True)
        return {
            "tasks": [item[2] for item in items],
            "count": matching_count,
        }

    @app.get("/api/tasks/viewer/{task_id}")
    async def task_viewer_item(task_id: str, session: dict = Depends(_require_session)):
        """Return full JSON for one task, from live memory or Redis."""
        live_task = core.queue.get_task(task_id) if core.queue else None
        if live_task is not None:
            return {"task": _task_to_api_item(_task_from_hash(live_task.to_redis_hash()))}

        ns = core.config.get("instance", "aidir")
        raw = await core.redis.hgetall(f"{ns}:task:{task_id}")
        if not raw:
            raise HTTPException(status_code=404, detail="Task not found")
        return {"task": _task_to_api_item(_task_from_hash(raw))}

    @app.get("/api/tasks/viewer/{task_id}/detail")
    async def task_viewer_detail(task_id: str, session: dict = Depends(_require_session)):
        """Return compact task metadata and lazy audit-event manifest."""
        live_task = core.queue.get_task(task_id) if core.queue else None
        if live_task is not None:
            task = _task_summary_from_hash(live_task.to_redis_hash())
        else:
            ns = core.config.get("instance", "aidir")
            raw = await core.redis.hgetall(f"{ns}:task:{task_id}")
            if not raw:
                raise HTTPException(status_code=404, detail="Task not found")
            task = _task_summary_from_hash(raw)
        audit_log = getattr(core, "audit_log", None)
        events = audit_log.list_task_events(task_id) if audit_log is not None else []
        terminal_audit = audit_log.task_terminal_snapshot(task_id) if audit_log is not None else None
        return {
            "task": task,
            "audit_events": events,
            "terminal_audit": terminal_audit,
            "active": task.get("status") in {"created", "queued", "running"},
        }

    @app.get("/api/tasks/viewer/{task_id}/request")
    async def task_viewer_request(task_id: str, session: dict = Depends(_require_session)):
        """Return the client-request body only when its audit event exists."""
        audit_log = getattr(core, "audit_log", None)
        if audit_log is None:
            raise HTTPException(status_code=404, detail="Audit is unavailable")
        for event in audit_log.list_task_events(task_id):
            if event["type"] == "client_request":
                return {"event": audit_log.read_event(event["event_id"])}
        raise HTTPException(status_code=404, detail="Client request audit event is unavailable")

    @app.get("/api/tasks/viewer/{task_id}/raw")
    async def task_viewer_raw(task_id: str, type: str, event_id: str, session: dict = Depends(_require_session)):
        """Return one task-owned audit event selected by a manifest event ID."""
        if type not in {"client_request", "client_response", "llm_request", "llm_response"}:
            raise HTTPException(status_code=400, detail="Unsupported audit event type")
        audit_log = getattr(core, "audit_log", None)
        if audit_log is None:
            raise HTTPException(status_code=404, detail="Audit is unavailable")
        event = audit_log.read_event(event_id)
        if event is None or event.get("task_id") != task_id or event.get("type") != type:
            raise HTTPException(status_code=404, detail="Audit event not found")
        return {"event": event}

    @app.get("/api/tasks/viewer/audit-files/{file_id}")
    async def task_viewer_audit_file(file_id: str, session: dict = Depends(_require_session)):
        """Serve a file-backed audit body by its opaque indexed identifier only."""
        audit_log = getattr(core, "audit_log", None)
        if audit_log is None:
            raise HTTPException(status_code=404, detail="Audit is unavailable")
        event = audit_log.find_body_file(file_id)
        body_file = event.get("body_file") if isinstance(event, dict) else None
        if not isinstance(body_file, dict):
            raise HTTPException(status_code=404, detail="Audit file not found")
        relative_path = Path(str(body_file.get("relative_path") or ""))
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise HTTPException(status_code=404, detail="Audit file not found")
        path = audit_log.directory / relative_path
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Audit file not found")
        content_type = str(body_file.get("content_type") or "application/octet-stream")
        inline = content_type.startswith(("image/png", "image/jpeg", "image/gif", "image/webp"))
        disposition = "inline" if inline else "attachment"
        return FileResponse(
            path,
            media_type=content_type,
            headers={
                "Content-Disposition": f'{disposition}; filename="{file_id}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/tasks/{task_id}/terminate")
    async def terminate_task(task_id: str, session: dict = Depends(_require_session)):
        """Terminate one live task visible on the dashboard/task viewer."""
        task_hash = await core.terminate_task(task_id)
        if task_hash is None:
            raise HTTPException(status_code=404, detail="Task not found")

        log("webui", "warn", f"Task termination requested by user {session['login']}: {task_id}", "control")
        return {"ok": True, "task": _task_to_api_item(_task_from_hash(task_hash))}

    @app.post("/api/tasks/{task_id}/extend-timeout")
    async def extend_task_timeout(task_id: str, session: dict = Depends(_require_session)):
        """Extend a queued or running task's active timeout by one minute."""
        task = await core.queue.extend_active_timeout(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Active task not found")

        log("webui", "info", f"Task timeout extended by user {session['login']}: {task_id}", "control")
        return {"ok": True, "task": _task_to_api_item(_task_from_hash(task.to_redis_hash()))}

    # ── Status ────────────────────────────────────────────────────────────────

    @app.get("/api/status")
    async def get_status(session: dict = Depends(_require_session)):
        workers_info = {
            wid: {"task_type": w.task_type, "enabled": w.enabled}
            for wid, w in core.workers.items()
        }
        return {
            "instance": core.config.get("instance", "aidir"),
            "workers":  workers_info,
            "tasks":    len(core.queue.list_tasks()),
            "resources": core.resources.snapshot() if core.resources else [],
            "runtime": core.get_runtime_status(),
            "health": {
                "cron": await _cron_health(core),
                "audit": core.audit_log.health() if core.audit_log else {"status": "unavailable"},
            },
        }

    @app.post("/api/cron/repair")
    async def repair_cron(session: dict = Depends(_require_session)):
        """Safely add or repair the unique aidir cron entry for the current user."""
        if core.config.get("cron.embedded", False):
            raise HTTPException(status_code=409, detail="Embedded cron is enabled")
        if shutil.which("crontab") is None:
            raise HTTPException(status_code=503, detail="System cron is not available")
        try:
            result = await asyncio.to_thread(_repair_user_crontab)
        except CronRepairError as exc:
            log("webui", "error", f"Cron repair failed for user {session['login']}: {exc}", "control")
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        log("webui", "info", f"Cron repair {result['action']} by user {session['login']}", "control")
        return {"ok": True, **result}

    @app.post("/api/cron/embedded/enable")
    async def enable_embedded_cron(session: dict = Depends(_require_session)):
        """Enable embedded cron in configuration while preserving other cron settings."""
        cron_config = core.config.get("cron", {})
        if not isinstance(cron_config, dict):
            raise HTTPException(status_code=400, detail="cron configuration must be an object")
        try:
            core.config.update_key("cron", {**cron_config, "embedded": True})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            log("webui", "error", f"Embedded cron enable failed for user {session['login']}: {exc}", "control")
            raise HTTPException(status_code=500, detail=f"Failed to enable embedded cron: {exc}") from exc

        log("webui", "info", f"Embedded cron enabled by user {session['login']}", "control")
        return {"ok": True, "restart_required": not core.embedded_cron_enabled()}

    @app.post("/api/resources/{resource_id}/use")
    async def set_resource_use(
        resource_id: str,
        request: Request,
        session: dict = Depends(_require_session),
    ):
        """Enable or disable one resource for new task reservations in this runtime."""
        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Request body must be JSON") from exc
        use = body.get("use") if isinstance(body, dict) else None
        if not isinstance(use, bool):
            raise HTTPException(status_code=422, detail="Field 'use' must be a boolean")
        if core.resources is None:
            raise HTTPException(status_code=404, detail="Resource not found")
        resource = core.resources.set_use(resource_id, use)
        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")

        log("webui", "warn", f"Resource {resource_id} use={use} set by user {session['login']}", "control")
        return {"ok": True, "resource": resource.snapshot()}

    @app.post("/api/resources/{resource_id}/force-release")
    async def force_release_resource(resource_id: str, session: dict = Depends(_require_session)):
        """Release idle models from one resource without interrupting active tasks."""
        if core.resources is None:
            raise HTTPException(status_code=404, detail="Resource not found")
        result = await core.resources.force_release(resource_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Resource not found")
        if result["active_consumers"]:
            raise HTTPException(
                status_code=409,
                detail=f"Resource has active task(s): {', '.join(result['active_consumers'])}",
            )

        log("webui", "warn", f"Resource {resource_id} force-released by user {session['login']}", "control")
        return {"ok": result["released"], **result}

    @app.post("/api/restart")
    async def restart_service(
        request: Request,
        session: dict = Depends(_require_session),
    ):
        callback = getattr(request.app.state, "restart_callback", None)
        if callback is None:
            raise HTTPException(status_code=503, detail="Restart is not available")

        runtime = core.get_runtime_status()
        if not runtime["restart_requested"]:
            log("webui", "warn", f"Restart requested by user {session['login']}", "control")
            asyncio.create_task(callback())

        return {
            "ok": True,
            "runtime": {**core.get_runtime_status(), "restart_requested": True},
        }

    # ── Logs (REST) ───────────────────────────────────────────────────────────

    @app.get("/api/logs/files")
    async def list_logs(session: dict = Depends(_require_session)):
        """Return log files available to the WebUI viewer."""
        return {"files": _list_log_files()}

    @app.get("/api/logs")
    async def get_logs(
        file: str = "all",
        lines: int = 200,
        session: dict = Depends(_require_session),
    ):
        """Return last N lines of a log file."""
        log_file = _resolve_log_file(file)
        if not log_file.exists():
            return {"lines": []}
        try:
            content = log_file.read_text(encoding="utf-8", errors="replace")
            last = content.splitlines()[-lines:]
            return {"lines": last}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.get("/api/logs/search")
    async def search_logs(
        file: str,
        contains: str,
        limit: int = 200,
        session: dict = Depends(_require_session),
    ):
        """Return matching lines from a log file using plain substring filtering."""
        log_file = _resolve_log_file(file)
        if not contains:
            raise HTTPException(status_code=400, detail="contains query parameter is required")

        if not log_file.exists():
            return {"file": log_file.name, "contains": contains, "lines": [], "count": 0}

        limit = max(1, min(int(limit or 200), 2000))

        matched_lines: list[str] = []
        total_count = 0
        try:
            with log_file.open("r", encoding="utf-8", errors="replace") as handle:
                for raw_line in handle:
                    line = raw_line.rstrip("\n")
                    if contains not in line:
                        continue
                    total_count += 1
                    if len(matched_lines) < limit:
                        matched_lines.append(line)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

        return {
            "file": log_file.name,
            "contains": contains,
            "lines": matched_lines,
            "count": total_count,
            "truncated": total_count > len(matched_lines),
        }

    @app.websocket("/ws/logs")
    async def ws_logs(
        websocket: WebSocket,
        file: str = "all",
        token: str = "",
    ):
        """Stream new log lines over WebSocket. Auth via query token or session cookie."""
        # Prefer query param token; fall back to cookie
        effective_token = token or websocket.cookies.get("aidir_token", "")
        session = await _get_session(core, effective_token)
        if session is None:
            await websocket.close(code=4001)
            return

        await websocket.accept()
        log_file = _resolve_log_file(file)

        # Deliver useful context, then stream only later writes.
        for line in _read_log_tail_lines(log_file):
            await websocket.send_text(line)
        cursor = _new_log_tail_cursor(log_file)

        try:
            while True:
                await asyncio.sleep(1)
                cursor, new_lines = _read_appended_log_lines(log_file, cursor)
                for line in new_lines:
                    await websocket.send_text(line)
        except WebSocketDisconnect:
            pass

    # ── Config read/write ─────────────────────────────────────────────────────

    @app.get("/api/config")
    async def get_config(session: dict = Depends(_require_session)):
        """Return current config (without secrets substituted back)."""
        return core.config.raw()

    @app.get("/api/config/raw")
    async def get_config_raw(session: dict = Depends(_require_session)):
        """Return raw config text for direct editing mode."""
        return {"text": core.config.raw_text()}

    @app.post("/api/config/raw")
    async def save_config_raw(request: Request, session: dict = Depends(_require_session)):
        """Validate and save full config text from UI, then reload config cache."""
        body = await request.json()
        config_text = body.get("config_text", "")
        if not isinstance(config_text, str) or not config_text.strip():
            raise HTTPException(status_code=400, detail="config_text must be a non-empty string")

        try:
            core.config.save_config_text(config_text)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to save config: {exc}")

        return {"ok": True, "config": core.config.raw()}

    @app.post("/api/config/fields")
    async def update_config_fields(request: Request, session: dict = Depends(_require_session)):
        """Update config fields one by one using Config.update_key and reload cache."""
        body = await request.json()
        changes = body.get("changes", [])
        if not isinstance(changes, list) or not changes:
            raise HTTPException(status_code=400, detail="changes must be a non-empty list")

        try:
            for change in changes:
                key = change.get("key")
                if not isinstance(key, str) or not key.strip():
                    raise ValueError("Each change must include non-empty key")
                if bool(change.get("remove")):
                    core.config.delete_key(key)
                elif "value_text" in change:
                    core.config.update_key_text(key, change.get("value_text", ""))
                else:
                    core.config.update_key(key, change.get("value"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to update config fields: {exc}")

        return {"ok": True, "config": core.config.raw()}

    @app.get("/api/config/fields")
    async def get_config_fields(keys: str, session: dict = Depends(_require_session)):
        """Return raw text values for requested comma-separated config keys."""
        items = [k.strip() for k in keys.split(",") if k.strip()]
        if not items:
            raise HTTPException(status_code=400, detail="keys query parameter is required")

        out: dict[str, str | None] = {}
        for key in items:
            out[key] = core.config.get_key_text_or_none(key)

        return {"fields": out}

    # ── Test: workers / models info ───────────────────────────────────────────

    @app.get("/api/workers/models")
    async def get_workers_models(session: dict = Depends(_require_session)):
        """Return workers list and model providers from config (for TEST LLM page)."""
        workers_list = []
        for wid, worker in core.workers.items():
            workers_list.append({
                "id": wid,
                "type": worker.task_type,
                "enabled": worker.enabled,
            })

        providers_list: list[dict] = []
        providers_cfg = core.config.get("models.providers") or {}
        if isinstance(providers_cfg, dict):
            for pid, pcfg in providers_cfg.items():
                if not isinstance(pcfg, dict):
                    continue
                raw_models = pcfg.get("models") or []
                models = [
                    {"id": m.get("id", ""), "name": m.get("name", m.get("id", ""))}
                    for m in raw_models if isinstance(m, dict)
                ]
                providers_list.append({
                    "id": pid,
                    "api": pcfg.get("api", ""),
                    "baseUrl": pcfg.get("baseUrl", ""),
                    "models": models,
                })

        return {"workers": workers_list, "providers": providers_list}

    # ── Test: endpoints / MCP tools info ─────────────────────────────────────

    @app.get("/api/endpoints/info")
    async def get_endpoints_info(session: dict = Depends(_require_session)):
        """Return all configured endpoints with their tools (for TEST MCP page)."""
        result: list[dict] = []
        endpoints_cfg = core.config.get("endpoints") or []
        if isinstance(endpoints_cfg, list):
            for ep in endpoints_cfg:
                if not isinstance(ep, dict):
                    continue
                tools_cfg = ep.get("tools") or {}
                tools_list: list[dict] = []
                if isinstance(tools_cfg, dict):
                    for tid, tcfg in tools_cfg.items():
                        tcfg = tcfg if isinstance(tcfg, dict) else {}
                        tools_list.append({
                            "name": tid,
                            "description": tcfg.get("description", f"Tool {tid}"),
                            "inputSchema": tcfg.get("inputSchema", {"type": "object", "properties": {}}),
                        })
                result.append({
                    "id": ep.get("id", ""),
                    "api": ep.get("api", ""),
                    "port": ep.get("port"),
                    "tools": tools_list,
                })
        return {"endpoints": result}

    # ── Test: LLM proxy ───────────────────────────────────────────────────────

    @app.post("/api/test/llm")
    async def test_llm(request: Request, session: dict = Depends(_require_session)):
        """Proxy LLM chat request to the configured ollama endpoint."""
        import httpx

        body = await request.json()

        endpoints_cfg = core.config.get("endpoints") or []
        ollama_port: int | None = None
        for ep in (endpoints_cfg if isinstance(endpoints_cfg, list) else []):
            if isinstance(ep, dict) and ep.get("api") == "ollama":
                ollama_port = ep.get("port")
                break

        if not ollama_port:
            raise HTTPException(status_code=503, detail="No ollama endpoint configured")

        timeout = float(core.config.get("webui.request_timeouts.ollama_chat") or 120)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"http://127.0.0.1:{ollama_port}/api/chat",
                    json=body,
                )
                try:
                    data = resp.json()
                except Exception:
                    data = {"error": {"code": "PARSE_ERROR", "message": resp.text or "Invalid response"}}
                return JSONResponse(content=data, status_code=resp.status_code)
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail="Cannot connect to ollama endpoint")
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Ollama endpoint timed out")
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    # ── Test: MCP proxy ───────────────────────────────────────────────────────

    @app.post("/api/test/mcp")
    async def test_mcp(request: Request, session: dict = Depends(_require_session)):
        """Proxy MCP JSON-RPC call to the configured MCP endpoint."""
        import httpx

        body = await request.json()
        endpoint_id = body.pop("_endpoint_id", None)
        method = str(body.get("method", ""))

        endpoints_cfg = core.config.get("endpoints") or []
        ep_cfg: dict | None = None
        for ep in (endpoints_cfg if isinstance(endpoints_cfg, list) else []):
            if isinstance(ep, dict) and ep.get("api") == "mcp":
                if endpoint_id is None or ep.get("id") == endpoint_id:
                    ep_cfg = ep
                    break

        if not ep_cfg:
            raise HTTPException(status_code=503, detail="MCP endpoint not found")

        endpoint_name = str(ep_cfg.get("id") or "mcp")
        port = ep_cfg.get("port")

        if method == "tools/list":
            log(
                "webui",
                "info",
                f"external_mcp_tools_list request user={session.get('login', 'unknown')} endpoint={endpoint_name} port={port}",
                "test_mcp",
            )

        timeout = float(core.config.get("webui.request_timeouts.mcp_proxy") or 60)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(f"http://127.0.0.1:{port}/mcp", json=body)
                try:
                    data = resp.json()
                except Exception:
                    data = {"error": resp.text or "Invalid response"}

                if method == "tools/list":
                    tools_count = -1
                    if isinstance(data, dict):
                        result_obj = data.get("result")
                        if isinstance(result_obj, dict) and isinstance(result_obj.get("tools"), list):
                            tools_count = len(result_obj.get("tools"))
                    log(
                        "webui",
                        "info",
                        (
                            f"external_mcp_tools_list response endpoint={endpoint_name} "
                            f"status={resp.status_code} tools_count={tools_count}"
                        ),
                        "test_mcp",
                    )

                return JSONResponse(content=data, status_code=resp.status_code)
        except httpx.ConnectError:
            if method == "tools/list":
                log(
                    "webui",
                    "info",
                    f"external_mcp_tools_list error endpoint={endpoint_name} reason=connect_error",
                    "test_mcp",
                )
            raise HTTPException(status_code=502, detail="Cannot connect to MCP endpoint")
        except httpx.TimeoutException:
            if method == "tools/list":
                log(
                    "webui",
                    "info",
                    f"external_mcp_tools_list error endpoint={endpoint_name} reason=timeout",
                    "test_mcp",
                )
            raise HTTPException(status_code=504, detail="MCP endpoint timed out")
        except Exception as exc:
            if method == "tools/list":
                log(
                    "webui",
                    "info",
                    f"external_mcp_tools_list error endpoint={endpoint_name} reason=exception detail={exc}",
                    "test_mcp",
                )
            raise HTTPException(status_code=500, detail=str(exc))

    @app.get("/api/test/agent/endpoints")
    async def list_agent_endpoints(session: dict = Depends(_require_session)):
        """Return chat-capable endpoints for Agent Request page."""
        result: list[dict] = []
        endpoints_cfg = core.config.get("endpoints") or []
        allowed = {"openaix", "ollama", "openai", "anthropic"}

        if isinstance(endpoints_cfg, list):
            for ep in endpoints_cfg:
                if not isinstance(ep, dict):
                    continue
                api = str(ep.get("api", ""))
                if api not in allowed:
                    continue
                result.append(
                    {
                        "id": ep.get("id", ""),
                        "api": api,
                        "port": ep.get("port"),
                    }
                )

        return {"endpoints": result}

    @app.get("/api/test/agent/catalog")
    async def get_agent_catalog(session: dict = Depends(_require_session)):
        """Return provider and model catalog from config for Agent Request page."""
        providers: list[dict] = []
        envids: list[str] = []
        providers_cfg = core.config.get("models.providers") or {}
        envids_cfg = core.config.get("envids.items") or {}

        if isinstance(providers_cfg, dict):
            for provider_id, provider_cfg in providers_cfg.items():
                if not isinstance(provider_cfg, dict):
                    continue

                models: list[str] = []
                raw_models = provider_cfg.get("models") or []
                if isinstance(raw_models, list):
                    for model in raw_models:
                        if isinstance(model, dict) and model.get("id"):
                            models.append(str(model["id"]))

                providers.append({
                    "id": str(provider_id),
                    "models": models,
                })

        if isinstance(envids_cfg, dict):
            for envid_id in envids_cfg.keys():
                envids.append(str(envid_id))

        return {"providers": providers, "envids": sorted(envids)}

    @app.get("/api/test/agent/models")
    async def list_agent_models(
        endpoint_id: str,
        protocol: str = "ollama",
        session: dict = Depends(_require_session),
    ):
        """Fetch model list from selected endpoint for chosen protocol."""
        import httpx

        endpoints_cfg = core.config.get("endpoints") or []
        ep_cfg: dict | None = None
        for ep in (endpoints_cfg if isinstance(endpoints_cfg, list) else []):
            if isinstance(ep, dict) and ep.get("id") == endpoint_id:
                ep_cfg = ep
                break

        if not ep_cfg:
            raise HTTPException(status_code=404, detail="Endpoint not found")

        port = ep_cfg.get("port")
        if not port:
            raise HTTPException(status_code=503, detail="Endpoint has no port configured")

        if protocol == "openai":
            url = f"http://127.0.0.1:{port}/v1/models"
        else:
            url = f"http://127.0.0.1:{port}/api/tags"

        timeout = float(core.config.get("webui.request_timeouts.model_list") or 20)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    raise HTTPException(status_code=resp.status_code, detail=f"Model list request failed: {resp.text[:200]}")
                data = resp.json()
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail=f"Cannot connect to endpoint on port {port}")
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Model list request timed out")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

        models: list[str] = []
        if protocol == "openai":
            for item in (data.get("data") if isinstance(data, dict) else []) or []:
                if isinstance(item, dict) and item.get("id"):
                    models.append(str(item["id"]))
        else:
            for item in (data.get("models") if isinstance(data, dict) else []) or []:
                if isinstance(item, dict) and item.get("name"):
                    models.append(str(item["name"]))

        return {"models": models}

    @app.post("/api/test/agent")
    async def test_agent(request: Request, session: dict = Depends(_require_session)):
        """Proxy agent request to a configured endpoint with chosen protocol."""
        import httpx

        body = await request.json()
        endpoint_id = body.pop("_endpoint_id", None)
        protocol    = body.pop("_protocol", "ollama")
        user_token  = body.pop("_user_token", None)

        endpoints_cfg = core.config.get("endpoints") or []
        ep_cfg: dict | None = None
        for ep in (endpoints_cfg if isinstance(endpoints_cfg, list) else []):
            if isinstance(ep, dict):
                if endpoint_id is None or ep.get("id") == endpoint_id:
                    ep_cfg = ep
                    break

        if not ep_cfg:
            raise HTTPException(status_code=503, detail="Endpoint not found")

        ep_api = str(ep_cfg.get("api", ""))
        if ep_api == "mcp":
            raise HTTPException(status_code=422, detail="Selected endpoint is MCP and cannot process chat requests")

        port = ep_cfg.get("port")
        if not port:
            raise HTTPException(status_code=503, detail="Endpoint has no port configured")

        if protocol == "openai":
            url = f"http://127.0.0.1:{port}/v1/chat/completions"
        elif protocol == "anthropic":
            url = f"http://127.0.0.1:{port}/v1/messages"
        else:
            url = f"http://127.0.0.1:{port}/api/chat"

        headers: dict[str, str] = {}
        if user_token:
            headers["Authorization"] = f"Bearer {user_token}"

        # Use timeout from request if specified, otherwise use config default
        request_timeout = body.pop("_timeout", None)
        if request_timeout is not None:
            timeout = float(request_timeout)
        else:
            timeout = float(core.config.get("webui.request_timeouts.agent_test") or 45)
        
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=body, headers=headers)
                try:
                    data = resp.json()
                except Exception:
                    data = {"error": {"code": "PARSE_ERROR", "message": resp.text or "Invalid response"}}
                return JSONResponse(content=data, status_code=resp.status_code)
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail=f"Cannot connect to endpoint on port {port}")
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Endpoint timed out")
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    # ── Static files ──────────────────────────────────────────────────────────
    # Served last so API routes take priority

    if _FRONTEND_DIR.exists():
        app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")

    return app
