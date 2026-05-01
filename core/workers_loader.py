"""
Dynamic worker loader.
Scans ./workers/ directory, imports app.py from each enabled worker,
and returns a dict of worker_id -> BaseWorker instance.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

from core.worker import BaseWorker
from core import log

_ROOT = Path(__file__).parent.parent


def load_workers(config: dict) -> dict[str, BaseWorker]:
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

    items_cfg: dict = config.get("workers", {}).get("items", {})

    # Ensure project root is importable
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

    for worker_dir in sorted(workers_dir.iterdir()):
        if not worker_dir.is_dir():
            continue
        worker_id = worker_dir.name

        # Check enabled flag (main config wins over local config.json)
        cfg_override = items_cfg.get(worker_id, {})
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
