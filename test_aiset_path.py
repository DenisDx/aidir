"""Importing aiset must fail with an instruction, not with a bare traceback.

`aiset` lives in a separate repository and reaches aidir as a Git submodule. Two
failures are easy to reach and were both hit in practice:

  * the clone was made without `--recurse-submodules`, so `./aiset` is empty;
  * the checkout is present but incomplete, so the package imports partially and
    fails deeper with `ImportError: cannot import name '__version__'`.

Both used to surface as a `ModuleNotFoundError` eight frames into `core/app.py`.
The messages are the contract tested here, together with the two details that
make them work at all: a cached PEP 420 namespace package must be discarded
before the real path is used, and any `ImportError` counts as unusable, not only
`ModuleNotFoundError`.
"""

import contextlib
import importlib.util
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent
MODULE_PATH = ROOT / "core" / "settings" / "aiset_path.py"


def load_module():
    """Load the module by path, so its module-level state is not shared."""
    spec = importlib.util.spec_from_file_location("aiset_path_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def isolated_aiset():
    """Run with no cached aiset modules and restore everything afterwards.

    The module under test purges `aiset*` from `sys.modules` when it retries,
    which would break every later test in the same process. Saving and restoring
    both `sys.modules` and `sys.path` keeps this test independent.
    """
    saved_modules = {
        name: module for name, module in sys.modules.items()
        if name == "aiset" or name.startswith("aiset.")
    }
    saved_path = list(sys.path)
    try:
        for name in list(saved_modules):
            del sys.modules[name]
        # The real checkout's `aiset/src` is on the path once core.settings has
        # been imported. Leaving it there would make "missing" unobservable.
        sys.path[:] = [
            entry for entry in sys.path
            if not entry.rstrip("/").endswith("/aiset/src")
        ]
        yield
    finally:
        for name in [n for n in sys.modules if n == "aiset" or n.startswith("aiset.")]:
            del sys.modules[name]
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path


class InstalledLibraryTests(unittest.TestCase):
    """The checkout in this repository must be usable as it stands."""

    def test_the_checked_out_library_imports(self):
        from core.settings.aiset_path import ensure_aiset_importable

        ensure_aiset_importable()
        import aiset.asgi  # noqa: F401
        import aiset.resolver  # noqa: F401

    def test_an_importable_library_is_not_put_on_the_path_again(self):
        module = load_module()
        before = list(sys.path)
        with mock.patch.object(module, "_library_error", return_value=None):
            self.assertIsNone(module.ensure_aiset_importable())
        self.assertEqual(before, sys.path)


class MissingLibraryTests(unittest.TestCase):
    """No checkout at all: name the three ways to get one."""

    def test_the_message_carries_the_commands(self):
        module = load_module()
        with isolated_aiset(), \
             mock.patch.object(module, "AISET_SRC", pathlib.Path("/nonexistent/aiset/src")):
            with self.assertRaises(ModuleNotFoundError) as caught:
                module.ensure_aiset_importable()

        error = caught.exception
        self.assertEqual(error.name, "aiset")
        message = str(error)
        self.assertIn("git submodule update --init --recursive", message)
        self.assertIn("git clone", message)
        self.assertIn("https://github.com/DenisDx/aiset.git", message)
        self.assertIn("./install.sh", message)
        self.assertIn("separate project", message)

    def test_it_does_not_claim_the_package_is_present(self):
        module = load_module()
        with isolated_aiset(), \
             mock.patch.object(module, "AISET_SRC", pathlib.Path("/nonexistent/aiset/src")):
            with self.assertRaises(ModuleNotFoundError) as caught:
                module.ensure_aiset_importable()
        self.assertNotIn("present at", str(caught.exception))


class IncompleteCheckoutTests(unittest.TestCase):
    """A present but unusable checkout has a different fix and must say so."""

    def test_a_partial_package_is_reported_as_incomplete(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp, isolated_aiset():
            # A package directory with an initialiser but none of the modules:
            # importable as a namespace, failing on `aiset.resolver`.
            package = pathlib.Path(tmp) / "aiset"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")

            with mock.patch.object(module, "AISET_SRC", pathlib.Path(tmp)):
                with self.assertRaises(ModuleNotFoundError) as caught:
                    module.ensure_aiset_importable()

            error = caught.exception
            message = str(error)
            self.assertEqual(error.name, "aiset")
            self.assertIn("present at", message)
            self.assertIn("__init__.py", message)
            self.assertIn("submodule status", message)
            self.assertIn("Underlying error:", message)
            self.assertIn("git -C aiset pull --ff-only", message)

    def test_a_broken_module_is_not_reported_as_missing(self):
        """`ImportError` from inside the package counts, not only a missing name."""
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp, isolated_aiset():
            package = pathlib.Path(tmp) / "aiset"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            # The failure a damaged checkout produces: a name the package should
            # export does not exist, so the import raises ImportError.
            (package / "resolver.py").write_text(
                "from . import __version__  # noqa: F401\n", encoding="utf-8"
            )

            with mock.patch.object(module, "AISET_SRC", pathlib.Path(tmp)):
                with self.assertRaises(ModuleNotFoundError) as caught:
                    module.ensure_aiset_importable()

            self.assertIn("Underlying error: ImportError", str(caught.exception))


class NamespacePackageTests(unittest.TestCase):
    """The project root puts a bare `aiset/` on the path; it must not win."""

    def test_a_cached_namespace_package_is_discarded(self):
        module = load_module()
        fake = type(sys)("aiset")          # a namespace package has no __file__
        with mock.patch.dict(sys.modules, {"aiset": fake}), \
             mock.patch.dict(sys.modules, {"aiset.resolver": type(sys)("aiset.resolver")}):
            module._discard_cached_aiset()
            self.assertNotIn("aiset", sys.modules)
            self.assertNotIn("aiset.resolver", sys.modules)

    def test_a_real_package_is_left_alone(self):
        module = load_module()
        real = type(sys)("aiset")
        real.__file__ = "/somewhere/aiset/__init__.py"
        with mock.patch.dict(sys.modules, {"aiset": real}):
            module._discard_cached_aiset()
        # Only reached when the library was already usable, which never happens
        # in practice; the guard exists so a future caller cannot corrupt state.
        self.assertIn("aiset", sys.modules)


if __name__ == "__main__":
    unittest.main()
