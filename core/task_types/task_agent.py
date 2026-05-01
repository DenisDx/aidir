"""
Task_agent: inference / AI-agent task.
Created by Endpoint_ollama for /api/chat requests.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.task import Task


@dataclass
class Task_agent(Task):
    """
    Agent task – requests inference from an AI model.
    payload contains the raw Ollama /api/chat request body.
    stream=True triggers streaming response mode.
    """

    type: str = "agent"
    stream: bool = False
