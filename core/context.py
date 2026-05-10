"""
Context object representing runtime state of a task execution within an envid.
Contains system prompt, tools, files, history, and other enrichment data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Context:
    """
    Runtime context for a task execution.
    Merged from saved envid context + task-specific overrides.
    
    Attributes:
        envid: Environment ID this context belongs to (optional, may be null)
        system_rendered: Rendered system prompt (openclaw-style with tools/files/meta)
        tooling: Tool configuration and available tools list
        safety: Safety instructions/rules
        skills: Available skills with metadata
        files: Files to include in context (with truncation rules)
        history: Conversation history (user/assistant/tool messages, excluding system)
        tools: Internal tools dict (key->tool_spec for context_render to process)
        rules: Additional context rules/guidelines (optional)
        meta: Metadata (datetime, environment info, etc.)
    """

    envid: Optional[str] = None
    system_rendered: str = ""
    tooling: Dict[str, Any] = field(default_factory=dict)
    safety: str = ""
    skills: Dict[str, Any] = field(default_factory=dict)
    files: List[Dict[str, Any]] = field(default_factory=list)
    history: List[Dict[str, Any]] = field(default_factory=list)
    tools: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    rules: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize context to dict for Redis storage."""
        return {
            "envid": self.envid,
            "system_rendered": self.system_rendered,
            "tooling": self.tooling,
            "safety": self.safety,
            "skills": self.skills,
            "files": self.files,
            "history": self.history,
            "tools": self.tools,
            "rules": self.rules,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Context:
        """Deserialize context from dict (Redis)."""
        return cls(
            envid=data.get("envid"),
            system_rendered=data.get("system_rendered", ""),
            tooling=data.get("tooling", {}),
            safety=data.get("safety", ""),
            skills=data.get("skills", {}),
            files=data.get("files", []),
            history=data.get("history", []),
            tools=data.get("tools", {}),
            rules=data.get("rules", ""),
            meta=data.get("meta", {}),
        )

    @classmethod
    def empty(cls) -> Context:
        """Create empty context."""
        return cls()
