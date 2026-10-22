"""Anthropic Messages <-> internal chat conversion helpers.

The internal request shape is OpenAI Chat Completions; worker results are
Ollama-shaped. These helpers are pure and contain no HTTP, Redis, or FastAPI
dependencies so both endpoints and workers can use them.
"""
from __future__ import annotations

import json
import math
import uuid
from typing import Any

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4096

_STOP_REASONS_INTERNAL_TO_ANTHROPIC = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}

_STOP_REASONS_ANTHROPIC_TO_INTERNAL = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "pause_turn": "stop",
    "refusal": "content_filter",
}

_HTTP_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "invalid_request_error",
    422: "invalid_request_error",
    429: "rate_limit_error",
    500: "api_error",
    502: "api_error",
    503: "overloaded_error",
    504: "timeout_error",
    529: "overloaded_error",
}


def messages_error_payload(error_type: str, message: str) -> dict:
    """Build an Anthropic error envelope."""
    return {"type": "error", "error": {"type": str(error_type), "message": str(message)}}


def messages_error_type_for_status(status_code: int) -> str:
    """Map an HTTP status to the closest Anthropic error type."""
    return _HTTP_ERROR_TYPES.get(int(status_code), "api_error")


def messages_error_type_for_code(code: str) -> str:
    """Map an aidir or upstream error code to an Anthropic error type."""
    normalized = str(code or "").strip().lower()
    if normalized in {"timeout", "queue_timeout", "upstream_timeout", "timeout_error"}:
        return "timeout_error"
    if normalized in {"unauthorized", "invalid_token", "authentication_error"}:
        return "authentication_error"
    if normalized in {"forbidden", "permission_error"}:
        return "permission_error"
    if normalized in {"invalid_model", "model_not_found", "not_found_error"}:
        return "not_found_error"
    if normalized in {"rate_limit", "rate_limit_error"}:
        return "rate_limit_error"
    if normalized in {"overloaded", "overloaded_error", "queue_error", "no_worker"}:
        return "overloaded_error"
    if normalized in {"invalid_request", "invalid_request_error", "validation_error"}:
        return "invalid_request_error"
    return "api_error"


def estimate_input_tokens(body: dict) -> int:
    """Return a deterministic rough token estimate for a Messages request."""
    pieces: list[str] = []
    system = body.get("system")
    if isinstance(system, str):
        pieces.append(system)
    elif isinstance(system, list):
        pieces.extend(_text_from_blocks(system))
    messages = body.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                pieces.append(content)
            elif isinstance(content, list):
                pieces.extend(_text_from_blocks(content))
    tools = body.get("tools")
    if tools:
        pieces.append(json.dumps(tools, ensure_ascii=False, sort_keys=True))
    characters = sum(len(piece) for piece in pieces)
    return max(1, int(math.ceil(characters / 4.0)))


def _text_from_blocks(blocks: object) -> list[str]:
    """Extract text values from Anthropic or OpenAI content blocks."""
    if not isinstance(blocks, list):
        return []
    out: list[str] = []
    for block in blocks:
        if isinstance(block, str):
            out.append(block)
        elif isinstance(block, dict):
            if isinstance(block.get("text"), str):
                out.append(block["text"])
            elif block.get("type") == "tool_result":
                out.extend(_text_from_blocks(block.get("content")))
    return out


def _coerce_text_content(content: object) -> str:
    """Return plain text from a string or a list of content blocks."""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, list):
        return "\n".join(text for text in _text_from_blocks(content) if text)
    if isinstance(content, dict):
        value = content.get("text")
        return value if isinstance(value, str) else ""
    return str(content)


def _data_url_parts(url: str) -> tuple[str, str] | None:
    """Split a base64 data URL into media type and payload."""
    if not url.startswith("data:") or ";base64," not in url:
        return None
    header, payload = url.split(",", 1)
    media_type = header[5:].split(";", 1)[0] or "application/octet-stream"
    return media_type, payload


