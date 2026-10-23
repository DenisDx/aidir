"""
Logging subsystem.
core.log(type, level, message[, tag]) writes to logs/{type}.log and logs/all.log
when the message level meets the configured threshold.

Level scale: 0=EMERG … 7=DEBUG (systemd-journald compatible).
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).parent.parent


def logs_dir() -> Path:
    """Return the directory the log files are written to.

    `AIDIR_LOG_DIR` moves it. The test suite uses it so that a test run does
    not write into the logs of the running instance: the doubles' messages
    would otherwise be indistinguishable from a real failure for whoever reads
    the log afterwards.
    """
    override = str(os.environ.get("AIDIR_LOG_DIR") or "").strip()
    if override:
        return Path(override)
    return _ROOT / "logs"

# The log types the code writes. A threshold for any other name never matches,
# and the mistake is invisible: the file simply keeps the global level.
KNOWN_SUBSYSTEMS = frozenset({"system", "core", "worker", "webui", "audit", "http", "hooks"})

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


def _format(type_: str, level: int, message: str, tag: str | None, tzinfo) -> str:
    ts = datetime.now(tzinfo).isoformat(timespec="milliseconds")
    lvl = _INT_TO_NAME.get(level, str(level))
    tag_part = f":{tag}" if tag else ""
    return f"{ts} [{lvl}] [{type_}{tag_part}] {message}\n"


def _append(path: Path, line: str) -> None:
    """Append one line and close the file explicitly.

    Writing through a temporary - `path.open("a").write(line)` - leaves the close
    to the garbage collector. CPython's refcounting does close it, so there is no
    descriptor leak, but the file is finalised rather than closed, which raises a
    ResourceWarning on every line and hides the ones that matter. It also costs
    more: the explicit block measured about 40% faster than the implicit close.
    """
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)


class CoreLogger:
    """
    Thread-safe (file-append) logger. Writes to per-type and all.log.
    Thresholds resolved lazily from global config.
    """

    def __init__(self) -> None:
        logs_dir().mkdir(parents=True, exist_ok=True)
        self._cfg = None

    def _config(self):
        if self._cfg is None:
            from core.config import config
            self._cfg = config
        return self._cfg

    def _timezone(self):
        """Resolve timezone from logging.timezone config (local, UTC, or IANA name)."""
        cfg = self._config()
        raw = cfg.get("logging.timezone", "local")
        name = str(raw or "local").strip()

        if not name:
            return datetime.now().astimezone().tzinfo or timezone.utc

        lowered = name.lower()
        if lowered in ("local", "system"):
            return datetime.now().astimezone().tzinfo or timezone.utc
        if lowered in ("utc", "gmt", "z"):
            return timezone.utc

        try:
            return ZoneInfo(name)
        except Exception:
            return timezone.utc

    def _endpoint_cfg_by_id(self, endpoint_id: str) -> dict | None:
        """Return endpoint config dict by id from root config.endpoints list."""
        cfg = self._config()
        endpoints = cfg.get("endpoints") or []
        if not isinstance(endpoints, list):
            return None
        for ep in endpoints:
            if isinstance(ep, dict) and str(ep.get("id")) == endpoint_id:
                return ep
        return None

    def _endpoint_logging_override(self, endpoint_id: str, *, individual: bool) -> int | None:
        """Resolve per-endpoint logging override from endpoint.logging section."""
        ep_cfg = self._endpoint_cfg_by_id(endpoint_id)
        if not isinstance(ep_cfg, dict):
            return None
        logging_cfg = ep_cfg.get("logging")
        if not isinstance(logging_cfg, dict):
            return None

        key = "level" if individual else "alllevel"
        value = logging_cfg.get(key)
        if value is None:
            return None
        return _level_int(value)

    def _threshold(self, type_: str, tag: str | None, *, individual: bool) -> int:
        """Resolve effective log threshold for subsystem, worker, middleware, or endpoint."""
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
            # Endpoint-level override (for logs emitted as type=http with endpoint id tag)
            if type_ in ("http", "endpoint") and tag:
                v = self._endpoint_logging_override(tag, individual=True)
                if v is not None:
                    return v
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
            # all.log: endpoint override
            if type_ in ("http", "endpoint") and tag:
                v = self._endpoint_logging_override(tag, individual=False)
                if v is not None:
                    return v
            # all.log per-subsystem
            v = cfg.get(f"logging.alllevels.{type_}")
            if v is not None:
                return _level_int(v)

        # Global fallback
        return _level_int(cfg.get("logging.level", "info"))

    def log(self, type_: str, level, message: str, tag: str | None = None) -> None:
        """Write message to individual type log and all.log if threshold permits."""
        lvl_int = _level_int(level)
        line = _format(type_, lvl_int, message, tag, self._timezone())

        target = logs_dir()

        # Individual type log (e.g. logs/worker.log)
        if lvl_int <= self._threshold(type_, tag, individual=True):
            _append(target / f"{type_}.log", line)

        # Shared all.log
        if lvl_int <= self._threshold(type_, tag, individual=False):
            _append(target / "all.log", line)

    def warn_unknown_subsystems(self) -> list[str]:
        """Report the log thresholds whose key is not a subsystem the code writes.

        A key that does not name a log type never matches anything, so the
        threshold is silently ignored. The mistake is invisible otherwise.
        """
        cfg = self._config()
        unknown: list[str] = []
        for section in ("levels", "alllevels"):
            values = cfg.get(f"logging.{section}", {}) or {}
            if not isinstance(values, dict):
                continue
            for name in values:
                if str(name).strip() not in KNOWN_SUBSYSTEMS:
                    unknown.append(f"logging.{section}.{name}")
        return sorted(unknown)

    def wipe_logs(self, max_age_seconds: int) -> None:
        """Remove log lines older than max_age_seconds from *.log and *.jsonl files."""
        cutoff = time.time() - max_age_seconds
        target = logs_dir()
        for pattern in ("*.log", "*.jsonl"):
            for log_file in target.glob(pattern):
                _wipe_file(log_file, cutoff)


def _wipe_file(path: Path, cutoff: float) -> None:
    """Keep only lines whose leading timestamp is >= cutoff."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        kept = []
        is_jsonl = path.suffix.lower() == ".jsonl"

        for line in lines:
            try:
                ts_str = _extract_timestamp_from_line(line, is_jsonl=is_jsonl)
                if not ts_str:
                    kept.append(line)
                    continue
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                if ts >= cutoff:
                    kept.append(line)
            except Exception:
                kept.append(line)  # keep lines we can't parse
        path.write_text("".join(kept), encoding="utf-8")
    except Exception:
        pass


def _extract_timestamp_from_line(line: str, *, is_jsonl: bool) -> str | None:
    """Extract ISO timestamp from logger or JSONL line."""
    if not is_jsonl:
        return line.split(" ")[0]

    raw = line.strip()
    if not raw:
        return None

    payload = json.loads(raw)
    if not isinstance(payload, dict):
        return None

    ts = payload.get("ts")
    if not isinstance(ts, str) or not ts.strip():
        return None
    return ts.strip()


# Global singleton
logger = CoreLogger()
