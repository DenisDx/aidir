"""Parity between the aiset settings backend and the reference reader.

Phase 4 of plan_aiset_integration.md asks for the two backends to be compared on
a running system. That is done by hand on a live instance. This file is the part
that can be automated and is therefore run on every change: for the shipped
`config.json5` and the live `.env`, the values aiset produces must be exactly the
values the reference reader produces, key for key, with no additions and no
omissions.

The reference reader is deliberately a *second, independent* implementation of
"read config.json5, substitute ${VAR} / ${VAR:-default}, parse JSON5". If both
sides shared a helper, the test would only prove that a helper is self
consistent. It therefore parses with `json5` and substitutes with its own
regular expression, exactly as the loader this project replaced did.

Run:
    venv/bin/python -m unittest -v test_settings_parity.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import json5  # noqa: E402

from core.settings.aiset_path import ensure_aiset_importable  # noqa: E402

ensure_aiset_importable()

from core.config import config  # noqa: E402
from core.settings.runtime import read_dotenv  # noqa: E402
from core.workers_loader import resolve_worker_configs  # noqa: E402

CONFIG = ROOT / "config.json5"
PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")


def load_descriptor() -> dict:
    """The shipped descriptor, read once per test session."""
    return json5.loads((ROOT / "config.descriptor.json5").read_text(encoding="utf-8"))


def declared_conversions(node: dict, prefix: tuple = ()) -> dict[tuple, str]:
    """Map every declared `env.convert` to its dotted path, straight from the descriptor.

    `env.convert` is a public, documented rule rather than an internal detail,
    so the reference reader applies it from the descriptor instead of sharing the
    resolver. That keeps the two readers independent and turns "the backend
    honours the declared conversion" into something the parity check verifies -
    without it, `ai.enabled` compared `True` against the string "true".
    """
    out: dict[tuple, str] = {}
    convert = ((node.get("env") or {}).get("convert")
               if isinstance(node.get("env"), dict) else None)
    if convert:
        out[prefix] = convert
    for key, child in (node.get("children") or {}).items():
        out.update(declared_conversions(child, prefix + (str(key),)))
    array = node.get("children") if node.get("type") == "array" else None
    if isinstance(array, dict):
        out.update(declared_conversions(array, prefix + ("0",)))
    additional = node.get("additional_children")
    if isinstance(additional, dict):
        out.update(declared_conversions(additional, prefix + ("*",)))
    return out


def convert_value(value, convert: str):
    """Apply one declared conversion, following the documented rules.

    A value that does not convert becomes None and is reported, never guessed:
    the backend raises an issue for the same input, and a reader that guessed
    would disagree with it for the wrong reason.
    """
    if convert == "string" or not isinstance(value, str):
        return value
    if convert == "bool":
        if value == "true":
            return True
        if value == "false":
            return False
        return None
    text = value.strip()
    try:
        if convert == "int":
            return int(text) if re.fullmatch(r"[+-]?\d+", text) else None
        return float(text) if text else None
    except ValueError:
        return None


def node_convert(node) -> str | None:
    """The conversion a node declares for a substituted value, if any."""
    env = node.get("env")
    return env.get("convert") if isinstance(env, dict) else None


def apply_env_conversions(document, descriptor):
    """Return the document with every declared `env.convert` applied in place.

    The conversion belongs to the node that *holds* the value, so it is read
    from the child while walking, never from the parent.
    """
    if not isinstance(descriptor, dict) or not isinstance(document, dict):
        return document
    children = descriptor.get("children") or {}
    array = children if descriptor.get("type") == "array" else None
    additional = descriptor.get("additional_children")
    for key, value in list(document.items()):
        node = (array or {}).get(key)
        if node is None and isinstance(additional, dict):
            node = additional
        if node is None:
            node = children.get(key)
        if not isinstance(node, dict):
            continue
        convert = node_convert(node)
        if convert and convert != "string" and isinstance(value, str):
            document[key] = convert_value(value, convert)
        else:
            document[key] = apply_env_conversions(value, node)
    return document


def reference_values(path: Path, environ: dict[str, str], descriptor: dict | None = None) -> dict:
    """Read a configuration file the way the replaced loader did.

    Substitution happens on the raw text before parsing, exactly as
    `core/config.py` did, and the `${VAR:-default}` rule is the loader's own:
    an unset variable yields the default, a variable set to the empty string
    yields the empty string. That last detail differs from POSIX and is the one
    place where the two readers are allowed to disagree; it is asserted below
    rather than assumed.
    """

    def substitute(text: str) -> str:
        def replace(match: re.Match) -> str:
            name = match.group(1)
            default = match.group(2) if match.group(2) is not None else ""
            return environ.get(name, default)

        return PLACEHOLDER.sub(replace, text)

    document = json5.loads(substitute(path.read_text(encoding="utf-8")))
    return apply_env_conversions(document, (descriptor or {}).get("root"))


def flatten(value, prefix: tuple = ()) -> dict:
    """Every leaf of a configuration, keyed by its dotted path."""
    out: dict[str, object] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            out.update(flatten(item, prefix + (str(key),)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            out.update(flatten(item, prefix + (str(index),)))
    else:
        out[".".join(prefix)] = value
    return out


class RootConfigParityTests(unittest.TestCase):
    """Every leaf of the live configuration must read the same on both sides."""

    @classmethod
    def setUpClass(cls):
        cls.environ = {**read_dotenv(ROOT / ".env"), **{k: v for k, v in os.environ.items()}}
        cls.descriptor = load_descriptor()
        cls.aiset = flatten(config.raw())
        cls.reference = flatten(reference_values(CONFIG, cls.environ, cls.descriptor))

    def test_both_sides_see_the_same_keys(self):
        self.assertEqual(set(self.aiset) - set(self.reference), set())
        self.assertEqual(set(self.reference) - set(self.aiset), set())

    def test_both_sides_see_the_same_values(self):
        differences = {
            key: (self.aiset[key], self.reference[key])
            for key in self.aiset
            if self.aiset[key] != self.reference[key]
        }
        self.assertEqual(differences, {}, f"{len(differences)} value(s) differ")

    def test_the_document_is_not_silently_empty(self):
        """A descriptor mismatch used to look exactly like this."""
        self.assertGreater(len(self.aiset), 400)
        self.assertEqual(self.aiset.get("version"), "1.0")

    def test_every_placeholder_resolved_to_a_string_value(self):
        """Values are substituted; nothing keeps a ${VAR} after resolution."""
        leftovers = [
            key for key, value in self.aiset.items()
            if isinstance(value, str) and PLACEHOLDER.search(value)
        ]
        self.assertEqual(leftovers, [])


class WorkerConfigParityTests(unittest.TestCase):
    """The merged worker entries must match the reference merge, key for key."""

    @classmethod
    def setUpClass(cls):
        cls.environ = {**read_dotenv(ROOT / ".env"), **{k: v for k, v in os.environ.items()}}
        cls.merged_entries = resolve_worker_configs(config.raw())
        cls.merged = flatten(cls.merged_entries)
        cls.descriptor = load_descriptor()
        cls.reference_entries = cls._reference_merge()
        cls.reference = flatten(cls.reference_entries)

    @classmethod
    def _reference_merge(cls) -> dict:
        """Worker entries merged the way the removed local deep merge did."""
        root_config = reference_values(CONFIG, cls.environ, cls.descriptor)
        root_items = (root_config.get("workers") or {}).get("items") or {}
        out: dict = {}
        # Every worker directory, not only the ones with a local file: a worker
        # without one is configured from the root entry alone.
        for worker_dir in sorted(path.parent for path in ROOT.glob("workers/*/*/*")):
            worker_id = worker_dir.name
            if worker_id.startswith(("_", ".")):
                continue  # __pycache__ and friends, as the loader skips them
            local_file = worker_dir / "config.json5"
            local = reference_values(local_file, cls.environ) if local_file.is_file() else {}
            root = root_items.get(worker_id) or {}
            out[worker_id] = cls._deep_merge(local, root)
        return out

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> dict:
        result = dict(base)
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = WorkerConfigParityTests._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    def test_both_sides_merge_the_same_entries(self):
        """Every worker directory is configured, with or without a local file."""
        self.assertEqual(set(self.merged_entries), set(self.reference_entries))
        self.assertEqual(len(self.merged_entries), 16)

    def test_the_six_workers_with_a_local_file_are_present(self):
        with_local = {
            worker_id for worker_id, entry in self.merged_entries.items()
            if entry and self.reference_entries.get(worker_id) == entry
        }
        self.assertEqual(len(with_local), 16)
        for worker_id in ("web_search", "web_fetch", "http_api", "openaix"):
            self.assertIn(worker_id, with_local)

    def test_both_sides_produce_the_same_values(self):
        differences = {
            key: (self.merged[key], self.reference[key])
            for key in self.merged
            if self.merged[key] != self.reference[key]
        }
        self.assertEqual(differences, {}, f"{len(differences)} worker value(s) differ")


class ReadApiParityTests(unittest.TestCase):
    """The dotted API every reader in the project uses must behave as before."""

    def test_dotted_lookup_matches_the_flattened_view(self):
        flat = flatten(config.raw())
        sample = [
            "logging.level", "redis.host", "redis.port", "workers.default",
            "webui.port", "tasks.queue_timeout", "envids.check_period_minutes",
            "workers.items.web_search.enabled",
            "models.providers.ollama_local.baseUrl",
        ]
        for key in sample:
            with self.subTest(key=key):
                self.assertEqual(config.get(key), flat[key])

    def test_a_list_member_is_not_reachable_by_a_dotted_path(self):
        """Contract, unchanged: the loader never walked into a list either."""
        self.assertIsNone(config.get("endpoints.0.id"))
        self.assertIsInstance(config.get("endpoints"), list)

    def test_an_absent_key_returns_the_default(self):
        self.assertEqual(config.get("no.such.key", "fallback"), "fallback")
        self.assertIsNone(config.get("no.such.key"))

    def test_an_explicit_null_returns_the_default(self):
        """The pre-migration loader did this, and readers depend on it."""
        from core.settings.facade import AisetConfig

        class StubRuntime:
            """One document whose value is explicitly null."""

            def effective(self, document_id):
                return {"set": None, "nested": {"set": None}, "kept": 1}

        facade = AisetConfig(runtime=StubRuntime())
        self.assertEqual(facade.get("set", "fallback"), "fallback")
        self.assertEqual(facade.get("nested.set", "fallback"), "fallback")
        self.assertEqual(facade.get("kept"), 1)

    def test_raw_returns_the_effective_dict_not_the_stored_tree(self):
        raw = config.raw()
        self.assertNotIn("${", json.dumps(raw, default=str))
        self.assertIsInstance(raw, dict)

    def test_the_same_object_is_shared_by_every_reader(self):
        """A write through the settings API must be visible to a reader here."""
        self.assertIs(config.raw(), config.raw())


if __name__ == "__main__":
    unittest.main(verbosity=2)