def _openai_part_to_anthropic(part: object) -> dict | None:
    """Convert one OpenAI content part to an Anthropic content block."""
    if isinstance(part, str):
        return {"type": "text", "text": part}
    if not isinstance(part, dict):
        return None
    part_type = str(part.get("type") or "").strip().lower()
    if part_type in {"text", "input_text"}:
        text = part.get("text")
        return {"type": "text", "text": str(text or "")}
    if part_type in {"image_url", "input_image"}:
        image = part.get("image_url")
        if isinstance(image, dict):
            url = str(image.get("url") or "")
        else:
            url = str(image or "")
        if not url:
            return None
        data_parts = _data_url_parts(url)
        if data_parts is not None:
            media_type, payload = data_parts
            return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": payload}}
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _anthropic_block_to_openai(block: dict) -> dict | None:
    """Convert one Anthropic content block to an OpenAI content part."""
    block_type = str(block.get("type") or "").strip().lower()
    if block_type == "text":
        return {"type": "text", "text": str(block.get("text") or "")}
    if block_type == "image":
        source = block.get("source") if isinstance(block.get("source"), dict) else {}
        source_type = str(source.get("type") or "").strip().lower()
        if source_type == "base64":
            media_type = str(source.get("media_type") or "application/octet-stream")
            data = str(source.get("data") or "")
            return {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}}
        if source_type == "url":
            return {"type": "image_url", "image_url": {"url": str(source.get("url") or "")}}
        raise ValueError("Unsupported Anthropic image source; only base64 and url are supported")
    if block_type == "tool_result":
        return {"type": "tool_result"}
    return None


def messages_tools_to_internal(tools: object) -> list:
    """Convert Anthropic tool definitions to OpenAI function tools."""
    if not isinstance(tools, list):
        return []
    out: list[dict] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if "input_schema" in tool or ("name" in tool and "function" not in tool):
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": str(tool.get("name") or ""),
                        "description": str(tool.get("description") or ""),
                        "parameters": tool.get("input_schema") if isinstance(tool.get("input_schema"), dict) else {"type": "object"},
                    },
                }
            )
            continue
        out.append(tool)
    return out


def internal_tools_to_messages(tools: object) -> list:
    """Convert OpenAI function tools to Anthropic tool definitions."""
    if not isinstance(tools, list):
        return []
    out: list[dict] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") if isinstance(tool.get("function"), dict) else None
        if function is None:
            if "name" in tool:
                out.append(tool)
            continue
        parameters = function.get("parameters")
        out.append(
            {
                "name": str(function.get("name") or ""),
                "description": str(function.get("description") or ""),
                "input_schema": parameters if isinstance(parameters, dict) else {"type": "object"},
            }
        )
    return out


def messages_tool_choice_to_internal(choice: object) -> object:
    """Convert Anthropic tool_choice to the OpenAI representation."""
    if isinstance(choice, str):
        return choice
    if not isinstance(choice, dict):
        return choice
    choice_type = str(choice.get("type") or "").strip().lower()
    if choice_type == "auto":
        return "auto"
    if choice_type == "any":
        return "required"
    if choice_type == "none":
        return "none"
    if choice_type == "tool":
        return {"type": "function", "function": {"name": str(choice.get("name") or "")}}
    return choice


def internal_tool_choice_to_messages(choice: object) -> object:
    """Convert OpenAI tool_choice to the Anthropic representation."""
    if isinstance(choice, str):
        normalized = choice.strip().lower()
        if normalized in {"required", "any"}:
            return {"type": "any"}
        if normalized == "none":
            return {"type": "none"}
        if normalized == "auto":
            return {"type": "auto"}
        return {"type": "auto"}
    if not isinstance(choice, dict):
        return choice
    function = choice.get("function") if isinstance(choice.get("function"), dict) else {}
    name = str(function.get("name") or choice.get("name") or "")
    if name:
        return {"type": "tool", "name": name}
    return {"type": "auto"}


