"""
Task_context_builder: pre-processing task for request context enrichment.
Created before agent task when request contains context_builder settings.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.task import Task


@dataclass
class Task_context_builder(Task):
    """
    Context-builder task that can mutate request payload before model execution.
    payload contains request_payload and context_builder config.
    """

    type: str = "context_builder"
