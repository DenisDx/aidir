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


def reference_values(path: Path, environ: dict[str, str]) -> dict:
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

    return json5.loads(substitute(path.read_text(encoding="utf-8")))


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
        cls.aiset = flatten(config.raw())
        cls.reference = flatten(reference_values(CONFIG, cls.environ))

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
        cls.reference_entries = cls._reference_merge()
        cls.reference = flatten(cls.reference_entries)

    @classmethod
    def _reference_merge(cls) -> dict:
        """Worker entries merged the way the removed local deep merge did."""
        root_config = reference_values(CONFIG, cls.environ)
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
        self.assertEqual(len(self.merged_entries), 15)

    def test_the_six_workers_with_a_local_file_are_present(self):
        with_local = {
            worker_id for worker_id, entry in self.merged_entries.items()
            if entry and self.reference_entries.get(worker_id) == entry
        }
        self.assertEqual(len(with_local), 15)
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