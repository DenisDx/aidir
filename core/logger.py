"""
Logging subsystem.
core.log(type, level, message[, tag]) writes to logs/{type}.log and logs/all.log
when the message level meets the configured threshold.

Level scale: 0=EMERG … 7=DEBUG (systemd-journald compatible).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_LOGS_DIR = _ROOT / "logs"

# Numeric ↔ name mappings (systemd-journald scale)
_INT_TO_NAME: dict[int, str] = {
    0: "EMERG", 1: "ALERT", 2: "CRIT", 3: "ERROR",
    4: "WARN",  5: "NOTICE", 6: "INFO", 7: "DEBUG",
}
_NAME_TO_INT: dict[str, int] = {
    **{name: num for num, name in _INT_TO_NAME.items()},
    "WARNING": 4, "INFORMATION": 6, "TRACE": 7,
}


def _level_int(level) -> int:
    """Convert string or int level to int."""
    if isinstance(level, int):
        return level
    return _NAME_TO_INT.get(str(level).upper(), 6)


def _format(type_: str, level: int, message: str, tag: str | None) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lvl = _INT_TO_NAME.get(level, str(level))
    tag_part = f":{tag}" if tag else ""
    return f"{ts} [{lvl}] [{type_}{tag_part}] {message}\n"


class CoreLogger:
    """
    Thread-safe (file-append) logger. Writes to per-type and all.log.
    Thresholds resolved lazily from global config.
    """

    def __init__(self) -> None:
        _LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self._cfg = None

    def _config(self):
        if self._cfg is None:
            from core.config import config
            self._cfg = config
        return self._cfg

    def _threshold(self, type_: str, tag: str | None, *, individual: bool) -> int:
        """Resolve effective log threshold for a subsystem / worker / middleware."""
        cfg = self._config()

        if individual:
            # Worker-level override
            if type_ == "worker" and tag:
                v = cfg.get(f"workers.items.{tag}.logging.level")
                if v is not None:
                    return _level_int(v)
            # Middleware-level override
            if type_ == "middleware" and tag:
                v = cfg.get(f"middleware.items.{tag}.logging.level")
                if v is not None:
                    return _level_int(v)
            # Per-subsystem default
            v = cfg.get(f"logging.levels.{type_}")
            if v is not None:
                return _level_int(v)
        else:
            # all.log: worker override
            if type_ == "worker" and tag:
                v = cfg.get(f"workers.items.{tag}.logging.alllevel")
                if v is not None:
                    return _level_int(v)
            # all.log per-subsystem
            v = cfg.get(f"logging.alllevels.{type_}")
            if v is not None:
                return _level_int(v)

        # Global fallback
        return _level_int(cfg.get("logging.level", "info"))

    def log(self, type_: str, level, message: str, tag: str | None = None) -> None:
        """Write message to individual type log and all.log if threshold permits."""
        lvl_int = _level_int(level)
        line = _format(type_, lvl_int, message, tag)

        # Individual type log (e.g. logs/worker.log)
        if lvl_int <= self._threshold(type_, tag, individual=True):
            (_LOGS_DIR / f"{type_}.log").open("a", encoding="utf-8").write(line)

        # Shared all.log
        if lvl_int <= self._threshold(type_, tag, individual=False):
            (_LOGS_DIR / "all.log").open("a", encoding="utf-8").write(line)

    def wipe_logs(self, max_age_seconds: int) -> None:
        """Remove log lines older than max_age_seconds from all *.log files."""
        cutoff = time.time() - max_age_seconds
        for log_file in _LOGS_DIR.glob("*.log"):
            _wipe_file(log_file, cutoff)


def _wipe_file(path: Path, cutoff: float) -> None:
    """Keep only lines whose leading timestamp is >= cutoff."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        kept = []
        for line in lines:
            try:
                ts_str = line.split(" ")[0]
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                if ts >= cutoff:
                    kept.append(line)
            except Exception:
                kept.append(line)  # keep lines we can't parse
        path.write_text("".join(kept), encoding="utf-8")
    except Exception:
        pass


# Global singleton
logger = CoreLogger()
