"""
aiset-backed settings runtime.

One `aiset.service.Service` owns every configuration document of aidir:

| Document id | File | Descriptor |
|---|---|---|
| `main` | `config.json5` | `config.descriptor.json5` |
| `worker:<type>:<id>` | `workers/<type>/<id>/config.json5` | `workers/<type>/<id>/descriptor.json5` |

The runtime is the only place that knows about that table. Everything else reads
settings through the compatibility facade in `core/settings/facade.py`, which
keeps the dotted `config.get("a.b")` API the rest of the project already uses.

This is the only settings backend; the loader it replaced is gone. A document
that does not match its descriptor raises here, at import, rather than being
served empty.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from core.settings.aiset_path import ensure_aiset_importable

ensure_aiset_importable()

import logging  # noqa: E402

from aiset.resolver import Resolver  # noqa: E402
from aiset.service import PERMISSIONS, Principal, Service
from aiset.store import Settings as AisetStoreSettings

from core.settings.documents import (
    MAIN_DOCUMENT,
    discover_worker_documents,
    worker_document_id,
)

ROOT = Path(__file__).resolve().parent.parent.parent

TYPES_DESCRIPTOR = ROOT / "config.types.json5"
ROOT_DESCRIPTOR = ROOT / "config.descriptor.json5"
ROOT_CONFIG = ROOT / "config.json5"
ENV_FILE = ROOT / ".env"

LOGGER = logging.getLogger("aidir.settings")

# The host itself is not a browser session. It needs every permission because it
# is the component the browser session is authorized against.
CORE_PRINCIPAL = Principal(id="aidir-core", permissions=frozenset(PERMISSIONS))


class SettingsError(RuntimeError):
    """A configuration document could not be loaded as a whole."""


def read_dotenv(path: Path = ENV_FILE) -> dict[str, str]:
    """Parse a .env file into a mapping.

    Deliberately not `dotenv.load_dotenv`: the mapping is data for the resolver,
    and the process environment must keep its higher priority. Lines are
    `NAME=value`, `#` starts a comment, `export` is accepted, and surrounding
    single or double quotes are removed.
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        if not separator:
            continue
        name = name.strip()
        if not name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name] = value
    return values


def build_resolver(env_file: Path = ENV_FILE) -> Resolver:
    """Resolver with the one policy aiset defines for every host.

    host overrides, then the process environment, then .env, then the inline
    `:-` default, then the descriptor default. `.env` is injected as data, so a
    variable exported in the shell still wins over the file, which is exactly
    what `load_dotenv(override=False)` did before the migration.
    """
    return Resolver(overrides={}, dotenv=read_dotenv(env_file), environ=os.environ)


