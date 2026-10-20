"""The aidir doctor: configuration through aiset, host state directly.

The checks that matter for the two real incidents: a drifted configuration
(040) and Redis on a port nothing publishes (110). Both must be visible in one
run, and `--fix-all` must repair the configuration while never touching a
partially repairable file.
"""

import re
import shutil
import socket
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from core import doctor

ROOT = Path(__file__).resolve().parent


def make_root(target: Path, *, stale_key: bool = False, env: dict | None = None) -> Path:
    """A minimal project root: the real descriptor and config, a private .env."""
    for name in ("config.descriptor.json5", "config.types.json5", "config.json5"):
        shutil.copy(ROOT / name, target / name)
    text = (ROOT / ".env").read_text(encoding="utf-8")
    text = re.sub(r"(?m)^AIDIR_ROOT=.*$", f"AIDIR_ROOT={target}", text)
    for key, value in (env or {}).items():
        text = re.sub(rf"(?m)^{re.escape(key)}=.*$", f"{key}={value}", text)
    (target / ".env").write_text(text, encoding="utf-8")
    if stale_key:
        config = target / "config.json5"
        body = config.read_text(encoding="utf-8")
        body = body.replace('  "version": "1.0",',
                            '  "version": "1.0",\n  "stale_key": 1,', 1)
        config.write_text(body, encoding="utf-8")
    return target


