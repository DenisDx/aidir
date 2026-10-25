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
        self._startup_failed_at: dict[str, float] = {}

    async def ensure_running(self, provider_id: str, model_id: str = "") -> None:
        """Ensure a llama.cpp provider is healthy, starting it only when configured locally."""
        try:
            await self._ensure_running(provider_id, model_id)
        except LocalServerError as exc:
            self._startup_errors[provider_id] = {
                "code": exc.code,
                "message": str(exc),
            }
            self._startup_failed_at[provider_id] = time.monotonic()
            raise
        else:
            self._startup_errors.pop(provider_id, None)
            self._startup_failed_at.pop(provider_id, None)

    def startup_error(self, provider_id: str) -> dict[str, str] | None:
        """Return the latest failed startup result for a local provider."""
        error = self._startup_errors.get(provider_id)
        return dict(error) if error else None

    def startup_errors(self) -> dict[str, dict[str, str]]:
        """Return the failed startup result of every local provider."""
        return {provider_id: dict(error) for provider_id, error in self._startup_errors.items()}

    def startup_blocked(self, provider_id: str, cooldown: float = 60.0) -> bool:
        """Return whether a recent startup failure should keep routing away from a provider."""
        failed_at = self._startup_failed_at.get(provider_id)
        if failed_at is None or provider_id not in self._startup_errors:
            return False
        return time.monotonic() - failed_at < max(0.0, cooldown)

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
                output = log_path.open("ab", buffering=0)
            except OSError as exc:
                raise LocalServerError(
                    "LLAMA_CPP_START_FAILED",
                    f"Cannot open the log file for '{provider_id}': {exc}",
                ) from exc
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=self._child_env(provider),
                    start_new_session=True,
                )
            except OSError as exc:
                output.close()
                raise LocalServerError("LLAMA_CPP_START_FAILED", f"Cannot start '{provider_id}': {exc}") from exc

            # The child's output is copied into its log file and into the aidir log.
            # A pipe is used instead of a file descriptor so that a startup
            # failure is visible while it happens: with a plain file the child's
            # stdout is block-buffered and its content is lost when it is killed.
            pump = asyncio.create_task(
                self._pump_child_output(provider_id, process, output),
                name=f"llama-cpp-output:{provider_id}",
            )

            record = {
                "pid": process.pid,
                "start_ticks": self._process_start_ticks(process.pid),
                "model_id": str(model_id or "").strip(),
            }
            self._store_state(provider_id, record)
            try:
                await self._wait_ready(provider_id, base_url, record, process)
            except LocalServerError:
                # Collect the evidence before the process is killed: its buffered
                # output disappears with it.
                await self._collect_startup_evidence(provider_id, process, pump, log_path)
                await self.stop(provider_id)
                await self._finish_output_pump(pump, output)
                raise
            await self._collect_startup_evidence(provider_id, process, pump, log_path, only_if_finished=True)

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

    async def adopt_existing(self) -> tuple[list[str], list[str]]:
        """Keep healthy inherited servers and stop only unhealthy or unconfigured ones."""
        kept: list[str] = []
        stopped: list[str] = []
        for provider_id in list(self.owned_records()):
            provider = self._provider(provider_id)
            base_url = str(provider.get("baseUrl") or "").rstrip("/")
            if base_url and await self._is_healthy(base_url):
                kept.append(provider_id)
                continue
            if await self.stop(provider_id):
                stopped.append(provider_id)
        return kept, stopped

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

    def _child_env(self, provider: dict) -> dict[str, str]:
        """Return the environment for a managed server process.

        The process environment is the base; it already carries the `.env`
        values, because the settings runtime publishes them into `os.environ`.
        A provider may add or override entries through its own `env` map, so a
        managed server can be given `CUDA_VISIBLE_DEVICES`, `LD_LIBRARY_PATH` or
        a fork-specific knob explicitly instead of relying on the shell the
        service happens to inherit.
        """
        env = dict(os.environ)
        declared = provider.get("env")
        if isinstance(declared, dict):
            for name, value in declared.items():
                key = str(name or "").strip()
                if key:
                    env[key] = str(value)
        return env

    async def _pump_child_output(self, provider_id: str, process, output) -> None:
        """Copy the managed server output into its log file and the aidir log."""
        try:
            stream = process.stdout
            if stream is None:
                return
            while True:
                raw = await stream.readline()
                if not raw:
                    break
                try:
                    output.write(raw)
                except (OSError, ValueError):
                    pass
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line:
                    log("system", "info", f"llama.cpp provider '{provider_id}': {line}", "llama_cpp")
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            try:
                output.close()
            except (OSError, ValueError):
                pass

    async def _collect_startup_evidence(
        self,
        provider_id: str,
        process,
        pump,
        log_path: Path,
        *,
        only_if_finished: bool = False,
    ) -> None:
        """Log the child's exit status and its last output lines.

        Called before the process is killed, because a killed process loses its
        buffered output. `only_if_finished` keeps a healthy server silent.
        """
        if only_if_finished and process.returncode is None:
            return
        tail = self._log_tail(log_path)
        status = f"exit code {process.returncode}" if process.returncode is not None else "still running"
        if tail:
            log(
                "system",
                "error",
                f"llama.cpp provider '{provider_id}' startup output ({status}):\n{tail}",
                "llama_cpp",
            )
        else:
            log(
                "system",
                "error",
                f"llama.cpp provider '{provider_id}' produced no output ({status})",
                "llama_cpp",
            )
        if pump is not None and not pump.done():
            pump.cancel()

    @staticmethod
    async def _finish_output_pump(pump, output=None, timeout: float = 2.0) -> None:
        """Stop the output reader and make sure the log file is closed."""
        if pump is None:
            if output is not None:
                try:
                    output.close()
                except (OSError, ValueError):
                    pass
            return
        done, pending = await asyncio.wait({pump}, timeout=timeout)
        for task in pending:
            task.cancel()
        if output is not None:
            # A pump cancelled before its first step never runs its own cleanup,
            # so the handle is closed here as well. Closing twice is harmless.
            try:
                output.close()
            except (OSError, ValueError):
                pass

    @staticmethod
    def _log_tail(log_path: Path, limit: int = 40) -> str:
        """Return the last lines of a managed server log file."""
        try:
            content = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        lines = [line for line in content.splitlines() if line.strip()]
        return "\n".join(lines[-limit:])

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

        raise LocalServerError(
            "UPSTREAM_UNREACHABLE",
            f"llama.cpp provider '{provider_id}' was not ready within {timeout}s "
            f"(see {self._log_path(provider_id)})",
        )

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