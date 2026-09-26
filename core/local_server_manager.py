"""Lifecycle management for locally started llama.cpp servers."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shlex
import signal
import time

import httpx

from core import log


class LocalServerError(Exception):
    """An expected local-server startup or readiness error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LocalServerManager:
    """Start, await, and stop only llama.cpp processes launched by aidir."""

    def __init__(self, full_config: dict, root: str | Path) -> None:
        self._config = full_config if isinstance(full_config, dict) else {}
        self._root = Path(root)
        self._state_path = self._root / "logs" / "llama_cpp_servers.json"
        self._locks: dict[str, asyncio.Lock] = {}
        self._startup_errors: dict[str, dict[str, str]] = {}

    async def ensure_running(self, provider_id: str, model_id: str = "") -> None:
        """Ensure a llama.cpp provider is healthy, starting it only when configured locally."""
        try:
            await self._ensure_running(provider_id, model_id)
        except LocalServerError as exc:
            self._startup_errors[provider_id] = {
                "code": exc.code,
                "message": str(exc),
            }
            raise
        else:
            self._startup_errors.pop(provider_id, None)

    def startup_error(self, provider_id: str) -> dict[str, str] | None:
        """Return the latest failed startup result for a local provider."""
        error = self._startup_errors.get(provider_id)
        return dict(error) if error else None

    async def _ensure_running(self, provider_id: str, model_id: str = "") -> None:
        """Start and await a local llama.cpp provider without recording status."""
        provider = self._provider(provider_id)
        base_url = str(provider.get("baseUrl") or "").rstrip("/")
        if not base_url:
            raise LocalServerError("UPSTREAM_UNREACHABLE", f"llama.cpp provider '{provider_id}' has no baseUrl")

        if await self._is_healthy(base_url):
            return

        exec_cmd = str(provider.get("exec_cmd") or "").strip()
        if not exec_cmd:
            raise LocalServerError("UPSTREAM_UNREACHABLE", f"llama.cpp provider '{provider_id}' is not healthy")

        lock = self._locks.setdefault(provider_id, asyncio.Lock())
        async with lock:
            if await self._is_healthy(base_url):
                return

            owned = self._load_state().get(provider_id)
            if isinstance(owned, dict) and self._is_owned_process(owned):
                await self._wait_ready(provider_id, base_url, owned)
                return

            self._remove_state(provider_id)
            try:
                command = [os.path.expandvars(os.path.expanduser(part)) for part in shlex.split(exec_cmd)]
            except ValueError as exc:
                raise LocalServerError("INVALID_EXEC_CMD", f"Invalid exec_cmd for '{provider_id}': {exc}") from exc
            if not command:
                raise LocalServerError("INVALID_EXEC_CMD", f"llama.cpp provider '{provider_id}' has an empty exec_cmd")

            log_path = self._log_path(provider_id)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with log_path.open("ab", buffering=0) as output:
                    process = await asyncio.create_subprocess_exec(
                        *command,
                        stdout=output,
                        stderr=asyncio.subprocess.STDOUT,
                        start_new_session=True,
                    )
            except OSError as exc:
                raise LocalServerError("LLAMA_CPP_START_FAILED", f"Cannot start '{provider_id}': {exc}") from exc

            record = {
                "pid": process.pid,
                "start_ticks": self._process_start_ticks(process.pid),
                "model_id": str(model_id or "").strip(),
            }
            self._store_state(provider_id, record)
            try:
                await self._wait_ready(provider_id, base_url, record, process)
            except LocalServerError:
                await self.stop(provider_id)
                raise

    async def stop(self, provider_id: str) -> bool:
        """Terminate the persisted aidir-owned process for a provider; never touch unowned servers."""
        record = self._load_state().get(provider_id)
        if not isinstance(record, dict) or not self._is_owned_process(record):
            self._remove_state(provider_id)
            return False

        pid = int(record["pid"])
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            self._remove_state(provider_id)
            return False
        except OSError:
            os.kill(pid, signal.SIGTERM)

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not self._is_owned_process(record):
                self._remove_state(provider_id)
                return True
            await asyncio.sleep(0.1)

        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self._remove_state(provider_id)
        return True

    async def stop_all(self) -> list[str]:
        """Stop every persisted process that is still verifiably owned by aidir."""
        stopped: list[str] = []
        for provider_id in list(self._load_state()):
            if await self.stop(provider_id):
                stopped.append(provider_id)
        return stopped

    def owned_records(self) -> dict[str, dict]:
        """Return valid persisted records for aidir-owned running processes only."""
        return {
            provider_id: record
            for provider_id, record in self._load_state().items()
            if isinstance(record, dict) and self._is_owned_process(record)
        }

    def _provider(self, provider_id: str) -> dict:
        """Return one configured provider dictionary."""
        providers = ((self._config.get("models") or {}).get("providers") or {})
        provider = providers.get(provider_id) if isinstance(providers, dict) else None
        return provider if isinstance(provider, dict) else {}

    async def _wait_ready(self, provider_id: str, base_url: str, record: dict, process=None) -> None:
        """Wait for the owned server health endpoint or raise an actionable startup error."""
        provider = self._provider(provider_id)
        try:
            timeout = max(1, int(provider.get("startup_timeout", 120)))
        except (TypeError, ValueError):
            timeout = 120
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if process is not None and process.returncode is not None:
                raise LocalServerError("LLAMA_CPP_START_FAILED", f"llama.cpp provider '{provider_id}' exited with code {process.returncode}")
            if not self._is_owned_process(record):
                raise LocalServerError("LLAMA_CPP_START_FAILED", f"llama.cpp provider '{provider_id}' exited during startup")
            if await self._is_healthy(base_url):
                log("system", "info", f"llama.cpp provider '{provider_id}' is ready")
                return
            await asyncio.sleep(0.5)

        raise LocalServerError("UPSTREAM_UNREACHABLE", f"llama.cpp provider '{provider_id}' was not ready within {timeout}s")

    @staticmethod
    async def _is_healthy(base_url: str) -> bool:
        """Return whether a server responds successfully to llama.cpp's health endpoint."""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(f"{base_url}/health")
            return 200 <= response.status_code < 300
        except httpx.HTTPError:
            return False

    def _load_state(self) -> dict:
        """Read persisted owned-process records, returning an empty state on corruption."""
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _store_state(self, provider_id: str, record: dict) -> None:
        """Persist an owned process record atomically."""
        state = self._load_state()
        state[provider_id] = record
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._state_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        temp_path.replace(self._state_path)

    def _remove_state(self, provider_id: str) -> None:
        """Remove one provider process record from persistent state."""
        state = self._load_state()
        if provider_id not in state:
            return
        state.pop(provider_id, None)
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

    def _log_path(self, provider_id: str) -> Path:
        """Return the log path for output from one aidir-managed llama.cpp provider."""
        if provider_id == "llama_local":
            return self._root / "logs" / "local_llama_cpp.log"
        return self._root / "logs" / f"llama_cpp_{self._safe_id(provider_id)}.log"

    @staticmethod
    def _process_start_ticks(pid: int) -> str:
        """Return Linux process start ticks to guard against reused PIDs."""
        try:
            content = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            return content.rsplit(")", 1)[1].split()[19]
        except (OSError, IndexError):
            return ""

    @classmethod
    def _is_owned_process(cls, record: dict) -> bool:
        """Return whether the persisted PID still identifies the originally started process."""
        try:
            pid = int(record.get("pid"))
        except (TypeError, ValueError):
            return False
        expected_ticks = str(record.get("start_ticks") or "")
        actual_ticks = cls._process_start_ticks(pid)
        return bool(expected_ticks and actual_ticks and expected_ticks == actual_ticks)

    @staticmethod
    def _safe_id(value: str) -> str:
        """Normalize a provider id for use in a log filename."""
        return "".join(character if character.isalnum() or character in "-_" else "_" for character in value)