"""
Shared exception logging helpers.
Provides traceback logging and request id extraction/generation.
"""
from __future__ import annotations

import traceback
import uuid

from fastapi import FastAPI
from fastapi import Request

from core import log


def get_or_create_request_id(request: Request) -> str:
    """Return request id from headers or generate a short one."""
    state_rid = getattr(request.state, "request_id", None)
    if isinstance(state_rid, str) and state_rid.strip():
        return state_rid.strip()

    rid = request.headers.get("x-request-id") or request.headers.get("X-Request-ID")
    if isinstance(rid, str) and rid.strip():
        return rid.strip()
    return uuid.uuid4().hex[:16]


def attach_request_id_middleware(app: FastAPI) -> None:
    """Attach middleware that ensures every HTTP response has X-Request-ID."""

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = get_or_create_request_id(request)
        request.state.request_id = request_id

        response = await call_next(request)
        if "X-Request-ID" not in response.headers:
            response.headers["X-Request-ID"] = request_id
        return response


def log_exception(type_: str, tag: str, message: str, exc: BaseException, request_id: str | None = None) -> None:
    """Write exception summary and full traceback to application logs."""
    rid_part = f" request_id={request_id}" if request_id else ""
    log(type_, "error", f"{message}{rid_part}: {exc}", tag)

    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log(type_, "error", f"Traceback{rid_part}:\n{trace}", tag)
