"""Make the `aiset` package importable, or say precisely why it is not.

`aiset` is a separate project with its own history. aidir consumes it in one of
two ways:

* as the Git submodule at `./aiset` (`.gitmodules`), whose importable package is
  `aiset/src/aiset`. This is the normal layout after `git clone
  --recurse-submodules`, and it is what `install.sh` initialises;
* as a package installed into the venv (`pip install -e ./aiset`), which is the
  form documented by the library.

The project root is on `sys.path`, so a bare `aiset/` directory also appears as
an empty namespace package that imports but has no `resolver`. That is why the
check below imports the modules aidir actually needs instead of trusting
`import aiset`.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

AISET_SRC = Path(__file__).resolve().parent.parent.parent / "aiset" / "src"
AISET_REPO = "https://github.com/DenisDx/aiset.git"

# What aidir imports. A directory that satisfies none of these is not an
# installation, however importable its name looks.
REQUIRED_MODULES = (
    "aiset.resolver",
    "aiset.service",
    "aiset.store",
    "aiset.asgi",
    "aiset.merge",
)

MISSING_AISET = f"""\
The aiset settings library is not available.

aidir imports aiset from {AISET_SRC}, which is a Git submodule, or from a package
installed in the venv. Install it with one of:

  git submodule update --init --recursive   # normal case: the clone skipped it
  git clone {AISET_REPO} aiset    # no submodule configured yet
  ./install.sh                              # performs the submodule step itself

aiset is a separate project with its own history and its own tests; see
aiset/README.md. Nothing else in this repository vendors a copy of it."""

INCOMPLETE_AISET = f"""\
The aiset library is present at {AISET_SRC} but cannot be imported.

The usual cause is an incomplete checkout: package initialisers (`__init__.py`)
are missing, so `aiset.__version__` and the asset helpers do not exist and
`aiset.asgi` cannot load. A checkout can look complete while only the Python
files tracked in Git are present.

Check, from the repository root, in this order:

  git submodule status                             # uninitialised submodule?
  ls {AISET_SRC}/aiset/__init__.py                 # does the package start?
  git -C aiset log -1 --oneline                    # which commit is pinned?

Then bring the library and the pinned pointer up to date:

  git submodule update --init --recursive
  git -C aiset pull --ff-only && git add aiset && git commit -m "aiset: update\""""


def _aiset_modules() -> list[str]:
    """Names of every aiset module currently cached in `sys.modules`."""
    return [name for name in sys.modules if name == "aiset" or name.startswith("aiset.")]


def _discard_cached_aiset() -> None:
    """Forget a failed or namespace-only `aiset` so the real package can load.

    A failed `import aiset.resolver` still leaves `aiset` in `sys.modules`: it
    resolved to the bare `aiset/` directory at the project root, which has no
    `__init__.py` and is therefore a PEP 420 namespace package. Adding
    `aiset/src` afterwards would not help, because the cached stub shadows the
    real package. The same applies to modules left behind by a failed import.
    """
    for name in _aiset_modules():
        del sys.modules[name]


def _library_error() -> str | None:
    """The first import failure, or None when every required module loads.

    Any `ImportError` counts, not only `ModuleNotFoundError`: a package whose
    `__init__.py` is missing fails later with `ImportError: cannot import name
    '__version__'`, and reporting that as "not installed" would hide the cause.
    """
    for name in REQUIRED_MODULES:
        try:
            importlib.import_module(name)
        except ImportError as exc:
            return f"{type(exc).__name__}: {exc}"
    return None


def ensure_aiset_importable() -> Path | None:
    """Make the library importable; raise an actionable error when it is absent.

    Returns the path added to `sys.path`, or None when an installed package
    already provides it and nothing had to be added.
    """
    if _library_error() is None:
        return None
    if (AISET_SRC / "aiset").is_dir():
        # A directory is enough: the package may legitimately have no
        # `__init__.py` and load as a namespace package. What matters is that
        # its modules import, which is checked immediately below.
        entry = str(AISET_SRC)
        if entry not in sys.path:
            sys.path.insert(0, entry)
        _discard_cached_aiset()
        error = _library_error()
        if error is None:
            return AISET_SRC
        raise ModuleNotFoundError(
            f"{INCOMPLETE_AISET}\n\nUnderlying error: {error}", name="aiset"
        )
    raise ModuleNotFoundError(MISSING_AISET, name="aiset")
