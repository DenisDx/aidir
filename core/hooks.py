"""Hook discovery, registration, and lifecycle actions."""
from __future__ import annotations

import ast
import importlib.util
import inspect
from pathlib import Path
from types import ModuleType
from typing import Any, Awaitable, Callable

from core import log
from core.local_server_manager import LocalServerManager
from core.task import Task

HookHandler = Callable[..., Awaitable[None]]
_EVENTS = {
    "task_created",
    "before_llm_request",
    "llm_response_complete",
    "before_consumer_delivery",
}


class HookManager:
    """Load enabled hooks and execute their lifecycle handlers."""

    def __init__(self, root: str | Path, local_server_manager: LocalServerManager, full_config: dict) -> None:
        self._hooks_dir = Path(root) / "hooks"
        self._local_server_manager = local_server_manager
        self._full_config = full_config
        self._handlers: dict[str, list[tuple[str, HookHandler]]] = {event: [] for event in _EVENTS}

    async def load(self) -> None:
        """Discover and register enabled hook modules."""
        if not self._hooks_dir.is_dir():
            return

        for hook_id, entry_path in self._entries():
            enabled = self._is_enabled(hook_id, entry_path)
            if not enabled:
                log("hooks", "info", f"Hook disabled: {hook_id}")
                continue
            module = self._import_module(hook_id, entry_path)
            if module is None:
                continue
            register = getattr(module, "register", None)
            if not callable(register):
                log("hooks", "error", f"Hook {hook_id} has no register(hooks) function")
                continue
            try:
                register(self)
            except Exception as exc:
                log("hooks", "error", f"Hook {hook_id} registration failed: {exc}")

    def on(self, event: str, handler: HookHandler) -> None:
        """Register one async handler for a supported lifecycle event."""
        if event not in _EVENTS:
            raise ValueError(f"Unsupported hook event: {event}")
        if not inspect.iscoroutinefunction(handler):
            raise TypeError(f"Hook handler for {event} must be async")
        module_name = getattr(handler, "__module__", "<unknown>")
        hook_id = module_name.removeprefix("_aidir_hook_")
        self._handlers[event].append((hook_id, handler))

    async def emit(self, event: str, *args: Any) -> None:
        """Run handlers for one lifecycle event without failing the task."""
        for hook_id, handler in self._handlers.get(event, ()):
            try:
                await handler(*args)
            except Exception as exc:
                log("hooks", "error", f"Hook {hook_id} failed during {event}: {exc}")

    async def restart_local_llama_cpp(self, provider_id: str) -> None:
        """Restart one aidir-owned local llama.cpp provider."""
        provider = self._provider(provider_id)
        if provider.get("api") != "llama-cpp":
            raise ValueError(f"Provider {provider_id} is not llama-cpp")
        if provider_id not in self._local_server_manager.owned_records():
            raise RuntimeError(f"llama.cpp provider {provider_id} is not owned by aidir")
        await self._local_server_manager.stop(provider_id)
        await self._local_server_manager.ensure_running(provider_id)

    def is_owned_local_llama_cpp(self, provider_id: str) -> bool:
        """Return whether a provider is an aidir-owned local llama.cpp server."""
        return (
            self._provider(provider_id).get("api") == "llama-cpp"
            and provider_id in self._local_server_manager.owned_records()
        )

    async def retry_task(self, task: Task, provider_id: str | None = None) -> None:
        """Request one scheduler-managed retry of a task."""
        metadata = task.hook_metadata
        if metadata.get("_hook_retry_requested"):
            raise RuntimeError(f"Task {task.id} already has a pending hook retry")
        metadata["_hook_retry_requested"] = True
        metadata["_hook_retry_provider_id"] = provider_id or ""

    async def cancel_task(self, task: Task, reason: str) -> None:
        """Request cancellation of a task at the current lifecycle boundary."""
        if not str(reason).strip():
            raise ValueError("Hook cancellation reason is required")
        task.hook_metadata["_hook_cancel_reason"] = str(reason)

    def consume_retry_request(self, task: Task) -> tuple[bool, str | None]:
        """Return and clear a hook retry request for scheduler processing."""
        metadata = task.hook_metadata
        if not metadata.pop("_hook_retry_requested", False):
            return False, None
        return True, str(metadata.pop("_hook_retry_provider_id", "") or "") or None

    def consume_cancel_request(self, task: Task) -> str | None:
        """Return and clear a hook cancellation request for scheduler processing."""
        return str(task.hook_metadata.pop("_hook_cancel_reason", "") or "") or None

    def _entries(self) -> list[tuple[str, Path]]:
        """Return hook entry points in deterministic hook-ID order."""
        entries: list[tuple[str, Path]] = []
        for path in self._hooks_dir.iterdir():
            if path.is_file() and path.suffix == ".py" and not path.name.startswith("_"):
                entries.append((path.stem, path))
            elif path.is_dir() and not path.name.startswith("_") and (path / "app.py").is_file():
                entries.append((path.name, path / "app.py"))
        return sorted(entries, key=lambda item: item[0])

    def _is_enabled(self, hook_id: str, entry_path: Path) -> bool:
        """Read a literal ENABLED value without importing an inactive hook."""
        try:
            tree = ast.parse(entry_path.read_text(encoding="utf-8"), filename=str(entry_path))
        except (OSError, SyntaxError) as exc:
            log("hooks", "error", f"Cannot read hook {hook_id}: {exc}")
            return False
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "ENABLED" for target in node.targets
            ):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, bool):
                    return node.value.value
                log("hooks", "error", f"Hook {hook_id} ENABLED must be a literal boolean")
                return False
        return False

    def _import_module(self, hook_id: str, entry_path: Path) -> ModuleType | None:
        """Import one enabled hook module with a unique internal name."""
        module_name = f"_aidir_hook_{hook_id}"
        spec = importlib.util.spec_from_file_location(module_name, entry_path)
        if spec is None or spec.loader is None:
            log("hooks", "error", f"Cannot create import specification for hook {hook_id}")
            return None
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            log("hooks", "error", f"Hook {hook_id} import failed: {exc}")
            return None
        return module

    def _provider(self, provider_id: str) -> dict:
        """Return one configured model provider."""
        providers = ((self._full_config.get("models") or {}).get("providers") or {})
        provider = providers.get(provider_id) if isinstance(providers, dict) else None
        return provider if isinstance(provider, dict) else {}
