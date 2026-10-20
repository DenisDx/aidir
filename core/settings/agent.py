"""OpenAI-compatible assistant for the Settings page.

The model never writes the configuration. It reads authorised nodes through the
aiset Service, proposes operations, and hands them back for the browser to
stage: the user reviews the staged batch and presses **Apply**, exactly as with
a manual edit. The provider call is made here, in the backend, so the API key
never reaches the browser.

Ported from the aiset `demo2` host agent, with the host-specific parts replaced:
the provider is this project's own openaix endpoint (`ai.provider.base_url`),
the document comes from `ai.document`, and a conversation belongs to the WebUI
session that started it rather than to a host-supplied header.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from aiset.errors import AisetError

SYSTEM_PROMPT = """You are the configuration assistant of the AI Director WebUI.

Paths are JSON Pointers. The configuration root is the empty pointer "" (the
whole document); a path always starts with "/". Address sections directly, for
example /webui/port or /models/items/0.

You help the user inspect and change this project's configuration. Rules:
- Read with settings_get or settings_get_tree before proposing a change.
- Never invent paths; only paths declared by the descriptor exist.
- Propose changes as operations: {"op":"set","path":"/a/b","value_text":"<JSON5>"},
  {"op":"add","path":"/a/b/-","value_text":"<JSON5>"},
  {"op":"remove","path":"/a/b/0"}, {"op":"move","from":"/a/b/2","path":"/a/b/0"}.
- value_text is exactly one JSON5 value; strings must be quoted.
- Call settings_validate_operations to check a proposal, then
  settings_stage_operations to hand the operations to the user.
- The staged operations are applied by the user in the editor. You cannot write
  the file yourself and must say so when asked directly.
- Never print secret values. A node marked `secret` reads as "hidden"; tell the
  user it is set and leave the value alone.
