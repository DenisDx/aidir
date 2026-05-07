"""
Dynamic worker loader.
Scans ./workers/ directory, imports app.py from each enabled worker,
and returns a dict of worker_id -> BaseWorker instance.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import json5

from core.config import _substitute
from core.worker import BaseWorker
from core import log

_ROOT = Path(__file__).parent.parent


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge dictionaries with override priority."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_worker_local_config(worker_dir: Path, worker_id: str) -> dict:
    """Read workers/<id>/config.json as JSON5 (with env placeholders)."""
    cfg_file = worker_dir / "config.json"
    if not cfg_file.exists():
        return {}

    try:
        raw = cfg_file.read_text(encoding="utf-8")
        parsed = json5.loads(_substitute(raw))
        return parsed if isinstance(parsed, dict) else {}
    except Exception as exc:
        log("system", "warn", f"Worker {worker_id}: invalid config.json ignored: {exc}")
        return {}


def resolve_worker_configs(config: dict) -> dict[str, dict]:
    """Build merged worker configs: local worker config overridden by root workers.items."""
    workers_cfg: dict[str, dict] = {}
    workers_dir = _ROOT / "workers"
    if not workers_dir.exists():
        return workers_cfg

    items_cfg: dict = config.get("workers", {}).get("items", {}) or {}

    for worker_dir in sorted(workers_dir.iterdir()):
        if not worker_dir.is_dir():
            continue
        worker_id = worker_dir.name
        local_cfg = _read_worker_local_config(worker_dir, worker_id)
        root_cfg = items_cfg.get(worker_id, {})
        if not isinstance(root_cfg, dict):
            root_cfg = {}
        workers_cfg[worker_id] = _deep_merge(local_cfg, root_cfg)

    return workers_cfg


def load_workers(config: dict, workers_cfg: dict[str, dict] | None = None) -> dict[str, BaseWorker]:
    """
    Load all enabled workers from ./workers/<id>/app.py.
    Each app.py must expose a module-level `worker` instance of BaseWorker.
    Main config section workers.items.<id> overrides worker's local config.json.
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

    for worker_dir in sorted(workers_dir.iterdir()):
        if not worker_dir.is_dir():
            continue
        worker_id = worker_dir.name

        # Check enabled flag (main config wins over local config.json)
        cfg_override = resolved_cfg.get(worker_id, {})
        if not cfg_override.get("enabled", True):
            log("system", "info", f"Worker {worker_id} disabled, skipping")
            continue

        app_file = worker_dir / "app.py"
        if not app_file.exists():
            log("system", "warn", f"Worker {worker_id}: app.py not found, skipping")
            continue

        try:
            module = importlib.import_module(f"workers.{worker_id}.app")
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