def _parse_arguments(value: object) -> dict:
    """Parse tool-call arguments into a JSON object."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _stringify_arguments(value: object) -> str:
    """Serialize tool-call arguments as a JSON string."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value if value is not None else {}, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


def messages_request_to_internal(body: dict, *, default_max_tokens: int | None = None) -> dict:
    """Convert a validated Anthropic Messages request into the internal request shape."""
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object")
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Field 'model' is required")
    max_tokens = body.get("max_tokens")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
        if default_max_tokens is None:
            raise ValueError("Field 'max_tokens' must be a positive integer")
        max_tokens = int(default_max_tokens)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("Field 'messages' must be a non-empty array")

    internal_messages: list[dict] = []
    system_text = _coerce_text_content(body.get("system"))
    if system_text:
        internal_messages.append({"role": "system", "content": system_text})

    pending_tool_results: list[dict] = []
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("Each message must be an object")
        role = str(message.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            raise ValueError(f"Unsupported message role: {role or '<empty>'}")
        content = message.get("content")
        if isinstance(content, str):
            if pending_tool_results:
                internal_messages.extend(pending_tool_results)
                pending_tool_results = []
            internal_messages.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            raise ValueError("Message content must be a string or an array of content blocks")

        text_parts: list[dict] = []
        tool_calls: list[dict] = []
        opaque_blocks: list[dict] = []
        has_tool_result = False
        for block in content:
            if not isinstance(block, dict):
                raise ValueError("Each content block must be an object")
            block_type = str(block.get("type") or "").strip().lower()
            if block_type == "text":
                text_parts.append({"type": "text", "text": str(block.get("text") or "")})
            elif block_type == "image":
                converted = _anthropic_block_to_openai(block)
                if converted is not None:
                    text_parts.append(converted)
            elif block_type == "tool_use":
                opaque_blocks.append(block)
                tool_calls.append(
                    {
                        "id": str(block.get("id") or f"call_{uuid.uuid4().hex[:12]}"),
                        "type": "function",
                        "function": {
                            "name": str(block.get("name") or ""),
                            "arguments": _stringify_arguments(block.get("input")),
                        },
                    }
                )
            elif block_type == "tool_result":
                has_tool_result = True
                if pending_tool_results:
                    internal_messages.extend(pending_tool_results)
                pending_tool_results = []
                result_content = _coerce_text_content(block.get("content"))
                pending_tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(block.get("tool_use_id") or ""),
                        "content": result_content,
                        "is_error": bool(block.get("is_error", False)),
                    }
                )
            elif block_type in {"thinking", "redacted_thinking"}:
                opaque_blocks.append(block)
            else:
                raise ValueError(f"Unsupported content block type: {block_type or '<empty>'}")

        if has_tool_result:
            if pending_tool_results:
                internal_messages.extend(pending_tool_results)
                pending_tool_results = []
            text_content = "\n".join(part["text"] for part in text_parts if part.get("type") == "text" and part.get("text"))
            if text_content:
                internal_messages.append({"role": "user", "content": text_content})
            continue

        if pending_tool_results:
            internal_messages.extend(pending_tool_results)
            pending_tool_results = []

        message_out: dict = {"role": role, "content": text_parts if text_parts else ""}
        if tool_calls:
            message_out["tool_calls"] = tool_calls
        if opaque_blocks:
            message_out["anthropic_blocks"] = opaque_blocks
            thinking_text = "".join(
                str(block.get("thinking") or "")
                for block in opaque_blocks
                if str(block.get("type") or "") == "thinking"
            )
            if thinking_text:
                message_out["reasoning_content"] = thinking_text
        internal_messages.append(message_out)

    if pending_tool_results:
        internal_messages.extend(pending_tool_results)

    out: dict = {
        "model": model.strip(),
        "messages": internal_messages,
        "stream": bool(body.get("stream", False)),
        "max_completion_tokens": max_tokens,
    }
    tools = messages_tools_to_internal(body.get("tools"))
    if tools:
        out["tools"] = tools
    if "tool_choice" in body:
        out["tool_choice"] = messages_tool_choice_to_internal(body.get("tool_choice"))
    stop_sequences = body.get("stop_sequences")
    if isinstance(stop_sequences, str):
        out["stop"] = [stop_sequences]
    elif isinstance(stop_sequences, list):
        out["stop"] = [str(item) for item in stop_sequences]
    for field in ("temperature", "top_p", "top_k"):
        if body.get(field) is not None:
            out[field] = body[field]
    if isinstance(body.get("thinking"), dict):
        out["thinking"] = dict(body["thinking"])
    for field in ("envid", "context_builder", "worker", "priority", "queue_timeout", "timeout", "log"):
        if body.get(field) is not None:
            out[field] = body[field]
    return out


