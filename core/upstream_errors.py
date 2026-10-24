"""Preserve executor HTTP errors across serializable task results."""
from __future__ import annotations

import base64
import json

from collections.abc import Mapping

from fastapi.responses import Response

_ALLOWED_UPSTREAM_RESPONSE_HEADERS = {"retry-after", "request-id", "x-request-id"}


def _filtered_upstream_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Return allowlisted upstream response headers with safe values only."""
    if not headers:
        return {}
    kept: dict[str, str] = {}
    for name, value in headers.items():
        lower = str(name).lower()
        if lower not in _ALLOWED_UPSTREAM_RESPONSE_HEADERS and not lower.startswith("x-ratelimit-"):
            continue
        text = str(value)
        if "\r" in text or "\n" in text:
            continue
        kept[lower] = text
    return kept


def build_upstream_error(
    status_code: int,
    body: bytes | str,
    content_type: str,
    upstream_headers: Mapping[str, str] | None = None,
) -> dict:
    """Return a serializable worker error retaining status, body and allowed headers."""
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
        "upstream_headers": _filtered_upstream_headers(upstream_headers),
    }


def upstream_error_response(error: dict) -> Response | None:
    """Return the original executor response, or None for an aidir-generated error."""
    if "upstream_body_base64" not in error:
        return None
    headers = {"content-type": error["upstream_content_type"]}
    upstream_headers = error.get("upstream_headers")
    if isinstance(upstream_headers, dict):
        headers.update({str(name): str(value) for name, value in upstream_headers.items()})
    return Response(
        base64.b64decode(error["upstream_body_base64"], validate=True),
        status_code=error["upstream_status"],
        headers=headers,
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
