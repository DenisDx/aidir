"""
Configuration management.
Loads config.json (JSON5) with ${VAR} / ${VAR:-default} substitution from .env.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import json5
from dotenv import load_dotenv

_ROOT = Path(__file__).parent.parent
load_dotenv(_ROOT / ".env")

_ENV_RE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}')


def _substitute(text: str) -> str:
    """Replace ${VAR} and ${VAR:-default} with env values."""
    def _replace(m: re.Match) -> str:
        var, default = m.group(1), m.group(2) if m.group(2) is not None else ""
        return os.environ.get(var, default)
    return _ENV_RE.sub(_replace, text)


class Config:
    """
    Loads and caches config.json; supports dot-path access and hot reload.
    Substitutes ${ENV_VAR} tokens from environment / .env before parsing.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (_ROOT / "config.json")
        self._data: dict = {}
        self.load()

    def load(self) -> None:
        """Load (or reload) config from disk."""
        raw = self._path.read_text(encoding="utf-8")
        raw = _substitute(raw)
        self._data = json5.loads(raw)

    def get(self, key: str, default=None):
        """Dot-path lookup: config.get('logging.level') → value or default."""
        node = self._data
        for part in key.split("."):
            if not isinstance(node, dict):
                return default
            node = node.get(part)
            if node is None:
                return default
        return node

    def __getitem__(self, key: str):
        return self._data[key]

    def raw(self) -> dict:
        """Return the full config dict."""
        return self._data


# Global singleton – imported by other modules
config = Config()
