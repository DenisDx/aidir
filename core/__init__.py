"""Core package. Exposes log() shortcut for use by workers and other subsystems."""
from __future__ import annotations


def log(type_: str, level, message: str, tag: str | None = None) -> None:
    """Shortcut: write log entry via the global logger instance."""
    from core.logger import logger
    logger.log(type_, level, message, tag)
