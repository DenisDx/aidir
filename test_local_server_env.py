"""Regression tests for the environment and the diagnostics of a managed local server.

A managed `llama-server` must see the same environment the configuration
describes, and a failed startup must leave its output behind instead of
losing it with the process.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.local_server_manager import LocalServerError, LocalServerManager
from core.settings.runtime import (
    SettingsRuntime,
    apply_dotenv_to_environment,
    build_resolver,
    read_dotenv,
)


class DotenvEnvironmentTests(unittest.TestCase):
    """The `.env` values must reach the process that spawns the local servers."""

    def test_publishing_keeps_the_shell_value(self):
        """A variable already exported in the shell keeps its value."""
        name = "AIDIR_TEST_KEEP"
        previous = os.environ.get(name)
        os.environ[name] = "from-shell"
        try:
            apply_dotenv_to_environment({name: "from-file", "AIDIR_TEST_NEW": "from-file"})
            self.assertEqual(os.environ[name], "from-shell")
            self.assertEqual(os.environ["AIDIR_TEST_NEW"], "from-file")
        finally:
            os.environ.pop("AIDIR_TEST_NEW", None)
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous

    def test_the_running_instance_publishes_its_own_env(self):
        """The application runtime publishes its `.env` into the process environment."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("AIDIR_TEST_PUBLISHED=from-file\n", encoding="utf-8")
            os.environ.pop("AIDIR_TEST_PUBLISHED", None)
            try:
                SettingsRuntime(root=root, publish_environment=True)
                self.assertEqual(os.environ.get("AIDIR_TEST_PUBLISHED"), "from-file")
            finally:
                os.environ.pop("AIDIR_TEST_PUBLISHED", None)

    def test_a_runtime_for_another_root_leaves_the_environment_alone(self):
        """The doctor inspects another checkout and must not publish its `.env`."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text("AIDIR_TEST_ISOLATED=from-file\n", encoding="utf-8")
            os.environ.pop("AIDIR_TEST_ISOLATED", None)
            try:
                SettingsRuntime(root=root, environ={})
                self.assertNotIn("AIDIR_TEST_ISOLATED", os.environ)
            finally:
                os.environ.pop("AIDIR_TEST_ISOLATED", None)

    def test_an_isolated_resolver_uses_the_root_env_file(self):
        """The placeholders of another root are resolved against that root's `.env`."""
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("AIDIR_TEST_VALUE=from-file\n", encoding="utf-8")
            resolver = build_resolver(env_file, environ={})
            self.assertEqual(resolver.lookup("AIDIR_TEST_VALUE", None)[0], "from-file")
            self.assertEqual(resolver.lookup("AIDIR_TEST_VALUE", None)[1], "dotenv")
            self.assertEqual(resolver.lookup("AIDIR_TEST_MISSING", "fallback")[0], "fallback")

    def test_read_dotenv_keeps_the_values_as_data(self):
        """The `.env` file is parsed into a mapping without touching the environment."""
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("# comment\nexport AIDIR_TEST_DATA='value'\n", encoding="utf-8")
            values = read_dotenv(env_file)
        self.assertEqual(values.get("AIDIR_TEST_DATA"), "value")


class ManagedServerEnvironmentTests(unittest.TestCase):
    """The managed server must receive an explicit environment."""

    def test_the_provider_environment_is_applied_on_top(self):
        """A provider declares the variables its server needs."""
        manager = LocalServerManager({}, Path("/tmp"))
        env = manager._child_env({"env": {"CUDA_VISIBLE_DEVICES": "0", "LLAMA_ARG": "value"}})
        self.assertEqual(env.get("CUDA_VISIBLE_DEVICES"), "0")
        self.assertEqual(env.get("LLAMA_ARG"), "value")
        self.assertIn("PATH", env)

    def test_the_process_environment_is_inherited(self):
        """The process environment is the base of the child environment."""
        manager = LocalServerManager({}, Path("/tmp"))
        env = manager._child_env({})
        self.assertEqual(env.get("PATH"), os.environ.get("PATH"))


class ManagedServerOutputTests(unittest.IsolatedAsyncioTestCase):
    """A failed startup must leave its output behind."""

    async def test_the_child_output_reaches_the_provider_log(self):
        """The managed server writes into its own log file."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {"models": {"providers": {"llama_local": {
                "baseUrl": "http://127.0.0.1:9",
                "exec_cmd": "/bin/sh -c 'echo first-line; echo second-line; sleep 5'",
                "startup_timeout": 2,
            }}}}
            manager = LocalServerManager(config, root)
            with self.assertRaises(LocalServerError):
                await manager.ensure_running("llama_local", "model")

            log_text = manager._log_path("llama_local").read_text(encoding="utf-8")
            self.assertIn("first-line", log_text)
            self.assertIn("second-line", log_text)

    async def test_a_failed_startup_reports_the_output_and_the_exit_code(self):
        """The failure names the exit code and points at the log file."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {"models": {"providers": {"llama_local": {
                "baseUrl": "http://127.0.0.1:9",
                "exec_cmd": "/bin/sh -c 'echo boom; exit 3'",
                "startup_timeout": 2,
            }}}}
            manager = LocalServerManager(config, root)
            with self.assertRaises(LocalServerError) as raised:
                await manager.ensure_running("llama_local", "model")

            error = manager.startup_error("llama_local")
            self.assertEqual(error["code"], "LLAMA_CPP_START_FAILED")
            self.assertIn("exited with code", error["message"])

    async def test_the_log_tail_is_read_from_the_provider_file(self):
        """The tail helper returns the last lines of the provider log."""
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "provider.log"
            log_path.write_text("one\ntwo\nthree\n", encoding="utf-8")
            tail = LocalServerManager._log_tail(log_path, limit=2)
        self.assertEqual(tail, "two\nthree")


if __name__ == "__main__":
    unittest.main()