def _internal_message_content_to_anthropic(content: object) -> list[dict]:
    """Convert internal message content to Anthropic content blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if isinstance(content, list):
        out: list[dict] = []
        for part in content:
            converted = _openai_part_to_anthropic(part)
            if converted is not None:
                out.append(converted)
        return out
    if content is None:
        return []
    return [{"type": "text", "text": str(content)}]


def internal_request_to_messages(payload: dict, *, default_max_tokens: int | None = None) -> dict:
    """Convert an internal OpenAI-shaped request into an Anthropic Messages request."""
    if not isinstance(payload, dict):
        raise ValueError("Internal payload must be an object")
    model = str(payload.get("model") or "").strip()
    if not model:
        raise ValueError("Internal payload has no model")

    system_parts: list[str] = []
    messages: list[dict] = []
    pending_tool_results: list[dict] = []

    def flush_tool_results() -> None:
        """Append accumulated OpenAI tool results as one Anthropic user turn."""
        if not pending_tool_results:
            return
        blocks = list(pending_tool_results)
        pending_tool_results.clear()
        messages.append({"role": "user", "content": blocks})

    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip().lower()
        content = message.get("content")
        if role in {"system", "developer"}:
            flush_tool_results()
            text = _coerce_text_content(content)
            if text:
                system_parts.append(text)
            continue
        if role == "tool":
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": str(message.get("tool_call_id") or message.get("name") or ""),
                    "content": _coerce_text_content(content),
                    **({"is_error": True} if message.get("is_error") else {}),
                }
            )
            continue

        flush_tool_results()
        if role == "assistant":
            opaque = message.get("anthropic_blocks")
            if isinstance(opaque, list) and opaque:
                messages.append({"role": "assistant", "content": [block for block in opaque if isinstance(block, dict)]})
                continue
            blocks = _internal_message_content_to_anthropic(content)
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(call.get("id") or f"toolu_{uuid.uuid4().hex[:12]}"),
                        "name": str(function.get("name") or call.get("name") or ""),
                        "input": _parse_arguments(function.get("arguments")),
                    }
                )
            messages.append({"role": "assistant", "content": blocks})
            continue

        blocks = _internal_message_content_to_anthropic(content)
        messages.append({"role": "user", "content": blocks})

    flush_tool_results()

    out: dict = {
        "model": model,
        "max_tokens": _resolve_max_tokens(payload, default_max_tokens),
        "messages": messages,
    }
    if system_parts:
        out["system"] = "\n\n".join(system_parts)
    if payload.get("stream"):
        out["stream"] = True
    tools = internal_tools_to_messages(payload.get("tools"))
    if tools:
        out["tools"] = tools
    if "tool_choice" in payload:
        out["tool_choice"] = internal_tool_choice_to_messages(payload.get("tool_choice"))
    if isinstance(payload.get("thinking"), dict):
        out["thinking"] = dict(payload["thinking"])
    for source, target in (
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("top_k", "top_k"),
    ):
        if payload.get(source) is not None:
            out[target] = payload[source]
    stop = payload.get("stop")
    if isinstance(stop, str):
        out["stop_sequences"] = [stop]
    elif isinstance(stop, list) and stop:
        out["stop_sequences"] = [str(item) for item in stop]
    for field in ("metadata",):
        if isinstance(payload.get(field), dict):
            out[field] = payload[field]
    for field in ("envid", "context_builder", "worker", "priority", "queue_timeout", "timeout", "log"):
        if payload.get(field) is not None:
            out[field] = payload[field]
    return out


def _resolve_max_tokens(payload: dict, default_max_tokens: int | None) -> int:
    """Resolve a required Anthropic max_tokens value from internal generation fields."""
    options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
    for value in (
        payload.get("max_completion_tokens"),
        payload.get("max_tokens"),
        payload.get("num_predict"),
        options.get("num_predict"),
    ):
        if isinstance(value, bool) or value is None:
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    if default_max_tokens is not None and int(default_max_tokens) > 0:
        return int(default_max_tokens)
    return DEFAULT_MAX_TOKENS


def _anthropic_usage_to_internal(usage: object) -> dict:
    """Convert Anthropic usage counters to the internal usage shape."""
    source = usage if isinstance(usage, dict) else {}
    input_tokens = int(source.get("input_tokens") or 0)
    output_tokens = int(source.get("output_tokens") or 0)
    out = {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    for field in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        if source.get(field) is not None:
            out[field] = source[field]
    return out


def _internal_usage_to_anthropic(result: dict) -> dict:
    """Convert internal or OpenAI usage counters to Anthropic usage."""
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    input_tokens = usage.get("prompt_tokens")
    if input_tokens is None:
        input_tokens = result.get("prompt_eval_count")
    output_tokens = usage.get("completion_tokens")
    if output_tokens is None:
        output_tokens = result.get("eval_count")
    out = {
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
    }
    for field in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        if usage.get(field) is not None:
            out[field] = usage[field]
    return out


def _finish_reason_to_stop_reason(finish_reason: object, *, stop_sequence: object = None) -> str:
    """Map an internal finish reason to an Anthropic stop reason."""
    normalized = str(finish_reason or "").strip().lower()
    if normalized == "stop" and stop_sequence:
        return "stop_sequence"
    return _STOP_REASONS_INTERNAL_TO_ANTHROPIC.get(normalized, "end_turn")


def _stop_reason_to_finish_reason(stop_reason: object) -> str:
    """Map an Anthropic stop reason to an internal finish reason."""
    return _STOP_REASONS_ANTHROPIC_TO_INTERNAL.get(str(stop_reason or "").strip().lower(), "stop")


def messages_response_to_internal(data: dict, *, streaming: bool = False) -> dict:
    """Convert one Anthropic response or final message to the Ollama-shaped result."""
    if not isinstance(data, dict):
        raise ValueError("Anthropic response must be a JSON object")
    content_blocks = data.get("content") if isinstance(data.get("content"), list) else []
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[dict] = []
    opaque: list[dict] = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "").strip().lower()
        if block_type == "text":
            text_parts.append(str(block.get("text") or ""))
        elif block_type == "tool_use":
            opaque.append(block)
            tool_calls.append(
                {
                    "id": str(block.get("id") or f"call_{uuid.uuid4().hex[:12]}"),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name") or ""),
                        "arguments": _stringify_arguments(block.get("input")),
                    },
                }
            )
        elif block_type in {"thinking", "redacted_thinking"}:
            opaque.append(block)
            if block_type == "thinking":
                thinking_parts.append(str(block.get("thinking") or ""))

    message: dict = {
        "role": "assistant",
        "content": "".join(text_parts),
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    if thinking_parts:
        message["thinking"] = "".join(thinking_parts)
    if opaque:
        message["anthropic_blocks"] = opaque
    stop_reason = data.get("stop_reason")
    usage = _anthropic_usage_to_internal(data.get("usage"))
    return {
        "id": str(data.get("id") or f"msg_{uuid.uuid4().hex}"),
        "model": str(data.get("model") or ""),
        "created_at": data.get("created_at") or "",
        "message": message,
        "done": True,
        "done_reason": _stop_reason_to_finish_reason(stop_reason),
        "stop_reason": stop_reason,
        "usage": usage,
        "prompt_eval_count": usage["prompt_tokens"],
        "eval_count": usage["completion_tokens"],
    }


def internal_result_to_messages(
    result: dict,
    *,
    model: str,
    task_id: str = "",
    include_thinking: bool = False,
) -> dict:
    """Convert an internal Ollama-shaped result into an Anthropic Messages response."""
    if isinstance(result, dict) and result.get("type") == "message" and isinstance(result.get("content"), list):
        return result
    source = result if isinstance(result, dict) else {}
    message = source.get("message") if isinstance(source.get("message"), dict) else {}
    if not message and isinstance(source.get("choices"), list) and source["choices"]:
        choice = source["choices"][0] if isinstance(source["choices"][0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}

    content: list[dict] = []
    opaque = message.get("anthropic_blocks")
    if isinstance(opaque, list) and opaque:
        content = [block for block in opaque if isinstance(block, dict)]
    else:
        text = _coerce_text_content(message.get("content"))
        if text:
            content.append({"type": "text", "text": text})
        if include_thinking and message.get("thinking"):
            content.append({"type": "thinking", "thinking": str(message["thinking"]), "signature": ""})
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            content.append(
                {
                    "type": "tool_use",
                    "id": str(call.get("id") or f"toolu_{uuid.uuid4().hex[:12]}"),
                    "name": str(function.get("name") or call.get("name") or ""),
                    "input": _parse_arguments(function.get("arguments")),
                }
            )

    finish_reason = source.get("done_reason") or source.get("stop_reason")
    stop_sequence = source.get("stop_sequence")
    response_id = str(source.get("id") or "")
    if not response_id.startswith("msg_"):
        response_id = f"msg_{task_id or uuid.uuid4().hex}"
    return {
        "id": response_id,
        "type": "message",
        "role": "assistant",
        "model": str(model or source.get("model") or ""),
        "content": content,
        "stop_reason": _finish_reason_to_stop_reason(finish_reason, stop_sequence=stop_sequence),
        "stop_sequence": stop_sequence,
        "usage": _internal_usage_to_anthropic(source),
    }


class AnthropicStreamDecoder:
    """Convert Anthropic SSE events into internal chunks and a final result."""

    def __init__(self) -> None:
        """Create an empty decoder state."""
        self.message: dict = {}
        self.usage: dict = {}
        self.stop_reason: str | None = None
        self.stop_sequence: str | None = None
        self.content_parts: list[str] = []
        self.thinking_parts: list[str] = []
        self.tool_calls: list[dict] = []
        self.opaque_blocks: list[dict] = []
        self._open_blocks: dict[int, dict] = {}

    def feed(self, event: dict) -> dict | None:
        """Consume one event and return an internal delta when it carries content."""
        if not isinstance(event, dict):
            return None
        event_type = str(event.get("type") or "").strip()
        if event_type == "error":
            detail = event.get("error") if isinstance(event.get("error"), dict) else {}
            raise RuntimeError(str(detail.get("message") or "Anthropic stream error"))
        if event_type == "message_start":
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            self.message = message
            self.usage.update(message.get("usage") if isinstance(message.get("usage"), dict) else {})
            self.stop_reason = message.get("stop_reason")
            return None
        if event_type == "content_block_start":
            index = int(event.get("index") or 0)
            block = event.get("content_block") if isinstance(event.get("content_block"), dict) else {}
            self._open_blocks[index] = dict(block)
            block_type = str(block.get("type") or "").strip().lower()
            if block_type in {"thinking", "redacted_thinking"}:
                value = str(block.get("thinking") or "")
                if value:
                    self.thinking_parts.append(value)
                    return {"message": {"role": "assistant", "thinking": value}, "done": False}
            return None
        if event_type == "content_block_delta":
            index = int(event.get("index") or 0)
            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
            delta_type = str(delta.get("type") or "").strip().lower()
            if delta_type == "text_delta":
                text = str(delta.get("text") or "")
                if text:
                    self.content_parts.append(text)
                    return {"message": {"role": "assistant", "content": text}, "done": False}
            if delta_type == "thinking_delta":
                text = str(delta.get("thinking") or "")
                if text:
                    self.thinking_parts.append(text)
                    return {"message": {"role": "assistant", "thinking": text}, "done": False}
            if delta_type == "signature_delta":
                block = self._open_blocks.get(index)
                if isinstance(block, dict):
                    block["signature"] = str(delta.get("signature") or "")
            if delta_type == "input_json_delta":
                block = self._open_blocks.get(index)
                if isinstance(block, dict):
                    block["_partial_json"] = str(block.get("_partial_json") or "") + str(delta.get("partial_json") or "")
            return None
        if event_type == "content_block_stop":
            index = int(event.get("index") or 0)
            block = self._open_blocks.pop(index, None)
            if not isinstance(block, dict):
                return None
            block_type = str(block.get("type") or "").strip().lower()
            if block_type == "tool_use":
                arguments = _parse_arguments(block.get("_partial_json") or block.get("input") or {})
                call = {
                    "id": str(block.get("id") or f"call_{uuid.uuid4().hex[:12]}"),
                    "type": "function",
                    "function": {"name": str(block.get("name") or ""), "arguments": _stringify_arguments(arguments)},
                }
                self.tool_calls.append(call)
                return {"message": {"role": "assistant", "tool_calls": [call]}, "done": False}
            if block_type in {"thinking", "redacted_thinking"}:
                clean = {key: value for key, value in block.items() if not str(key).startswith("_")}
                self.opaque_blocks.append(clean)
            return None
        if event_type == "message_delta":
            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
            if delta.get("stop_reason") is not None:
                self.stop_reason = str(delta.get("stop_reason") or "")
            if delta.get("stop_sequence") is not None:
                self.stop_sequence = str(delta.get("stop_sequence") or "")
            self.usage.update(event.get("usage") if isinstance(event.get("usage"), dict) else {})
            return None
        if event_type == "message_stop":
            return {"done": True}
        return None

    def result(self) -> dict:
        """Return the final internal result assembled from all fed events."""
        message: dict = {
            "role": "assistant",
            "content": "".join(self.content_parts),
        }
        if self.thinking_parts:
            message["thinking"] = "".join(self.thinking_parts)
        if self.tool_calls:
            message["tool_calls"] = list(self.tool_calls)
        if self.opaque_blocks:
            message["anthropic_blocks"] = list(self.opaque_blocks)
        usage = _anthropic_usage_to_internal(self.usage)
        return {
            "id": str(self.message.get("id") or f"msg_{uuid.uuid4().hex}"),
            "model": str(self.message.get("model") or ""),
            "message": message,
            "done": True,
            "done_reason": _stop_reason_to_finish_reason(self.stop_reason),
            "stop_reason": self.stop_reason,
            "stop_sequence": self.stop_sequence,
            "usage": usage,
            "prompt_eval_count": usage["prompt_tokens"],
            "eval_count": usage["completion_tokens"],
        }


class AnthropicStreamEncoder:
    """Convert internal chunks into a valid Anthropic SSE event sequence."""

    def __init__(self, *, model: str, message_id: str, include_thinking: bool = False) -> None:
        """Create an encoder that has not emitted message_start yet."""
        self.model = str(model or "")
        self.message_id = str(message_id or f"msg_{uuid.uuid4().hex}")
        self.include_thinking = bool(include_thinking)
        self.started = False
        self.stopped = False
        self.block_index = -1
        self.open_block: str | None = None
        self.input_tokens = 0
        self.output_tokens = 0
        self.stop_reason = "end_turn"
        self.stop_sequence: str | None = None
        self._tool_calls: dict[int, dict] = {}

    def _message_start(self) -> dict:
        """Build and mark the initial message_start event."""
        self.started = True
        return {
            "type": "message_start",
            "message": {
                "id": self.message_id,
                "type": "message",
                "role": "assistant",
                "model": self.model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": self.input_tokens, "output_tokens": 0},
            },
        }

    def _close_block(self) -> list[dict]:
        """Close the currently open content block when present."""
        if self.open_block is None:
            return []
        event = {"type": "content_block_stop", "index": self.block_index}
        self.open_block = None
        return [event]

    def _start_block(self, block_type: str, content_block: dict) -> list[dict]:
        """Close the previous block and start a new one."""
        events = self._close_block()
        self.block_index += 1
        self.open_block = block_type
        events.append({"type": "content_block_start", "index": self.block_index, "content_block": content_block})
        return events

    def feed(self, chunk: dict) -> list[dict]:
        """Convert one internal chunk into zero or more Anthropic events."""
        if self.stopped or not isinstance(chunk, dict):
            return []
        events: list[dict] = []
        if not self.started:
            events.append(self._message_start())
        message = chunk.get("message") if isinstance(chunk.get("message"), dict) else {}
        text = message.get("content")
        if isinstance(text, str) and text:
            if self.open_block != "text":
                events.extend(self._start_block("text", {"type": "text", "text": ""}))
            events.append({"type": "content_block_delta", "index": self.block_index, "delta": {"type": "text_delta", "text": text}})
        thinking = message.get("thinking")
        if self.include_thinking and isinstance(thinking, str) and thinking:
            if self.open_block != "thinking":
                events.extend(self._start_block("thinking", {"type": "thinking", "thinking": ""}))
            events.append({"type": "content_block_delta", "index": self.block_index, "delta": {"type": "thinking_delta", "thinking": thinking}})
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            call_id = str(call.get("id") or "")
            if not call_id:
                call_id = f"toolu_{uuid.uuid4().hex[:12]}"
            if call_id not in self._tool_calls:
                self._tool_calls[call_id] = call
                events.extend(
                    self._start_block(
                        "tool_use",
                        {"type": "tool_use", "id": call_id, "name": str(function.get("name") or ""), "input": {}},
                    )
                )
                arguments = _stringify_arguments(function.get("arguments"))
                if arguments:
                    events.append(
                        {
                            "type": "content_block_delta",
                            "index": self.block_index,
                            "delta": {"type": "input_json_delta", "partial_json": arguments},
                        }
                    )
                events.extend(self._close_block())

        usage = chunk.get("usage") if isinstance(chunk.get("usage"), dict) else {}
        if usage:
            self.input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or self.input_tokens or 0)
            self.output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or self.output_tokens or 0)
        if chunk.get("done"):
            self.stop_reason = _finish_reason_to_stop_reason(chunk.get("done_reason") or chunk.get("stop_reason"), stop_sequence=chunk.get("stop_sequence"))
            self.stop_sequence = chunk.get("stop_sequence")
            events.extend(self._close_block())
            events.append(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": self.stop_reason, "stop_sequence": self.stop_sequence},
                    "usage": {"output_tokens": self.output_tokens},
                }
            )
            events.append({"type": "message_stop"})
            self.stopped = True
        return events

    def finish(self) -> list[dict]:
        """Finish a stream that ended without an explicit done chunk."""
        if self.stopped:
            return []
        return self.feed({"done": True})


def sse_event_bytes(event: dict) -> bytes:
    """Serialize one Anthropic event as an SSE frame."""
    return f"event: {event.get('type', 'message')}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode()
