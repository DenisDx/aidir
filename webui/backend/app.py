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
import hmac
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import (
    Cookie, Depends, FastAPI, HTTPException, Request,
    Response, WebSocket, WebSocketDisconnect, status,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from core import log

if TYPE_CHECKING:
    from core.app import Core

_FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
_LOGS_DIR     = Path(__file__).parent.parent.parent / "logs"
_SESSION_PREFIX = "aidir:session:"


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _constant_eq(a: str, b: str) -> bool:
    """Constant-time string comparison (prevents timing attacks)."""
    return hmac.compare_digest(a.encode(), b.encode())


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

def create_app(core: "Core") -> FastAPI:
    app = FastAPI(title="aidir WebUI", docs_url=None, redoc_url=None)
    app.state.core = core

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
        tasks = core.queue.list_tasks()
        return {
            "tasks": [
                {
                    "id":         t.id,
                    "type":       t.type,
                    "status":     t.status,
                    "priority":   t.priority,
                    "worker_id":  t.worker_id,
                    "created_at": t.created_at.isoformat(),
                    "started_at": t.started_at.isoformat() if t.started_at else None,
                }
                for t in tasks
            ]
        }

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
        }

    # ── Logs (REST) ───────────────────────────────────────────────────────────

    @app.get("/api/logs")
    async def get_logs(
        file: str = "all",
        lines: int = 200,
        session: dict = Depends(_require_session),
    ):
        log_file = _LOGS_DIR / f"{file}.log"
        if not log_file.exists():
            return {"lines": []}
        try:
            content = log_file.read_text(encoding="utf-8", errors="replace")
            last = content.splitlines()[-lines:]
            return {"lines": last}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    # ── Logs (WebSocket) ──────────────────────────────────────────────────────

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
        log_file = _LOGS_DIR / f"{file}.log"

        # Start from end of file
        offset = log_file.stat().st_size if log_file.exists() else 0

        try:
            while True:
                await asyncio.sleep(1)
                if not log_file.exists():
                    continue
                size = log_file.stat().st_size
                if size <= offset:
                    continue
                with open(log_file, "rb") as f:
                    f.seek(offset)
                    new_data = f.read(size - offset)
                offset = size
                lines = new_data.decode("utf-8", errors="replace").splitlines()
                for line in lines:
                    if line:
                        await websocket.send_text(line)
        except WebSocketDisconnect:
            pass

    # ── Config read/write ─────────────────────────────────────────────────────

    @app.get("/api/config")
    async def get_config(session: dict = Depends(_require_session)):
        """Return current config (without secrets substituted back)."""
        return core.config.raw()

    # ── Static files ──────────────────────────────────────────────────────────
    # Served last so API routes take priority

    if _FRONTEND_DIR.exists():
        app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")

    return app