- Keep every unrelated setting, comment and placeholder untouched.
- Answer in the language of the user's message, briefly and concretely.
"""

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "settings_get",
            "description": (
                "Read one authorized configuration node as source text. Use the empty path for "
                "the whole document; a path always starts with '/'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "JSON Pointer, for example /webui/port"},
                    "view": {"type": "string", "enum": ["source", "effective"], "default": "source"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "settings_get_tree",
            "description": (
                "Read a bounded subtree with descriptor metadata and capabilities. Use the empty "
                "path for the configuration root. The result is bounded: if it is marked "
                "'truncated', read the sections you need with their own paths instead of the root."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "JSON Pointer of the subtree root"},
                    "view": {"type": "string", "enum": ["source", "effective"], "default": "source"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "settings_validate_operations",
            "description": "Validate proposed operations without writing anything.",
            "parameters": {
                "type": "object",
                "properties": {
                    "operations": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Operations to validate",
                    }
                },
                "required": ["operations"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "settings_stage_operations",
            "description": (
                "Return validated operations to the browser for staging. The user applies them; "
                "this call never writes the configuration."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "operations": {"type": "array", "items": {"type": "object"}},
                    "note": {"type": "string", "description": "Short explanation for the user"},
                },
                "required": ["operations"],
            },
        },
    },
]

# The assistant may read non-secret values and validate proposals, nothing else.
AGENT_PERMISSIONS = ("settings_read_source", "settings_read_effective", "settings_validate")
MAX_TOOL_CONTENT = 16000
MAX_TREE_DEPTH = 3
MAX_HISTORY_CHARS = 24000

AGENT_ERROR_STATUS = {
    "BAD_REQUEST": 400,
    "AGENT_BAD_ARGUMENTS": 400,
    "AGENT_TOOL_ERROR": 400,
    "PROVIDER_HTTP_ERROR": 502,
    "PROVIDER_UNREACHABLE": 503,
    "PROVIDER_TIMEOUT": 504,
    "PROVIDER_PROTOCOL": 502,
    "AGENT_ERROR": 502,
}


class AgentError(RuntimeError):
    """A provider or protocol failure; reported to the user, never hidden."""

    def __init__(self, message: str, *, code: str = "AGENT_ERROR", detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass
class ConfigurationAgent:
    """OpenAI-compatible configuration assistant with a bounded tool loop."""

    service: Any = None
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    document: str = "main"
    timeout_seconds: float = 60.0
    temperature: float = 0.2
    max_tool_rounds: int = 8
    history_messages: int = 24
    principal: Any = None
    sessions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.principal is None:
            # The assistant never receives secret permissions, so a tool result
            # can never carry a secret value.
            from aiset.service import Principal

            self.principal = Principal(id="aidir-settings-agent", permissions=frozenset(AGENT_PERMISSIONS))

    # -- public ---------------------------------------------------------
    def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Process one user message and return the chat response."""
        message = str(payload.get("message") or "").strip()
        if not message:
            raise AgentError("message is required", code="BAD_REQUEST")
        session = str(payload.get("session") or "default")[:64]
        history = self.sessions.setdefault(session, [])
        history.append({"role": "user", "content": message})
        self._trim(history)
        staged: list[dict[str, Any]] = []
        activity: list[dict[str, Any]] = []
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history]

        for round_index in range(max(1, self.max_tool_rounds)):
            reply = self._complete(messages)
            choice = reply["choices"][0]["message"]
            messages.append(choice)
            calls = choice.get("tool_calls") or []
            if not calls:
                history.append({"role": "assistant", "content": choice.get("content") or ""})
                self._trim(history)
                return {
                    "message": choice.get("content") or "",
                    "staged_operations": staged,
                    "activity": activity,
                    "rounds": round_index + 1,
                    "model": self.model,
                }
            for call in calls:
                name = call["function"]["name"]
                try:
                    arguments = _parse_arguments(call["function"].get("arguments"))
                except AgentError as exc:
                    # A malformed tool call is recoverable: the model is told to
                    # resend it as JSON instead of losing the whole answer.
                    activity.append({"tool": name, "arguments": {"error": str(exc)}})
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "content": json.dumps(
                                {
                                    "ok": False,
                                    "code": "AGENT_BAD_ARGUMENTS",
                                    "message": f"{exc}. Call the tool again with arguments as a JSON object.",
                                }
                            ),
                        }
                    )
                    continue
                result = self._run_tool(name, arguments, staged, activity)
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": result})
        history.append(
            {"role": "assistant", "content": "The tool round limit was reached before a final answer."}
        )
        return {
            "message": "The tool round limit was reached before a final answer.",
            "staged_operations": staged,
            "activity": activity,
            "rounds": self.max_tool_rounds,
            "model": self.model,
        }

    def history(self, session: str) -> list[dict[str, Any]]:
        """Return the stored turns of one conversation."""
        return list(self.sessions.get(session or "default", []))

    def forget(self, session: str) -> None:
        """Drop one conversation."""
        self.sessions.pop(session or "default", None)

    # -- provider -------------------------------------------------------
    def _complete(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """One Chat Completions request; HTTP and protocol errors are explicit."""
        body = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "tools": TOOLS,
                "tool_choice": "auto",
                "temperature": self.temperature,
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:400]
            raise AgentError(
                f"Provider returned HTTP {exc.code}: {_provider_message(body) or exc.reason}",
                code="PROVIDER_HTTP_ERROR",
                detail=body,
            ) from exc
        except urllib.error.URLError as exc:
            raise AgentError(f"Provider is unreachable: {exc.reason}", code="PROVIDER_UNREACHABLE") from exc
        except TimeoutError as exc:
            raise AgentError("Provider timed out", code="PROVIDER_TIMEOUT") from exc
        except json.JSONDecodeError as exc:
            raise AgentError("Provider returned malformed JSON", code="PROVIDER_PROTOCOL") from exc
        if not isinstance(payload.get("choices"), list) or not payload["choices"]:
            raise AgentError("Provider response has no choices", code="PROVIDER_PROTOCOL")
        return payload

    # -- tools ----------------------------------------------------------
    def _run_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        staged: list[dict[str, Any]],
        activity: list[dict[str, Any]],
    ) -> str:
        """Execute one tool call and return its bounded JSON result."""
        try:
            if name == "settings_get":
                result = self.service.get(
                    self.document,
                    _path(arguments),
                    view=str(arguments.get("view") or "source"),
                    principal=self.principal,
                ).to_dict()
            elif name == "settings_get_tree":
                result = self.service.tree(
                    self.document,
                    _path(arguments),
                    view=str(arguments.get("view") or "source"),
                    principal=self.principal,
                )
            elif name == "settings_validate_operations":
                operations = _operations(arguments)
                outcome = self.service.apply_operations(
                    self.document,
                    operations,
                    expected_revision=self._revision(),
                    expected_descriptor_revision=self._descriptor_revision(),
                    principal=self.principal,
                    dry_run=True,
                )
                result = {"valid": outcome.valid, "issues": [i.to_dict() for i in outcome.issues]}
            elif name == "settings_stage_operations":
                operations = _operations(arguments)
                outcome = self.service.apply_operations(
                    self.document,
                    operations,
                    expected_revision=self._revision(),
                    expected_descriptor_revision=self._descriptor_revision(),
                    principal=self.principal,
                    dry_run=True,
                )
                if not outcome.valid:
                    result = {
                        "staged": False,
                        "valid": False,
                        "issues": [i.to_dict() for i in outcome.issues],
                        "note": str(arguments.get("note") or ""),
                    }
                else:
                    staged.extend(operations)
                    result = {
                        "staged": True,
                        "valid": True,
                        "operations": operations,
                        "note": str(arguments.get("note") or ""),
                    }
            else:
                raise AgentError(f"Unknown tool {name!r}", code="AGENT_TOOL_ERROR")
        except AisetError as exc:
            result = {"ok": False, "code": exc.code, "issues": exc.issues, "message": exc.message}
            if exc.code == "NOT_FOUND":
                # Tell the model how to address the configuration instead of
                # leaving it to guess (a model naturally writes "/" for the root).
                sections = self._sections()
                if sections:
                    result["available_sections"] = sections
                    result["hint"] = (
                        'The configuration root is the empty path "". '
                        "Sections: " + ", ".join("/" + name for name in sections)
                    )
        except AgentError as exc:
            result = {"ok": False, "code": exc.code, "message": str(exc)}
        activity.append({"tool": name, "arguments": _safe_arguments(arguments)})
        return _tool_content(name, result)

    def _sections(self) -> list[str]:
        """Top-level section names declared by the descriptor."""
        try:
            root = self.service.documents[self.document].descriptor.root
        except (AttributeError, KeyError):
            return []
        return sorted(root.children or {})

    def _revision(self) -> str:
        return self.service.load(self.document, principal=self.principal).revision

    def _descriptor_revision(self) -> str:
        return self.service.documents[self.document].descriptor.revision

    # -- history --------------------------------------------------------
    def _trim(self, history: list[dict[str, Any]]) -> None:
        while len(history) > max(2, self.history_messages):
            history.pop(0)
        while sum(len(str(item.get("content") or "")) for item in history) > MAX_HISTORY_CHARS and history:
            history.pop(0)


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------
def build_agent(service: Any, effective: dict[str, Any]) -> ConfigurationAgent | None:
    """Build the assistant from the effective configuration, or None if disabled.

    Never raises for a missing provider: the pane reports "disabled" and the
    rest of the WebUI is unaffected, which is what a configuration switch is for.
    """
    settings = effective.get("ai") or {}
    if not settings.get("enabled"):
        return None
    base_url = str((settings.get("provider") or {}).get("base_url") or "").strip()
    model = str(settings.get("model") or "").strip()
    if not base_url or not model:
        return None
    return ConfigurationAgent(
        service=service,
        base_url=base_url,
        api_key=str((settings.get("provider") or {}).get("api_key") or ""),
        model=model,
        document=str(settings.get("document") or "main"),
        timeout_seconds=float(settings.get("timeout_seconds") or 60),
        temperature=float(
            settings.get("temperature") if settings.get("temperature") is not None else 0.2
        ),
        max_tool_rounds=int(settings.get("max_tool_rounds") or 8),
        history_messages=int(settings.get("history_messages") or 24),
    )


