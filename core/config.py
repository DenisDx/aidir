"""
Configuration access for aidir.

The subsystem lives in `core/settings/` and is built on the `aiset` library:
`config.descriptor.json5` is the schema of `config.json5`, each
`workers/<type>/<id>/descriptor.json5` is the schema of that worker's own
configuration, and `config.types.json5` holds the shapes they share.

This module is the address the rest of the project imports. It owns nothing:
the object it exposes is the compatibility facade, which keeps the dotted API
that every reader already uses:

    config.get("logging.level")   -> resolved value, or the default
    config.raw()                  -> whole configuration, placeholders resolved
    config.raw_text()             -> the file exactly as stored

Two deliberate differences from the library contract, both required by the
existing readers and recorded in plan_aiset_integration.md:

* `get()` returns the default for an explicit `null`, not only for an absent
  key. The loader this module replaced did the same.
* `raw()` returns the *effective* (resolved) dict, because every reader expects
  `${VAR}` to have been substituted.

Startup policy: a configuration file that does not match its descriptor stops
the process with a precise diagnostic. A settings subsystem that quietly serves
an empty configuration is worse than a refused start.
"""
from __future__ import annotations

from core.settings.facade import AisetConfig as Config
from core.settings.runtime import get_runtime

# The process-wide configuration object, built once at import. Every importer in
# the project shares it, so a write made through the WebUI and a read made by a
# worker see the same values at the same time. Building it here also means a
# configuration file that does not match its descriptor fails at import, with the
# issue list, rather than somewhere later with an empty configuration.
config = Config(runtime=get_runtime())

__all__ = ["Config", "config"]