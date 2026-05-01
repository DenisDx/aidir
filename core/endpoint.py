"""Base Endpoint class. Subclasses implement specific API protocols."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.app import Core


class BaseEndpoint(ABC):
    """Abstract base for all endpoints."""

    id: str = ""
    api: str = ""

    @abstractmethod
    def create_app(self, core: "Core") -> Any:
        """Create and return a FastAPI app for this endpoint."""

    @abstractmethod
    async def initialize(self, core: "Core") -> None:
        """Called once at startup."""
