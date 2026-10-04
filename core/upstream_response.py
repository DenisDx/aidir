"""Original executor responses carried beside aidir's normalized task data."""
from __future__ import annotations

import base64
import json
from typing import Any, AsyncIterator, Awaitable, Callable, TypedDict

from fastapi.responses import Response


class UpstreamResponse(TypedDict):
    """Serializable response body, protocol, status, and content type."""

    protocol: str
    status_code: int
    content_type: str
    body_base64: str


def capture_response(protocol: str, status_code: int, content_type: str, body: bytes | str) -> UpstreamResponse:
    """Return serializable transport metadata for one executor response."""
    raw = body.encode("utf-8") if isinstance(body, str) else body
    return {
        "protocol": protocol,
        "status_code": status_code,
        "content_type": content_type,
        "body_base64": base64.b64encode(raw).decode("ascii"),
    }


def original_response(response: UpstreamResponse | None, protocol: str) -> Response | None:
    """Restore an original HTTP response only when its protocol matches the client."""
    if response is None or response["protocol"] != protocol:
        return None
    return Response(
        base64.b64decode(response["body_base64"], validate=True),
        status_code=response["status_code"],
        headers={"content-type": response["content_type"]},
    )


def original_payload(response: UpstreamResponse | None, protocol: str, fallback: dict) -> dict:
    """Return original JSON for conversion, or normalized data when unavailable."""
    restored = original_response(response, protocol)
    if restored is None:
        return fallback
    payload = json.loads(restored.body)
    if not isinstance(payload, dict):
        raise ValueError("Executor chat response must be a JSON object")
    return payload


class UpstreamChunk(dict[str, Any]):
    """Internal chunk with an original transport event that is never serialized as fields."""

    def __init__(self, data: dict, *, protocol: str, raw: bytes, content_type: str, original: dict | None) -> None:
        """Keep normalized data and original event bytes separately for endpoint delivery."""
        super().__init__(data)
        self.protocol = protocol
        self.raw = raw
        self.content_type = content_type
        self.original = original


def original_chunk(chunk: dict, protocol: str) -> bytes | None:
    """Return exact event bytes for matching protocols, otherwise None."""
    return chunk.raw if isinstance(chunk, UpstreamChunk) and chunk.protocol == protocol else None


def without_reasoning(data: dict) -> dict:
    """Remove reasoning content from final tool-loop messages, retaining answer and usage."""
    result = {key: value for key, value in data.items() if key not in {"thinking", "reasoning", "reasoning_content", "reasoning_details"}}
    for key in ("message", "delta"):
        if isinstance(result.get(key), dict):
            result[key] = without_reasoning(result[key])
    if isinstance(result.get("choices"), list):
        result["choices"] = [without_reasoning(choice) if isinstance(choice, dict) else choice for choice in result["choices"]]
    return result


async def iter_response_lines(response, spool) -> AsyncIterator[bytes]:
    """Yield complete transport lines, preserving delimiters and audit bytes."""
    aiter_raw = getattr(response, "aiter_raw", None)
    if getattr(response, "headers", {}).get("content-encoding"):
        aiter_raw = getattr(response, "aiter_bytes", None)
    if callable(aiter_raw):
        buffer = b""
        async for raw in aiter_raw():
            if spool is not None:
                spool.write(raw)
            buffer += raw
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                yield line + b"\n"
        if buffer:
            yield buffer
        return
    async for line in response.aiter_lines():
        raw = line.encode("utf-8") + b"\n"
        if spool is not None:
            spool.write(raw)
        yield raw


async def iter_sse_events(response, spool) -> AsyncIterator[bytes]:
    """Yield complete original SSE events, including comments and a final unterminated event."""
    event = b""
    async for line in iter_response_lines(response, spool):
        event += line
        if line in (b"\n", b"\r\n"):
            yield event
            event = b""
    if event:
        yield event


def sse_data(event: bytes) -> bytes | None:
    """Extract joined SSE data lines without changing the original event."""
    lines = []
    for line in event.splitlines():
        if line == b"data" or line.startswith(b"data:"):
            value = line[5:] if line.startswith(b"data:") else b""
            lines.append(value[1:] if value.startswith(b" ") else value)
    return b"\n".join(lines) if lines else None


async def consume_ndjson_response(
    response,
    emit: Callable[[dict], Awaitable[None]] | None,
    chunks: list[dict],
    spool,
    save_call: bool,
    normalize: Callable[[dict], object],
) -> dict | None:
    """Parse Ollama events while retaining every original line for transparent clients."""
    final_data = None
    content_type = getattr(response, "headers", {}).get("content-type", "application/x-ndjson")
    async for raw in iter_response_lines(response, spool):
        if not raw.strip():
            if emit is not None:
                await emit(UpstreamChunk({}, protocol="ollama", raw=raw, content_type=content_type, original=None))
            continue
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Executor chat stream event must be a JSON object")
        data = normalize(payload)
        if not isinstance(data, dict):
            raise ValueError("Normalized executor chat event must be a JSON object")
        chunk = UpstreamChunk(data, protocol="ollama", raw=raw, content_type=content_type, original=payload)
        if emit is not None:
            await emit(chunk)
        if save_call:
            chunks.append(data)
        final_data = data
    return final_data
