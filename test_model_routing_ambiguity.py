"""Tests for duplicate model ids and sticky routing."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from core.app import Core


class _Config:
    """Dotted configuration stub."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def get(self, key: str, default=None):
        """Return a nested value."""
        node = self._data
        for part in key.split("."):
            if not isinstance(node, dict):
                return default
            node = node.get(part)
            if node is None:
                return default
        return node


class AmbiguousModelWarningTests(unittest.TestCase):
    """The startup warning names every external id exposed by several providers."""

    def test_duplicate_external_id_is_reported_once(self) -> None:
        core = Core.__new__(Core)
        core.config = _Config({"models": {"providers": {
            "p1": {"models": [{"id": "same", "alias": "same-alias"}]},
            "p2": {"models": [{"id": "same", "alias": "other"}]},
        }}})

        with patch("core.app.log") as log_mock:
            core._warn_ambiguous_model_ids()

        log_mock.assert_called_once()
        args = log_mock.call_args.args
        self.assertEqual(args[0], "system")
        self.assertEqual(args[1], "warning")
        self.assertIn("same", args[2])
        self.assertIn("p1/same", args[2])
        self.assertIn("p2/same", args[2])

    def test_unique_ids_do_not_warn(self) -> None:
        core = Core.__new__(Core)
        core.config = _Config({"models": {"providers": {
            "p1": {"models": [{"id": "one"}]},
            "p2": {"models": [{"id": "two"}]},
        }}})

        with patch("core.app.log") as log_mock:
            core._warn_ambiguous_model_ids()

        log_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
