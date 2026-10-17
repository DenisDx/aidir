"""Make the `aiset` package importable without installing it.

`aiset/` is a separately tracked checkout at the project root and its importable
package lives in `aiset/src/aiset`. The project root itself is on `sys.path`, so
`import aiset` would otherwise find the `aiset/` directory as an empty namespace
package and fail on `aiset.resolver`.

The supported production form is `pip install -e ./aiset` into the aidir venv
(see aiset/README.md, "Integration boundary"). Until that is done, this module
puts `aiset/src` on the path. It is idempotent and never removes an entry, so a
real installation keeps winning.
"""
from __future__ import annotations

import sys
from pathlib import Path

AISET_SRC = Path(__file__).resolve().parent.parent.parent / "aiset" / "src"


def ensure_aiset_importable() -> Path | None:
    """Prepend `aiset/src` to `sys.path` when it exists; return the path used."""
    if not (AISET_SRC / "aiset" / "__init__.py").is_file():
        return None
    entry = str(AISET_SRC)
    if entry not in sys.path:
        sys.path.insert(0, entry)
    return AISET_SRC