class SettingsRuntime:
    """Owns the aiset Service and the mapping from document id to file."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else ROOT
        self.service = Service(
            AisetStoreSettings(backup_depth=5, lock_timeout_seconds=10.0),
            audit=self._audit,
        )
        self._effective: dict[str, dict[str, Any]] = {}
        self._resolver = build_resolver()
        self._callbacks_bound = False

    # ── construction ────────────────────────────────────────────────

    @staticmethod
    def _audit(name: str, payload: dict[str, Any]) -> None:
        """Forward an aiset audit event to the aidir log."""
        from core import log

        try:
            log("audit", "info", f"settings {name}: {_summarize(payload)}", "settings")
        except Exception:  # pragma: no cover - logging must never break a write
            pass

    def build(self) -> "SettingsRuntime":
        """Register every document and load it once.

        Raises SettingsError when a document is invalid on disk. The startup
        rule is deliberate: a settings subsystem that quietly serves an empty
        configuration is worse than a refused start.
        """
        self._register_main()
        self._register_workers()
        self._bind_callbacks()
        self.refresh()
        return self

    def _register_main(self) -> None:
        self.service.register(
            MAIN_DOCUMENT,
            self.root / "config.json5",
            self._read_descriptor(self.root / "config.descriptor.json5"),
            typedef_sources=self._typedef_sources(),
            resolver=self.resolver,
        )

    def _register_workers(self) -> None:
        for document_id, config_path, descriptor_path in discover_worker_documents(self.root):
            self.service.register(
                document_id,
                config_path,
                self._read_descriptor(descriptor_path),
                typedef_sources=self._typedef_sources(),
                resolver=self.resolver,
            )

    def _read_descriptor(self, path: Path) -> str:
        """Read one descriptor file as text."""
        return path.read_text(encoding="utf-8")

    def _bind_callbacks(self) -> None:
        """Register the host callbacks exactly once.

        aiset keeps the callbacks in a list and invokes every entry, so a second
        registration would run the same callback twice for each write.
        """
        if self._callbacks_bound:
            return
        self.service.on_after_save(self._on_after_save)
        self.service.on_probe_changes(self._on_probe_changes)
        self.service.on_application(self._on_application)
        self._callbacks_bound = True

    def _typedef_sources(self) -> list[str]:
        return [self._read_descriptor(self.root / "config.types.json5")]

    # ── reads ───────────────────────────────────────────────────────

    @property
    def resolver(self) -> Resolver:
        """Resolver shared by every document of this runtime.

        Built once and handed to every `register` call, so the process
        environment and the .env mapping are read in one place and every
        document resolves identically.
        """
        return self._resolver

    def load(self, document_id: str):
        """Load one document and raise when it is invalid on disk."""
        result = self.service.load(document_id, principal=CORE_PRINCIPAL)
        if result.invalid_on_disk:
            raise SettingsError(f"{document_id}: {result.reason}")
        return result

    def refresh(self) -> None:
        """Reload every document from disk and rebuild the effective cache."""
        self._bind_callbacks()
        for document_id in self.service.documents:
            result = self.load(document_id)
            self._effective[document_id] = result.effective

    def effective(self, document_id: str = MAIN_DOCUMENT) -> dict[str, Any]:
        """Resolved values of one document; placeholders already substituted."""
        return self._effective.get(document_id, {})

    def document_ids(self) -> list[str]:
        """Every registered document id."""
        return sorted(self.service.documents)

    def worker_document(self, worker_type: str, worker_id: str) -> str | None:
        """Document id of a worker-local configuration, or None when it has none."""
        document_id = worker_document_id(worker_type, worker_id)
        return document_id if document_id in self.service.documents else None

    # ── callbacks ───────────────────────────────────────────────────

    def _on_after_save(self, previous: Any, current: Any, event: dict[str, Any]) -> None:
        """Refresh the effective cache after a committed write.

        aiset calls this with (previous stored tree, new stored tree, event).
        The cache must be refreshed here and not lazily, because a write made
        through the HTTP API has to be visible to the running process at once.
        """
        del previous, current  # only the event is needed
        document_id = str(event.get("document") or MAIN_DOCUMENT)
        if document_id not in self.service.documents:
            return
        try:
            self._effective[document_id] = self.load(document_id).effective
        except SettingsError as exc:
            LOGGER.warning("settings cache not refreshed document=%s reason=%s", document_id, exc)

    def _on_probe_changes(self, previous: Any, current: Any, changed_paths: list[str]) -> None:
        """Host veto hook. aidir validates through the descriptor, so no veto."""
        del previous, current, changed_paths

    def _on_application(
        self, previous: Any, current: Any, changed_paths: list[str]
    ) -> None:
        """Record how the running system picked the change up.

        aiset calls this with (previous effective, new effective, changed paths)
        and only for `live` and `reload` changes. The signature is checked when
        the callback is registered, so it cannot drift silently.
        """
        from core import log

        del previous, current  # only the paths are reported
        try:
            log(
                "system",
                "info",
                f"settings applied: paths={','.join(changed_paths) or '-'}",
                "settings",
            )
        except Exception:  # pragma: no cover - logging must never break a write
            pass


def _summarize(payload: dict[str, Any]) -> str:
    """One-line audit summary; values are never expanded."""
    return " ".join(f"{key}={payload[key]}" for key in sorted(payload))


_RUNTIME: SettingsRuntime | None = None


def get_runtime() -> SettingsRuntime:
    """Process-wide settings runtime, built on first use."""
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = SettingsRuntime().build()
    return _RUNTIME


def set_runtime(runtime: SettingsRuntime | None) -> None:
    """Install or clear the process-wide runtime (tests, shutdown)."""
    global _RUNTIME
    _RUNTIME = runtime