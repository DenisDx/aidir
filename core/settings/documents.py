"""Discovery of the worker-local configuration documents.

A worker may keep its own `config.json5` next to its `app.py`. That file and the
root entry `workers.items.<id>` describe the same worker, and the root entry
wins on every key present in both. This module finds those files, gives them a
document id, and performs the merge the way `core/workers_loader.py` did before
the migration.

Document id: `worker:<type>:<id>`, for example `worker:tool:web_search`. It is
the only handle a client ever passes; the file path never crosses the API.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from core.settings.aiset_path import ensure_aiset_importable

ensure_aiset_importable()

MAIN_DOCUMENT = "main"
WORKER_PREFIX = "worker"


def worker_document_id(worker_type: str, worker_id: str) -> str:
    """Stable document id of one worker-local configuration file."""
    return f"{WORKER_PREFIX}:{worker_type}:{worker_id}"


def worker_config_path(worker_dir: Path) -> Path:
    """Configuration file of one worker directory."""
    return worker_dir / "config.json5"


def worker_descriptor_path(worker_dir: Path) -> Path:
    """Descriptor of one worker configuration file."""
    return worker_dir / "descriptor.json5"


def iter_worker_dirs(workers_dir: Path) -> Iterator[tuple[str, str, Path]]:
    """Yield (worker_type, worker_id, worker_dir) for the two-level layout."""
    if not workers_dir.is_dir():
        return
    for type_dir in sorted(workers_dir.iterdir()):
        if not type_dir.is_dir() or type_dir.name.startswith(("_", ".")):
            continue
        for worker_dir in sorted(type_dir.iterdir()):
            if not worker_dir.is_dir() or worker_dir.name.startswith(("_", ".")):
                continue
            yield type_dir.name, worker_dir.name, worker_dir


def discover_worker_documents(root: Path) -> list[tuple[str, Path, Path]]:
    """Return (document_id, config_path, descriptor_path) for every worker file.

    A worker directory is only a document when it carries both the
    configuration file and its descriptor. A worker without them keeps the
    previous behaviour of being configured from the root entry alone.
    """
    documents: list[tuple[str, Path, Path]] = []
    for worker_type, worker_id, worker_dir in iter_worker_dirs(root / "workers"):
        config_path = worker_config_path(worker_dir)
        descriptor_path = worker_descriptor_path(worker_dir)
        if config_path.is_file() and descriptor_path.is_file():
            documents.append((worker_document_id(worker_type, worker_id), config_path, descriptor_path))
    return documents


def parse_worker_document_id(document_id: str) -> tuple[str, str] | None:
    """Split a worker document id back into (worker_type, worker_id)."""
    parts = document_id.split(":")
    if len(parts) == 3 and parts[0] == WORKER_PREFIX:
        return parts[1], parts[2]
    return None


def merge_worker_entry(root_entry: dict[str, Any], local_entry: dict[str, Any]) -> dict[str, Any]:
    """Merge one worker entry: the root entry wins, the local entry fills the gaps.

    `aiset.merge(prefer="base")` already expresses exactly this rule, so it is
    used instead of a private deep merge: the behaviour must not drift away from
    what the library documents.
    """
    from aiset.merge import merge

    return merge(root_entry or {}, local_entry or {}, prefer="base")