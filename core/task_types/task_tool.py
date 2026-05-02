"""
Task_tool: tool invocation task.
Created by MCP endpoint for tools/call requests.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.task import Task


@dataclass
class Task_tool(Task):
    """
    Tool task for built-in or external utility execution.
    payload stores tool_id and arguments from MCP call.
    """

    type: str = "tool"