def describe(agent: ConfigurationAgent | None) -> dict[str, Any]:
    """Host-facing summary for the page: whether the assistant runs, and on what."""
    if agent is None:
        return {"enabled": False, "model": None}
    return {"enabled": True, "model": agent.model, "document": agent.document}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _path(arguments: dict[str, Any]) -> str:
    """Normalize a model-supplied path to a canonical JSON Pointer.

    The root is the empty pointer; models usually write "/" (or a bare
    `webui/port`), so those forms are mapped onto the canonical spelling
    instead of failing as undeclared paths.
    """
    text = str(arguments.get("path") or "").strip()
    if not text or text in ("/", "#", "root", "/root"):
        return ""
    if not text.startswith("/"):
        text = "/" + text
    return text.rstrip("/")


def _tool_content(name: str, result: dict[str, Any]) -> str:
    """Serialize a tool result without ever cutting JSON in half.

    A tree is bounded structurally (depth and a character budget) instead of
    being cut mid-text, so the model always receives usable JSON.
    """
    if name == "settings_get_tree" and isinstance(result, dict) and "node" in result:
        bounded = _bounded_node(result["node"], [MAX_TOOL_CONTENT])
        return json.dumps(
            {**result, "node": bounded, "truncated": bounded.get("truncated", False)},
            ensure_ascii=False,
            default=str,
        )
    payload = json.dumps(result, ensure_ascii=False, default=str)
    if len(payload) <= MAX_TOOL_CONTENT:
        return payload
    summary = {
        "truncated": True,
        "reason": "The subtree exceeded the bounded tool context",
        "preview": payload[:MAX_TOOL_CONTENT // 4],
    }
    return json.dumps(summary, ensure_ascii=False, default=str)


def _bounded_node(node: dict[str, Any], budget: list[int], depth: int = 0) -> Any:
    """Keep the tree structured and bounded by depth and a character budget."""
    entry: dict[str, Any] = {
        "path": node.get("path"),
        "type": node.get("type"),
        "kind": node.get("kind"),
        "exists": node.get("exists"),
        "value_text": node.get("value_text"),
        "access_denied": node.get("access_denied", False),
        "capabilities": node.get("capabilities"),
        "children": [],
    }
    budget[0] -= _size(entry)
    children = list(node.get("children") or []) + list(node.get("items") or [])
    if depth >= MAX_TREE_DEPTH:
        if children:
            entry["truncated"] = True
            entry["children"] = [{"remaining": len(children)}]
        return entry
    for child in children:
        if budget[0] <= 0:
            entry["truncated"] = True
            entry["children"].append({"remaining": len(children) - len(entry["children"])})
            break
        entry["children"].append(_bounded_node(child, budget, depth + 1))
    return entry


def _size(value: Any) -> int:
    """Serialized size of a value in characters."""
    return len(json.dumps(value, ensure_ascii=False, default=str))


def _provider_message(body: str) -> str:
    """Extract the provider's own error message, for example a missing model."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body[:200]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:200]
    return str(error or payload)[:200]


def _operations(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate the operations argument the model supplied."""
    operations = arguments.get("operations")
    if not isinstance(operations, list) or not all(isinstance(item, dict) for item in operations):
        raise AgentError("'operations' must be an array of operation objects", code="AGENT_TOOL_ERROR")
    return operations


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """Parse tool arguments, tolerating the sloppy JSON some models emit."""
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    for candidate in (text, _relaxed_json(text)):
        try:
            value = json.loads(candidate or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise AgentError("Tool arguments are not a JSON object", code="AGENT_BAD_ARGUMENTS")


def _relaxed_json(text: str) -> str:
    """Last-resort repair: single-quoted keys and values become double-quoted."""
    if "'" not in text:
        return text
    return re.sub(r"'([^'\\]*)'", lambda m: json.dumps(m.group(1)), text)


def _safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Arguments may contain configuration values, never secrets from .env."""
    return {key: value for key, value in arguments.items() if key != "api_key"}
