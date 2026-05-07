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
from typing import TYPE_CHECKING, Awaitable, Callable

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

def create_app(
    core: "Core",
    restart_callback: Callable[[], Awaitable[None]] | None = None,
) -> FastAPI:
    app = FastAPI(title="aidir WebUI", docs_url=None, redoc_url=None)
    app.state.core = core
    app.state.restart_callback = restart_callback

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
            "resources": core.resources.snapshot() if core.resources else [],
            "runtime": core.get_runtime_status(),
        }

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

    @app.get("/api/logs")
    async def get_logs(
        file: str = "all",
        lines: int = 200,
        session: dict = Depends(_require_session),
    ):
        """Return last N lines of a log file."""
        log_file = _LOGS_DIR / f"{file}.log"
        if not log_file.exists():
            return {"lines": []}
        try:
            content = log_file.read_text(encoding="utf-8", errors="replace")
            last = content.splitlines()[-lines:]
            return {"lines": last}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

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
                new_lines = new_data.decode("utf-8", errors="replace").splitlines()
                for line in new_lines:
                    if line:
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

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
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

        endpoints_cfg = core.config.get("endpoints") or []
        ep_cfg: dict | None = None
        for ep in (endpoints_cfg if isinstance(endpoints_cfg, list) else []):
            if isinstance(ep, dict) and ep.get("api") == "mcp":
                if endpoint_id is None or ep.get("id") == endpoint_id:
                    ep_cfg = ep
                    break

        if not ep_cfg:
            raise HTTPException(status_code=503, detail="MCP endpoint not found")

        port = ep_cfg.get("port")
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(f"http://127.0.0.1:{port}/mcp", json=body)
                try:
                    data = resp.json()
                except Exception:
                    data = {"error": resp.text or "Invalid response"}
                return JSONResponse(content=data, status_code=resp.status_code)
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail="Cannot connect to MCP endpoint")
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="MCP endpoint timed out")
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    # ── Static files ──────────────────────────────────────────────────────────
    # Served last so API routes take priority

    if _FRONTEND_DIR.exists():
        app.mount("/", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")

    return app
