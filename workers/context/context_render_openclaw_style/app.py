"""
context_render_openclaw_style worker.
Renders context.system_rendered using XML-style sections (Anthropic/openClaw format).
Combines tools descriptions, rules, files, and metadata into a formatted system prompt.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Awaitable, Callable

from core.context import Context
from core.task import Task
from core.worker import BaseWorker, WorkerResult


class ContextRenderOpenClawStyleWorker(BaseWorker):
    """
    Worker that renders context.system_rendered in XML-style format.
    
    Processes:
    - context.tools: renders as <available_tools> section
    - context.rules: renders as <rules> section
    - context.files: renders as <context_files> section
    - context.meta: renders as <meta> section
    - context.system_rendered: preserves or builds if empty
    
    Input: task.context must exist.
    Output: Updated task.context.system_rendered with full prompt.
    """

    id = "context_render_openclaw_style"
    task_type = "context"

    async def initialize(self, config: dict) -> None:
        """Initialize worker."""
        pass

    async def execute(
        self,
        task: Task,
        emit_chunk: Callable[[dict], Awaitable[None]] | None = None,
    ) -> WorkerResult:
        """Render context.system_rendered."""
        
        # Ensure context exists
        if task.context is None:
            task.context = Context.empty()
        
        sections = []
        
        # Preserve or start system_rendered
        if task.context.system_rendered:
            sections.append(task.context.system_rendered)
        
        # Tools section
        if task.context.tools:
            sections.append(_render_tools(task.context.tools))
        
        # Rules section
        if task.context.rules:
            sections.append(_render_section("rules", task.context.rules))
        
        # Files section
        if task.context.files:
            sections.append(_render_files(task.context.files))
        
        # Meta section
        if task.context.meta:
            sections.append(_render_meta(task.context.meta))
        
        # Combine all sections
        task.context.system_rendered = "\n\n".join(
            s for s in sections if s
        )
        
        return WorkerResult(
            ok=True,
            data={"rendered_length": len(task.context.system_rendered)},
        )


# ── Rendering helpers ─────────────────────────────────────────────────────────

def _render_section(tag: str, content: str) -> str:
    """Wrap content in XML-style tags."""
    if not content or not content.strip():
        return ""
    return f"<{tag}>\n{content}\n</{tag}>"


def _render_tools(tools: dict) -> str:
    """
    Render available tools section.
    tools: Dict[tool_name -> tool_spec]
    """
    if not tools:
        return ""
    
    lines = ["Available tools:"]
    for tool_name, tool_spec in sorted(tools.items()):
        if isinstance(tool_spec, dict):
            desc = tool_spec.get("description", tool_name)
            lines.append(f"  - {tool_name}: {desc}")
        else:
            lines.append(f"  - {tool_name}")
    
    # Add instruction for tool-call workflow
    """
    lines.append("\n")
    lines.append("IMPORTANT:")
    lines.append("1. You may call any of the available tools by using tool_calls.")
    lines.append("2. After tool results are returned, analyze them carefully.")
    lines.append("3. Provide a clear, direct answer to the user based on tool results.")
    lines.append("4. Do NOT ask for more input if you already have tool results - provide your answer.")
    """
    
    return _render_section("available_tools", "\n".join(lines))


def _render_files(files: list) -> str:
    """
    Render files section (list of file dicts with name/location/content).
    """
    if not files:
        return ""
    
    lines = ["Context files:"]
    for file_item in files:
        if isinstance(file_item, dict):
            name = file_item.get("name", "unknown")
            content = file_item.get("content", "")
            lines.append(f"\n### {name}")
            if content:
                lines.append(content[:500])  # Truncate for safety
        else:
            lines.append(str(file_item))
    
    return _render_section("context_files", "\n".join(lines))


def _render_meta(meta: dict) -> str:
    """
    Render metadata section (datetime, env info, etc.).
    """
    if not meta:
        meta = {}
    
    lines = []
    
    # Add current datetime
    now = datetime.now(timezone.utc).isoformat()
    lines.append(f"Timestamp: {now}")
    
    # Add provided meta fields
    for key, value in sorted(meta.items()):
        lines.append(f"{key}: {value}")
    
    return _render_section("meta", "\n".join(lines))


# Export worker instance
worker = ContextRenderOpenClawStyleWorker()
