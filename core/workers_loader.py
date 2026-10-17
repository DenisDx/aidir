"""
Dynamic worker loader.
Scans ./workers/<type>/<id>/ directories, imports app.py from each enabled worker,
and returns a dict of worker_id -> BaseWorker instance.
Layout: workers/<type>/<worker_id>/app.py (e.g. workers/agent/openaix/app.py)
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

from core.worker import BaseWorker
from core import log

_ROOT = Path(__file__).parent.parent


def _read_worker_local_config(worker_dir: Path, worker_id: str) -> dict:
    """Resolved settings of a worker's own config.json5, or {} when it has none.

    The file is a settings document in its own right, with its own descriptor, so
    its values come from the settings runtime rather than from a second JSON5
    parse here. A worker without a local file keeps being configured from the
    root entry alone.
    """
    from core.settings.documents import worker_document_id
    from core.settings.runtime import get_runtime

    if not (worker_dir / "descriptor.json5").is_file():
        return {}

    document_id = worker_document_id(worker_dir.parent.name, worker_id)
    effective = get_runtime().effective(document_id)
    return dict(effective) if isinstance(effective, dict) else {}


def _iter_worker_dirs(workers_dir: Path):
    """Yield (worker_type, worker_id, worker_dir) for all workers in two-level layout."""
    if not workers_dir.exists():
        return
    for type_dir in sorted(workers_dir.iterdir()):
        if not type_dir.is_dir() or type_dir.name.startswith("_") or type_dir.name.startswith("."):
            continue
        worker_type = type_dir.name
        for worker_dir in sorted(type_dir.iterdir()):
            if not worker_dir.is_dir() or worker_dir.name.startswith("_") or worker_dir.name.startswith("."):
                continue
            yield worker_type, worker_dir.name, worker_dir


def resolve_worker_configs(config: dict) -> dict[str, dict]:
    """Build merged worker configs: local worker config overridden by root workers.items."""
    workers_cfg: dict[str, dict] = {}
    workers_dir = _ROOT / "workers"

    items_cfg: dict = config.get("workers", {}).get("items", {}) or {}

    for _worker_type, worker_id, worker_dir in _iter_worker_dirs(workers_dir):
        local_cfg = _read_worker_local_config(worker_dir, worker_id)
        root_cfg = items_cfg.get(worker_id, {})
        if not isinstance(root_cfg, dict):
            root_cfg = {}
        workers_cfg[worker_id] = _merge_worker_entry(local_cfg, root_cfg)

    return workers_cfg


def _merge_worker_entry(local_cfg: dict, root_cfg: dict) -> dict:
    """Combine a worker-local entry with its root entry; the root entry wins.

    `aiset.merge(prefer="base")` states that rule directly, so the library owns
    it and the host keeps no second implementation. test_settings_backend.py
    asserts the result is identical to the merge order this replaced.
    """
    from core.settings.documents import merge_worker_entry

    return merge_worker_entry(root_cfg, local_cfg)


def load_workers(config: dict, workers_cfg: dict[str, dict] | None = None) -> dict[str, BaseWorker]:
    """
    Load all enabled workers from ./workers/<type>/<id>/app.py.
    Each app.py must expose a module-level `worker` instance of BaseWorker.
    Main config section workers.items.<id> overrides worker's local config.json5.
    Returns dict of worker_id -> BaseWorker.
    """
    workers: dict[str, BaseWorker] = {}
    workers_dir = _ROOT / "workers"

    if not workers_dir.exists():
        log("system", "warn", "workers/ directory not found")
        return workers

    resolved_cfg = workers_cfg or resolve_worker_configs(config)

    # Ensure project root is importable
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

    for worker_type, worker_id, worker_dir in _iter_worker_dirs(workers_dir):
        # Check enabled flag (main config wins over local config.json5)
        cfg_override = resolved_cfg.get(worker_id, {})
        if not cfg_override.get("enabled", True):
            log("system", "info", f"Worker {worker_id} disabled, skipping")
            continue

        app_file = worker_dir / "app.py"
        if not app_file.exists():
            log("system", "warn", f"Worker {worker_id}: app.py not found, skipping")
            continue

        try:
            module = importlib.import_module(f"workers.{worker_type}.{worker_id}.app")
            instance: BaseWorker = module.worker  # required export
            instance.id = worker_id
            workers[worker_id] = instance
            log("system", "info", f"Worker {worker_id} loaded (type={instance.task_type})")
        except AttributeError:
            log("system", "error",
                f"Worker {worker_id}: app.py must expose a module-level `worker` object")
        except Exception as exc:
            log("system", "error", f"Worker {worker_id} load failed: {exc}")

    return workers