def free_port() -> int:
    """A port that is free right now."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class HelpTests(unittest.TestCase):
    """Help must work before the configuration is imported."""

    def test_usage_lists_the_doctor(self):
        text = doctor.usage()
        self.assertIn("doctor", text)
        self.assertIn("--help", text)

    def test_the_doctor_module_does_not_import_the_configuration_at_load(self):
        """`core.config` builds the config at import and raises on a broken file.

        The doctor is imported before that module on purpose; a top-level import
        here would make the command unusable exactly when it is needed.
        """
        source = (ROOT / "core" / "doctor.py").read_text(encoding="utf-8")
        top = source.split("def ", 1)[0]
        self.assertNotIn("from core.config", top)
        self.assertNotIn("import core.config", top)


class ConfigCheckTests(unittest.TestCase):
    """The library doctor, reported through the host check."""

    def test_a_clean_configuration_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp))
            report = doctor.run(root=root, only=["config"])
        check = report.checks[0]
        self.assertEqual(check.status, "ok")
        self.assertEqual(report.exit_code, 0)

    def test_a_stale_key_is_reported_with_its_fix_class(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp), stale_key=True)
            report = doctor.run(root=root, only=["config"])
        check = report.checks[0]
        self.assertEqual(check.status, "fail")
        self.assertEqual(report.exit_code, 1)
        paths = {issue["path"]: issue for issue in check.issues}
        self.assertEqual(paths["/stale_key"]["fix"], "drop_unknown")
        self.assertTrue(any("drop_unknown" in line for line in check.details))

    def test_fix_does_not_delete_an_undeclared_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp), stale_key=True)
            report = doctor.run(root=root, only=["config"], fix=True)
            still_there = "stale_key" in (root / "config.json5").read_text(encoding="utf-8")
        self.assertEqual(report.checks[0].status, "fail")
        self.assertTrue(still_there)

    def test_fix_all_deletes_the_key_and_reports_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp), stale_key=True)
            report = doctor.run(root=root, only=["config"], fix_all=True)
            body = (root / "config.json5").read_text(encoding="utf-8")
        self.assertEqual(report.checks[0].status, "ok")
        self.assertEqual(report.exit_code, 0)
        self.assertNotIn("stale_key", body)
        self.assertTrue(report.checks[0].fixes)

    def test_dry_run_plans_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp), stale_key=True)
            before = (root / "config.json5").read_text(encoding="utf-8")
            report = doctor.run(root=root, only=["config"], fix_all=True, dry_run=True)
            after = (root / "config.json5").read_text(encoding="utf-8")
        self.assertEqual(before, after)
        self.assertEqual(report.checks[0].status, "fail")
        self.assertTrue(report.checks[0].fixes)
        self.assertTrue(any("stale_key" in item for item in report.skipped))


class HostCheckTests(unittest.TestCase):
    """Checks that need nothing but the filesystem and a socket."""

    def test_env_root_mismatch_is_a_warning(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            root = make_root(Path(tmp), env={"AIDIR_ROOT": other})
            check = doctor.run(root=root, only=["env-root"]).checks[0]
        self.assertEqual(check.status, "warn")
        self.assertIn("install.sh", " ".join(check.details))

    def test_env_root_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp))
            check = doctor.run(root=root, only=["env-root"]).checks[0]
        self.assertEqual(check.status, "ok")

    def test_missing_directories_are_reported_and_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp))
            check = doctor.run(root=root, only=["dirs"]).checks[0]
            self.assertEqual(check.status, "fail")
            check = doctor.run(root=root, only=["dirs"], fix=True).checks[0]
            self.assertEqual(check.status, "ok")
            self.assertTrue((root / "logs" / "audit").is_dir())
            self.assertTrue((root / "docker" / "volumes" / "redis").is_dir())

    def test_an_unreachable_redis_is_reported_quickly(self):
        port = free_port()
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp), env={"REDIS_HOST": "127.0.0.1",
                                             "REDIS_PORT": str(port)})
            check = doctor.run(root=root, only=["redis"]).checks[0]
        self.assertEqual(check.status, "fail")
        self.assertIn(str(port), check.message)

    def test_the_store_lock_is_not_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp))
            check = doctor.run(root=root, only=["store-lock"]).checks[0]
        self.assertEqual(check.status, "ok")

    def test_an_unknown_check_is_rejected(self):
        with self.assertRaises(SystemExit):
            doctor.run(only=["nonsense"])


class ConcurrencyTests(unittest.TestCase):
    """The service may be running while the doctor runs."""

    def _proc_tree(self, target: Path, entries: dict[int, bytes]) -> Path:
        for pid, cmdline in entries.items():
            directory = target / str(pid)
            directory.mkdir()
            (directory / "cmdline").write_bytes(cmdline)
        return target

    def test_the_doctor_does_not_count_itself_as_a_running_service(self):
        """`pgrep -f core/app.py` matches `core/app.py doctor`; /proc must not."""
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._proc_tree(Path(tmp), {10: b"python\0core/app.py\0doctor\0"})
            context = doctor.Context(root=ROOT)
            self.assertFalse(doctor._aidir_running(context, proc_root=proc))

    def test_a_core_process_is_recognised(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._proc_tree(Path(tmp), {
                10: b"python\0core/app.py\0doctor\0",
                11: b"python\0core/app.py\0",
            })
            context = doctor.Context(root=ROOT)
            self.assertTrue(doctor._aidir_running(context, proc_root=proc))

    def test_the_json_report_says_whether_aidir_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp))
            with unittest.mock.patch.object(doctor, "_aidir_running", return_value=True):
                payload = doctor.run(root=root, only=["env-root"]).to_dict()
        self.assertTrue(payload["aidir_running"])

    def test_a_configuration_repair_asks_for_a_reload_while_running(self):
        """The repaired file is not picked up by a running process by itself."""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp), stale_key=True)
            with unittest.mock.patch.object(doctor, "_aidir_running", return_value=True):
                report = doctor.run(root=root, only=["config"], fix_all=True)
        details = " ".join(report.checks[0].details)
        self.assertIn("systemctl reload aidir", details)


class WrapperTests(unittest.TestCase):
    """doctor.sh forwards its arguments to `core/app.py doctor`."""

    def test_help_lists_every_option_and_explanation(self):
        result = subprocess.run(
            [str(ROOT / "doctor.sh"), "--help"], capture_output=True, text=True, timeout=120
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for option in ("-h, --help", "--config-only", "--quick", "--fix", "--fix-all",
                       "--root", "--only", "--json", "--dry-run"):
            self.assertIn(option, result.stdout)
        self.assertIn("exit codes", result.stdout)
        self.assertIn("fix classes", result.stdout)

    def test_the_short_form_is_the_same_help(self):
        long = subprocess.run([str(ROOT / "doctor.sh"), "--help"], capture_output=True,
                              text=True, timeout=120)
        short = subprocess.run([str(ROOT / "doctor.sh"), "-h"], capture_output=True,
                               text=True, timeout=120)
        self.assertEqual(long.stdout, short.stdout)

    def test_it_runs_from_another_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [str(ROOT / "doctor.sh"), "--config-only"],
                cwd=tmp, capture_output=True, text=True, timeout=180,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("config", result.stdout)

    def test_a_missing_venv_is_explained(self):
        """The wrapper must not fail with a bare Python error."""
        source = (ROOT / "doctor.sh").read_text(encoding="utf-8")
        self.assertIn("./install.sh", source)


class ReportTests(unittest.TestCase):
    """The JSON shape automation depends on."""

    def test_json_has_a_schema_version_and_an_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp))
            payload = doctor.run(root=root, only=["env-root"]).to_dict()
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["exit_code"], 0)
        self.assertTrue(payload["ok"])
        self.assertEqual([check["id"] for check in payload["checks"]], ["env-root"])

    def test_a_startup_report_is_one_line(self):
        summary = doctor.startup_report()
        self.assertIn("doctor:", summary)
        self.assertEqual(len(summary.splitlines()), 1)

    def test_text_rendering_indents_details(self):
        result = doctor.CheckResult("x", "fail", "broken", details=["line one"])
        text = doctor._render(doctor.Report(checks=[result]))
        self.assertIn("x", text)
        self.assertIn("line one", text)


if __name__ == "__main__":
    unittest.main()
