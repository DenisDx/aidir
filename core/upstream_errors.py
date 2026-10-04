"""Preserve executor HTTP errors across serializable task results."""
from __future__ import annotations

import base64
import json

from fastapi.responses import Response


def build_upstream_error(status_code: int, body: bytes | str, content_type: str) -> dict:
    """Return a serializable worker error retaining status, content type, and body."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    message = f"Upstream returned HTTP {status_code}"
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        payload = None
    if isinstance(payload, dict):
        detail = payload.get("error", payload)
        if isinstance(detail, dict) and isinstance(detail.get("message"), str):
            message = detail["message"]
        elif isinstance(detail, str):
            message = detail
    return {
        "code": "UPSTREAM_ERROR",
        "message": message,
        "body": body.decode("utf-8", errors="replace"),
        "upstream_status": status_code,
        "upstream_body_base64": base64.b64encode(body).decode("ascii"),
        "upstream_content_type": content_type,
    }


def upstream_error_response(error: dict) -> Response | None:
    """Return the original executor response, or None for an aidir-generated error."""
    if "upstream_body_base64" not in error:
        return None
    return Response(
        base64.b64decode(error["upstream_body_base64"], validate=True),
        status_code=error["upstream_status"],
        headers={"content-type": error["upstream_content_type"]},
    )


def upstream_error_payload(error: dict) -> dict:
    """Return the original JSON error for an open stream, or an explicit text error."""
    response = upstream_error_response(error)
    if response is None:
        return {"error": error}
    try:
        payload = json.loads(response.body)
    except (ValueError, UnicodeDecodeError):
        return {"error": {"message": error["body"], "code": error["upstream_status"]}}
    return payload if isinstance(payload, dict) else {"error": payload}
