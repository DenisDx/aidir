"""HTTP transport for the aiset settings API.

The aidir WebUI is an ASGI application (FastAPI + uvicorn), so the library's ASGI
adapter is mounted directly. Nothing is bridged in the host any more: the
library owns the transport, the environ and the response assembly.

Authentication stays in the host. The session is resolved in
`webui/backend/app.py` and handed to aiset as a finished `Principal` through
`principal_from_scope`; a request without a session gets a principal with no
permissions and is refused by the library itself.
"""
from __future__ import annotations

from typing import Any

from core.settings.aiset_path import ensure_aiset_importable

ensure_aiset_importable()

from aiset.asgi import create_asgi_app  # noqa: E402
from aiset.service import PERMISSIONS, Principal, Service  # noqa: E402

SETTINGS_BASE = "/api/settings"
ASSET_BASE = "/assets"

READ_ONLY_PERMISSIONS = frozenset({"settings_read_source", "settings_read_effective"})
SESSION_PERMISSIONS = frozenset(
    {
        "settings_read_source",
        "settings_read_effective",
        "settings_validate",
        "settings_write",
        "settings_load_lkg",
    }
)
FULL_PERMISSIONS = SESSION_PERMISSIONS | {
    "settings_read_secret",
    "settings_edit_secret",
    "settings_descriptor_raw",
}

ANONYMOUS = Principal(id="anonymous", permissions=frozenset())

# The permission sets below are a subset of the library's, by construction. An
# explicit check keeps a future rename in the library from silently turning a
# grant into a denial at runtime. `assert` is not used: it disappears under
# `python -O`, which is exactly when a misconfigured deployment needs it most.
_UNKNOWN_PERMISSIONS = FULL_PERMISSIONS - frozenset(PERMISSIONS)
if _UNKNOWN_PERMISSIONS:
    raise RuntimeError(
        "aidir grants permissions aiset does not define: "
        + ", ".join(sorted(_UNKNOWN_PERMISSIONS))
    )


def principal_for(permissions: list[str] | None, login: str) -> Principal:
    """Map a WebUI account onto an aiset principal.

    `webui.auth.users[].permissions` carries group names, and `all` is the group
    every capability belongs to. Anything the library does not name is ignored,
    so an unknown group never accidentally grants access.
    """
    granted = {str(item) for item in (permissions or [])}
    if not granted:
        return ANONYMOUS
    if granted & {"all", "admin", "root"}:
        return Principal(id=login or "admin", permissions=FULL_PERMISSIONS)
    return Principal(
        id=login or "user",
        permissions=SESSION_PERMISSIONS & frozenset(granted) or READ_ONLY_PERMISSIONS,
    )


def build_asgi_app(service: Service, resolve_principal: Any = None) -> Any:
    """The aiset ASGI application mounted under `/api/settings`.

    `resolve_principal(scope)` is the host hook that turns a WebUI session into
    an aiset `Principal`. The library serves its own browser assets under
    `/assets/`, so the editor works with this mount alone.
    """
    return create_asgi_app(
        service,
        base=SETTINGS_BASE,
        principal_from_scope=resolve_principal,
    )