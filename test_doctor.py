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

    def test_a_stale_key_inside_a_worker_union_is_repaired(self):
        """040: `apiKey` sat at the worker level, inside a union node.

        The validator reports that as a union issue at the worker path; the
        repair has to remove the key itself, or the document can never become
        valid and every other planned fix is skipped.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp))
            config = root / "config.json5"
            body = config.read_text(encoding="utf-8")
            for worker in ("web_search", "web_fetch"):
                body = body.replace(
                    f'      "{worker}": {{',
                    f'      "{worker}": {{\n        "apiKey": "${{BRAVE_APIKEY}}",',
                    1,
                )
            config.write_text(body, encoding="utf-8")

            safe = doctor.run(root=root, only=["config"], fix=True)
            self.assertEqual(safe.checks[0].status, "fail")

            report = doctor.run(root=root, only=["config"], fix_all=True)
            after = config.read_text(encoding="utf-8")
        self.assertEqual(report.checks[0].status, "ok", report.checks[0].details)
        self.assertEqual(report.exit_code, 0)
        for worker in ("web_search", "web_fetch"):
            self.assertNotIn(f'"{worker}": {{\n        "apiKey"', after)

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


class VerdictTests(unittest.TestCase):
    """The last line must say plainly whether everything is fine."""

    def test_green_verdict_when_every_check_passes(self):
        report = doctor.Report(checks=[doctor.CheckResult("config", "ok", "valid")])
        text = doctor._render(report)
        self.assertIn("RESULT: all checks passed", text)
        self.assertNotIn("\033[", text)

    def test_red_verdict_when_a_check_fails(self):
        report = doctor.Report(checks=[doctor.CheckResult("redis", "fail", "down")])
        text = doctor._render(report)
        self.assertIn("RESULT: 1 problem(s) remain", text)
        self.assertEqual(report.exit_code, 1)

    def test_warnings_are_named_in_the_verdict(self):
        report = doctor.Report(checks=[
            doctor.CheckResult("systemd", "warn", "inactive"),
            doctor.CheckResult("config", "ok", "valid"),
        ])
        text = doctor._render(report)
        self.assertIn("all checks passed, 1 warning(s)", text)
        self.assertEqual(report.exit_code, 0)

    def test_colour_is_used_only_when_asked(self):
        report = doctor.Report(checks=[doctor.CheckResult("redis", "fail", "down")])
        self.assertIn("\033[31m", doctor._render(report, color=True))
        self.assertNotIn("\033[", doctor._render(report, color=False))

    def test_a_recommendation_is_rendered(self):
        result = doctor.CheckResult("redis", "fail", "down")
        result.recommendation = "restart Docker"
        text = doctor._render(doctor.Report(checks=[result]))
        self.assertIn("recommendations:", text)
        self.assertIn("restart Docker", text)

    def test_a_failed_repair_is_listed_not_hidden(self):
        report = doctor.Report(
            checks=[doctor.CheckResult("redis", "fail", "down")],
            failed=["redis: docker compose up -d redis -> exit status 1"],
        )
        text = doctor._render(report)
        self.assertIn("failed repairs:", text)
        self.assertIn("exit status 1", text)

    def test_an_unreachable_redis_reports_the_failed_repair_and_advice(self):
        """The 110 shape: unreachable, the repair cannot run, nothing is hidden."""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp), env={"REDIS_HOST": "127.0.0.1",
                                             "REDIS_PORT": str(free_port())})
            report = doctor.run(root=root, only=["redis"], fix_all=True)
            text = doctor._render(report)
        self.assertIn("RESULT: 1 problem(s) remain", text)
        self.assertTrue(report.failed or report.checks[0].fixes)
        self.assertIsNotNone(report.checks[0].recommendation)

    def test_json_carries_the_failed_repairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp), env={"REDIS_HOST": "127.0.0.1",
                                             "REDIS_PORT": str(free_port())})
            payload = doctor.run(root=root, only=["redis"], fix_all=True).to_dict()
        self.assertIn("failed", payload)
        self.assertEqual(payload["exit_code"], 1)


class PortConflictTests(unittest.TestCase):
    """`address already in use` is a host port conflict, not a broken Docker."""

    SS = (
        "State Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
        'LISTEN 0 511 0.0.0.0:8080 0.0.0.0:* users:(("docker-proxy",pid=812,fd=4))\n'
        'LISTEN 0 511 127.0.0.1:6380 0.0.0.0:* users:(("docker-proxy",pid=2041,fd=5))\n'
    )

    def test_the_listener_is_parsed(self):
        self.assertEqual(doctor._parse_listener(self.SS, 6380), "docker-proxy (pid 2041)")
        self.assertIsNone(doctor._parse_listener(self.SS, 6381))

    def test_an_invisible_pid_is_reported(self):
        output = "LISTEN 0 511 127.0.0.1:6380 0.0.0.0:*\n"
        self.assertIn("not visible", doctor._parse_listener(output, 6380))

    def test_a_docker_proxy_points_at_the_owning_container(self):
        text = doctor._redis_advice(
            state="no container",
            output="failed to bind host port 127.0.0.1:6380/tcp: address already in use",
            attempted=True,
            aidir_running=False,
            port=6380,
            holder="docker-proxy (pid 2041)",
        )
        self.assertIn("docker ps -a --filter publish=6380", text)
        self.assertIn("restart Docker", text)

    def test_a_foreign_process_is_named_and_never_blames_docker(self):
        text = doctor._redis_advice(
            state="no container",
            output="address already in use",
            attempted=False,
            aidir_running=False,
            port=6380,
            holder="redis-server (pid 512)",
        )
        self.assertIn("redis-server", text)
        self.assertIn("./install.sh", text)
        self.assertNotIn("restart Docker", text)

    def test_redis_alive_inside_but_not_on_the_host_points_at_the_mapping(self):
        text = doctor._redis_advice(
            state="running", output="Timeout connecting to server", attempted=True,
            aidir_running=False, port=6380, inside="pong",
        )
        self.assertIn("port mapping is broken", text)
        self.assertIn("restart docker", text.lower())

    def test_volume_permissions_are_fixed_with_chown_first(self):
        text = doctor._redis_advice(
            state="restarting", output="exit status 1", attempted=True,
            aidir_running=False, permission_denied=True,
        )
        self.assertIn("chown -R 999:999", text)
        self.assertNotIn("force-recreate", text)

    def test_a_foreign_holder_is_freed_before_anything_else(self):
        text = doctor._redis_advice(
            state="no container", output="", attempted=False, aidir_running=False,
            port=6380, holder="redis-server (pid 512)", foreign_holder=True,
        )
        self.assertIn("redis-server", text)
        self.assertIn("./install.sh", text)

    def test_an_unknown_holder_points_at_ss(self):
        text = doctor._redis_advice(
            state="no container",
            output="address already in use",
            attempted=False,
            aidir_running=False,
            port=6380,
        )
        self.assertIn("ss -ltnp", text)


class ServiceStateTests(unittest.TestCase):
    """Detecting a restart loop is what turns the report from misleading to useful."""

    def test_show_output_is_parsed(self):
        state, restarts = doctor._parse_service_show(
            "ActiveState=activating\nSubState=auto-restart\nNRestarts=9\n"
        )
        self.assertEqual(state, "activating/auto-restart")
        self.assertEqual(restarts, 9)

    def test_a_crash_loop_is_recognised(self):
        context = doctor.Context(root=ROOT, service_state="activating/auto-restart",
                                 service_restarts=4)
        self.assertTrue(doctor._service_is_crashing(context))

    def test_a_recovered_service_is_not_a_loop(self):
        context = doctor.Context(root=ROOT, service_state="active/running",
                                 service_restarts=4)
        self.assertFalse(doctor._service_is_crashing(context))

    def test_an_active_unit_wins_over_an_inactive_one(self):
        chosen = doctor._choose_service([
            ("system", "inactive/dead", 0),
            ("user", "active/running", 0),
        ])
        self.assertEqual(chosen, ("user", "active/running", 0))

    def test_a_crashing_unit_wins_over_an_inactive_one(self):
        chosen = doctor._choose_service([
            ("system", "inactive/dead", 0),
            ("user", "activating/auto-restart", 7),
        ])
        self.assertEqual(chosen[0], "user")
        self.assertTrue(doctor._looks_crashing(chosen[1], chosen[2]))

    def test_no_unit_is_reported_as_unknown(self):
        self.assertEqual(doctor._choose_service([]), (None, "unknown", 0))

    def test_the_service_is_not_started_while_something_still_fails(self):
        """Restarting the loop before Redis is fixed would just rebuild it."""
        stopped = doctor.Context(root=ROOT, service_stopped=True, failed_checks=1)
        self.assertFalse(doctor._should_start_after_repair(stopped))
        clean = doctor.Context(root=ROOT, service_stopped=True, failed_checks=0)
        self.assertTrue(doctor._should_start_after_repair(clean))
        running = doctor.Context(root=ROOT, service_stopped=False)
        self.assertFalse(doctor._should_start_after_repair(running))

    def test_the_advice_for_a_restart_loop_mentions_fix_all(self):
        text = doctor._redis_advice(
            state="running", output="", attempted=False, aidir_running=True, crashing=True
        )
        self.assertIn("restart loop", text)
        self.assertIn("--fix-all", text)


class RedisAdviceTests(unittest.TestCase):
    """What the doctor tells an operator when Redis does not answer."""

    def test_an_engine_failure_recommends_restarting_docker(self):
        text = doctor._redis_advice(
            state="running",
            output="driver failed programming external connectivity on endpoint aidir_redis",
            attempted=True,
            aidir_running=False,
        )
        self.assertIn("restart docker", text.lower())

    def test_a_crash_loop_points_at_the_log_lines(self):
        text = doctor._redis_advice(
            state="restarting", output="exit status 1", attempted=True, aidir_running=False
        )
        self.assertIn("does not stay up", text)
        self.assertIn("log", text)

    def test_a_running_service_is_told_to_stop_while_repairing(self):
        text = doctor._redis_advice(
            state="running (unhealthy)", output="", attempted=False, aidir_running=True
        )
        self.assertIn("stop aidir", text)
        self.assertIn("--fix-all", text)

    def test_docker_state_is_compact(self):
        # The formatter is exercised through the check's helper; an unknown
        # container must not raise.
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp))
            context = doctor.Context(root=root)
            self.assertEqual(doctor._docker_state(context, "does-not-exist"), "unknown")


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
