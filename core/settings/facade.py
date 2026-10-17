"""Compatibility facade: the dotted configuration API on top of aiset.

The rest of aidir reads settings with `config.get("logging.level", "info")` and
receives the whole resolved dict with `config.raw()`. 158 call sites depend on
those two shapes, so they are preserved exactly. Everything else - the dotted
path, the JSON Pointer, the revision, the atomic write - is aiset's job.

Three deliberate differences from the library contract, all of them required by
the existing readers and recorded in plan_aiset_integration.md:

* `get()` returns `default` for an explicit `null`, not only for an absent key.
  `core/config.py` did the same, and readers depend on it.
* `raw()` returns the *effective* (resolved) dict, not the stored tree, because
  every reader expects `${VAR}` to have been substituted.
* the facade writes with the host principal. A browser session never reaches
  this object; the HTTP layer maps a session to its own principal.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.settings.aiset_path import ensure_aiset_importable

ensure_aiset_importable()

from aiset.service import Principal  # noqa: E402

from core.settings.documents import MAIN_DOCUMENT
from core.settings.runtime import CORE_PRINCIPAL, SettingsRuntime, get_runtime

FACADE_PRINCIPAL = CORE_PRINCIPAL


def dotted_to_pointer(key: str) -> str:
    """Convert `a.b.c` into the JSON Pointer `/a/b/c`.

    A key that is already a pointer is returned unchanged, so a caller may pass
    either spelling. The root is the empty string, never `/`.
    """
    if key == "":
        return ""
    if key.startswith("/"):
        return key
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in key.split("."))


def pointer_to_dotted(pointer: str) -> str:
    """Convert `/a/b` back into `a.b`; the root becomes the empty string."""
    if pointer in ("", "/"):
        return ""
    return ".".join(
        part.replace("~1", "/").replace("~0", "~") for part in pointer.lstrip("/").split("/")
    )


def value_to_text(value: Any) -> str:
    """Serialize a Python value as JSON5 source text for an operation.

    JSON is a subset of JSON5, so `json.dumps` output is valid input. Strings
    keep their quotes, which is what the operation contract expects.
    """
    return json.dumps(value, ensure_ascii=False)


class AisetConfig:
    """The `config` object of aidir, backed by an aiset document."""

    def __init__(self, runtime: SettingsRuntime | None = None, document_id: str = MAIN_DOCUMENT) -> None:
        self._runtime = runtime or get_runtime()
        self._document_id = document_id
        self._principal: Principal = FACADE_PRINCIPAL

    # ── reads ───────────────────────────────────────────────────────

    @property
    def document_id(self) -> str:
        """aiset document id behind this object."""
        return self._document_id

    @property
    def runtime(self) -> SettingsRuntime:
        """The process-wide settings runtime."""
        return self._runtime

    def load(self) -> None:
        """Re-read the document from disk and rebuild the effective cache."""
        self._runtime.refresh()

    def get(self, key: str, default: Any = None) -> Any:
        """Dot-path lookup: `get('logging.level')` returns the value or the default.

        An absent key and an explicit `null` both yield `default`, which is what
        `core/config.py` did and what the readers assume.
        """
        node: Any = self._runtime.effective(self._document_id)
        for part in key.split("."):
            if not isinstance(node, dict):
                return default
            node = node.get(part)
            if node is None:
                return default
        return node

    def __getitem__(self, key: str) -> Any:
        return self._runtime.effective(self._document_id)[key]

    def raw(self) -> dict[str, Any]:
        """Whole configuration with every placeholder already substituted."""
        return self._runtime.effective(self._document_id)

    def raw_text(self) -> str:
        """The document file exactly as it is stored on disk."""
        registration = self._runtime.service.documents[self._document_id]
        return Path(registration.store.path).read_text(encoding="utf-8")

    def revision(self) -> str:
        """Opaque hash of the stored bytes; send it with every write."""
        return self._runtime.load(self._document_id).revision

    # ── writes ──────────────────────────────────────────────────────

    def _apply(self, operations: list[dict[str, Any]]) -> None:
        """Run one atomic batch of operations against the document."""
        registration = self._runtime.service.documents[self._document_id]
        state = self._runtime.load(self._document_id)
        self._runtime.service.apply_operations(
            self._document_id,
            operations,
            expected_revision=state.revision,
            expected_descriptor_revision=registration.descriptor.revision,
            principal=self._principal,
        )

    def update_key(self, key: str, value: Any) -> None:
        """Set one dot-path key to a Python value."""
        self._apply([{"op": "set", "path": dotted_to_pointer(key), "value_text": value_to_text(value)}])

    def update_key_text(self, key: str, value_text: str) -> None:
        """Set one dot-path key from JSON5 source text, keeping `${VAR}` intact."""
        if not isinstance(value_text, str):
            raise ValueError("value_text must be a string")
        text = value_text.strip()
        if not text:
            raise ValueError("value_text must not be empty")
        self._apply([{"op": "set", "path": dotted_to_pointer(key), "value_text": text}])

    def delete_key(self, key: str) -> None:
        """Remove one dot-path key from the document."""
        self._apply([{"op": "remove", "path": dotted_to_pointer(key)}])

    def get_key_text(self, key: str) -> str:
        """Stored source text of one dot-path key."""
        text = self.get_key_text_or_none(key)
        if text is None:
            raise ValueError(f"Unknown config key path: {key}")
        return text

    def get_key_text_or_none(self, key: str) -> str | None:
        """Stored source text of one dot-path key, or None when it is absent."""
        result = self._runtime.service.get(
            self._document_id,
            dotted_to_pointer(key),
            view="source",
            principal=self._principal,
        )
        if not result.exists:
            return None
        return result.value_text

    # ── helpers used by the runtime ─────────────────────────────────

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<AisetConfig document={self._document_id!r}>"


__all__ = [
    "AisetConfig",
    "dotted_to_pointer",
    "pointer_to_dotted",
    "value_to_text",
]