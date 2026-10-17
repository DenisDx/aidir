"""
aiset-backed settings subsystem of aidir.

The package is imported when `core.config` is imported, which is when the
process configuration is first needed.

| Module | Responsibility |
|---|---|
| `aiset_path` | Puts `aiset/src` on `sys.path` when the package is not installed. |
| `runtime` | Builds the one `Service`, registers every document, owns the effective cache. |
| `documents` | Finds worker-local configuration files and performs the root-wins merge. |
| `facade` | The `config` object the rest of aidir already uses, on top of the service. |
| `wsgi` | The ASGI application for the WebUI and the session-to-principal mapping. |
"""
from core.settings.runtime import (
    CORE_PRINCIPAL,
    ROOT,
    SettingsError,
    SettingsRuntime,
    build_resolver,
    get_runtime,
    read_dotenv,
    set_runtime,
)

__all__ = [
    "CORE_PRINCIPAL",
    "ROOT",
    "SettingsError",
    "SettingsRuntime",
    "build_resolver",
    "get_runtime",
    "read_dotenv",
    "set_runtime",
]