"""Diagnose aidir itself: configuration documents plus host state.

Two layers, one report:

* the configuration documents are handed to the aiset `doctor`, which reports
  every issue with a repair class;
* the host checks are what the library cannot see - the aiset checkout, `.env`,
  directories, the store lock, the virtual environment, Docker, Redis, nginx,
  ports, the Ollama upstream and the systemd unit.

Nothing here imports `core.config`: that module builds the process-wide
configuration at import and raises when a document is invalid, which is exactly
the case the doctor exists for. `core/doctor.py` imports the settings runtime
lazily and treats a failure as a finding, not as a crash.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

ROOT = Path(__file__).resolve().parent.parent

USAGE = """\
usage: core/app.py [--help] <command>

  (no command)   start aidir
  doctor         diagnose the configuration and this machine
                 doctor --help for the options

  --help, -h     this text
"""

DOCTOR_EPILOG = """\
modes:
  --config-only  only the configuration documents; safe while aidir is running
  --quick        the cheap subset; used for the one-line startup report

fix classes:
  clamp          move a number to its declared minimum or maximum
  coerce         "8080" to 8080 for an unambiguous literal (opt-in)
  drop_unknown   remove a key the descriptor does not declare (lossy, opt-in)

  --fix applies clamp only; --fix-all adds coerce and drop_unknown.
  The document is written only when the repairs make it fully valid.

exit codes:
  0  healthy, or warnings only
  1  failures remain
  2  repairs were applied, but issues remain

examples:
  ./doctor.sh
  ./doctor.sh --config-only
  ./doctor.sh --fix
  ./doctor.sh --fix-all --root
"""

# Fix classes for the host. `safe` runs with --fix; `visible` and `lossy` only
# with --fix-all; `privileged` only with --root.
SAFE = "safe"
VISIBLE = "visible"
LOSSY = "lossy"
PRIVILEGED = "privileged"

QUICK_CHECKS = ("aiset", "config", "env-file", "env-root", "dirs", "store-lock", "venv")
ADVISORY_CHECKS = ("config", "env-file", "env-root", "dirs", "store-lock", "venv")


@dataclass
class CheckResult:
    """Outcome of one check, with the fixes that would apply to it."""

    id: str
    status: str = "ok"                 # ok | warn | fail
    message: str = ""
    details: list[str] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    fixes: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "message": self.message,
            "details": self.details,
            "issues": self.issues,
            "fixes": self.fixes,
        }


@dataclass
class Context:
    """Everything a check needs: the project root and the requested modes."""

    root: Path
    fix: bool = False
    fix_all: bool = False
    privileges: bool = False
    dry_run: bool = False
    runtime: Any = None
    config_error: str | None = None
    aidir_running: bool | None = None

    @property
    def classes(self) -> list[str]:
        """Repair classes handed to the aiset doctor."""
        if self.fix_all:
            return ["clamp", "coerce", "drop_unknown"]
        if self.fix:
            return ["clamp"]
        return []


@dataclass
class Report:
    """The whole run: one result per check plus the exit code."""

    checks: list[CheckResult]
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    aidir_running: bool = False

    @property
    def exit_code(self) -> int:
        if not any(check.status == "fail" for check in self.checks):
            return 0
        return 2 if self.applied else 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "ok": self.exit_code == 0,
            "checks": [check.to_dict() for check in self.checks],
            "aidir_running": self.aidir_running,
            "applied": self.applied,
            "skipped": self.skipped,
            "exit_code": self.exit_code,
        }


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------
def usage() -> str:
    """Text for `core/app.py --help`; imports nothing heavy."""
    return USAGE


def main(argv: Sequence[str] | None = None) -> int:
    """`core/app.py doctor` entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="core/app.py doctor",
        description="diagnose the aidir configuration and this machine",
        epilog=DOCTOR_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=True,
    )
    parser.add_argument("--config-only", action="store_true",
                        help="diagnose the configuration documents only; touches nothing")
    parser.add_argument("--quick", action="store_true", help="cheap checks only, one line")
    parser.add_argument("--fix", action="store_true", help="apply the safe repairs")
    parser.add_argument("--fix-all", action="store_true",
                        help="also recreate containers and drop obsolete keys")
    parser.add_argument("--root", action="store_true",
                        help="allow privileged repairs through sudo")
    parser.add_argument("--only", metavar="IDS",
                        help="run only the listed checks, comma separated")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, apply nothing")
    args = parser.parse_args(list(argv) if argv is not None else None)

    only = [part.strip() for part in (args.only or "").split(",") if part.strip()]
    if args.config_only:
        only = ["aiset", "config"]
    elif args.quick:
        only = list(QUICK_CHECKS)
    report = run(
        root=ROOT,
        only=only or None,
        fix=args.fix or args.fix_all,
        fix_all=args.fix_all,
        privileges=args.root,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(_render(report))
    return report.exit_code


def startup_report(root: Path = ROOT) -> str:
    """One line for the service log; never raises and never blocks startup."""
    try:
        report = run(root=root, only=list(QUICK_CHECKS))
    except Exception as exc:  # pragma: no cover - the doctor must not break startup
        return f"doctor: unavailable ({exc.__class__.__name__}: {exc})"
    failures = [c for c in report.checks if c.status == "fail"]
    warnings = [c for c in report.checks if c.status == "warn"]
    if not failures and not warnings:
        return "doctor: ok"
    parts = []
    if failures:
        parts.append(f"{len(failures)} failure{'s' if len(failures) > 1 else ''}")
    if warnings:
        parts.append(f"{len(warnings)} warning{'s' if len(warnings) > 1 else ''}")
    return (f"doctor: {', '.join(parts)} - run "
            f"'venv/bin/python core/app.py doctor'")


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------
CHECKS: dict[str, Callable[[Context], CheckResult]] = {}


def _register(name: str) -> Callable[[Callable[[Context], CheckResult]], Callable[[Context], CheckResult]]:
    """Register one check under its id."""
    def wrap(function: Callable[[Context], CheckResult]) -> Callable[[Context], CheckResult]:
        CHECKS[name] = function
        return function
    return wrap


def run(
    *,
    root: Path = ROOT,
    only: Iterable[str] | None = None,
    fix: bool = False,
    fix_all: bool = False,
    privileges: bool = False,
    dry_run: bool = False,
) -> Report:
    """Run the selected checks in dependency order and collect their results."""
    context = Context(
        root=Path(root).resolve(),
        fix=fix,
        fix_all=fix_all,
        privileges=privileges,
        dry_run=dry_run,
    )
    selected = list(only) if only else list(CHECKS)
    order = list(CHECKS)
    unknown = [name for name in selected if name not in CHECKS]
    if unknown:
        raise SystemExit(f"aiset doctor: unknown check(s): {', '.join(unknown)}")

    results: list[CheckResult] = []
    report = Report(checks=results)
    report.aidir_running = _aidir_running(context)
    for name in order:
        if name not in selected:
            continue
        try:
            result = CHECKS[name](context)
        except Exception as exc:  # a broken check is a finding, not a crash
            result = CheckResult(name, "fail", f"{exc.__class__.__name__}: {exc}")
        results.append(result)
        for item in result.fixes:
            if item.get("applied"):
                report.applied.append(f"{name}: {item['action']}")
        if context.dry_run:
            report.skipped.extend(
                f"{name}: {item.get('document', '')} {item.get('path', '')} "
                f"{item['action']} ({item['class']})".replace("  ", " ")
                for item in result.fixes if not item.get("applied")
            )
    return report


def _render(report: Report) -> str:
    """One line per check, details indented, fixes and skips at the end."""
    lines: list[str] = []
    width = max((len(check.id) for check in report.checks), default=0)
    for check in report.checks:
        lines.append(f"{check.id:<{width}}  {check.status:<4}  {check.message}")
        for detail in check.details:
            lines.append(f"{'':<{width}}        {detail}")
    if report.applied:
        lines.append("")
        lines.append("applied:")
        lines.extend(f"  {item}" for item in report.applied)
    if report.skipped:
        lines.append("")
        lines.append("skipped:")
        lines.extend(f"  {item}" for item in report.skipped)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------
@_register("aiset")
def _check_aiset(context: Context) -> CheckResult:
    """The settings library must be importable from the submodule or the venv."""
    try:
        import importlib

        importlib.import_module("core.settings.runtime")
        aiset = importlib.import_module("aiset")
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        result = CheckResult("aiset", "fail", message.splitlines()[0],
                             details=[line for line in message.splitlines()[1:] if line.strip()])
        if (context.root / ".gitmodules").is_file() and shutil.which("git"):
            result.fixes.append({"action": "git submodule update --init --recursive",
                                 "class": SAFE})
            result.fixes[-1]["run"] = lambda: _run(["git", "-C", str(context.root),
                                                    "submodule", "update", "--init", "--recursive"])
            _apply_fix(context, result.fixes[-1])
            if result.fixes[-1].get("applied"):
                return _check_aiset(context) if not context.dry_run else result
        return result
    version = getattr(aiset, "__version__", "?")
    path = getattr(aiset, "__file__", "") or ""
    return CheckResult("aiset", "ok", f"library {version} from {path}")


@_register("config")
def _check_config(context: Context) -> CheckResult:
    """Run the aiset doctor over every configuration document."""
    try:
        from core.settings.runtime import CORE_PRINCIPAL, SettingsRuntime
    except Exception as exc:
        return CheckResult("config", "fail", str(exc).splitlines()[0])

    runtime = SettingsRuntime(root=context.root)
    try:
        runtime.build()
    except Exception as exc:
        context.config_error = str(exc)
    context.runtime = runtime

    details: list[str] = []
    issues: list[dict[str, Any]] = []
    fixes: list[dict[str, Any]] = []
    failed = False
    for document_id in sorted(runtime.service.documents):
        try:
            report = runtime.service.doctor(
                document_id,
                principal=CORE_PRINCIPAL,
                fix=False if context.dry_run else (context.classes or False),
            )
        except Exception as exc:
            failed = True
            details.append(f"{document_id}: {exc}")
            continue
        if not report.valid:
            failed = True
        for issue in report.issues:
            entry = {"document": document_id, **issue.to_dict()}
            issues.append(entry)
            if issue.severity != "error":
                continue
            suffix = f" -> {issue.fix}" if issue.fix else f" {issue.message}"
            if issue.fix and issue.after is not None:
                suffix += f" ({issue.before} => {issue.after})"
            details.append(f"{document_id} {issue.path or '/'} [{issue.rule}]{suffix}")
        for item in report.fixes:
            fixes.append({"document": document_id, "applied": report.applied,
                          **item.to_dict()})
        if context.dry_run and context.classes:
            # The library does not build a plan when `fix` is off, so the dry run
            # shows the plan from the annotated issues instead.
            for issue in report.issues:
                if issue.fix in context.classes:
                    fixes.append({
                        "document": document_id, "applied": False, "class": issue.fix,
                        "action": "drop" if issue.fix == "drop_unknown" else "set",
                        "path": issue.path, "rule": issue.rule,
                        "before": issue.before, "after": issue.after,
                    })
        for item in report.skipped:
            details.append(f"{document_id} skipped {item.path}: {item.reason}")
        if report.applied and _aidir_running(context):
            details.append(
                "aidir is running with the old configuration; reload it: "
                "systemctl reload aidir (SIGHUP), or restart it"
            )
    if context.config_error:
        # The build failed before the repair, so it is context, not a verdict:
        # the per-document reports below decide whether anything is still wrong.
        details.insert(0, context.config_error.splitlines()[0])

    if not runtime.service.documents:
        return CheckResult("config", "fail", "no configuration documents are registered",
                           details=details)
    errors = [item for item in issues if item["severity"] == "error"]
    if failed:
        status = "fail"
        message = f"{len(errors)} issue(s)"
        if fixes:
            message += f", {len(fixes)} repaired"
    elif fixes and context.dry_run:
        status = "ok"
        message = f"{len(fixes)} repair(s) planned"
    elif fixes:
        status = "ok"
        message = f"{len(fixes)} repair(s) applied; all documents valid"
    else:
        status = "ok"
        message = f"{len(runtime.service.documents)} document(s) valid"
    return CheckResult("config", status, message, details=details, issues=issues, fixes=fixes)


@_register("env-file")
def _check_env_file(context: Context) -> CheckResult:
    """`.env` must exist and parse. install.sh owns every write to it."""
    path = context.root / ".env"
    if not path.is_file():
        return CheckResult("env-file", "fail", ".env is missing",
                           details=["run ./install.sh; it owns .env"])
    try:
        values = _read_env(path)
    except OSError as exc:
        return CheckResult("env-file", "fail", f".env cannot be read: {exc.strerror}")
    return CheckResult("env-file", "ok", f"{len(values)} variable(s)")


@_register("env-root")
def _check_env_root(context: Context) -> CheckResult:
    """`AIDIR_ROOT` should point at this checkout; the doctor only reports it."""
    path = context.root / ".env"
    if not path.is_file():
        return CheckResult("env-root", "warn", "no .env to read AIDIR_ROOT from")
    value = _read_env(path).get("AIDIR_ROOT", "")
    actual = str(context.root)
    if not value:
        return CheckResult("env-root", "fail", "AIDIR_ROOT is empty",
                           details=[f"install.sh should set it to {actual}"])
    if Path(value).resolve() != context.root:
        return CheckResult("env-root", "warn", f"AIDIR_ROOT={value}, this checkout is {actual}",
                           details=["install.sh owns .env; re-run it to correct the value"])
    return CheckResult("env-root", "ok", actual)


@_register("dirs")
def _check_dirs(context: Context) -> CheckResult:
    """Directories aidir writes to must exist and be writable."""
    # The host writes logs; the redis volume is written by the container as root,
    # so only its existence matters here.
    writable = [context.root / "logs", context.root / "logs" / "audit"]
    existing = writable + [context.root / "docker" / "volumes" / "redis"]
    missing = [path for path in existing if not path.is_dir()]
    if missing and (context.fix or context.fix_all) and not context.dry_run:
        for path in missing:
            path.mkdir(parents=True, exist_ok=True)
        missing = [path for path in existing if not path.is_dir()]
    unwritable = [path for path in writable if path.is_dir() and not os.access(path, os.W_OK)]
    if missing or unwritable:
        details = [f"missing: {p.relative_to(context.root)}" for p in missing]
        details += [f"not writable: {p.relative_to(context.root)}" for p in unwritable]
        result = CheckResult("dirs", "fail", "directories are missing or not writable",
                             details=details)
        if missing:
            result.fixes.append({"action": "create the missing directories", "class": SAFE})
        return result
    return CheckResult("dirs", "ok", "present; logs writable")


@_register("store-lock")
def _check_store_lock(context: Context) -> CheckResult:
    """Report whether a store lock is held; a flock cannot go stale."""
    held: list[str] = []
    candidates = [context.root / "config.json5"]
    candidates += sorted(context.root.glob("workers/*/*/config.json5"))
    for config in candidates:
        lock = config.with_name(config.name + ".lock")
        if not lock.exists():
            continue
        try:
            import fcntl
            with open(lock, "a+") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    held.append(str(lock.relative_to(context.root)))
        except OSError:
            continue
    if held:
        return CheckResult("store-lock", "warn", "held by a running writer",
                           details=held + ["the kernel releases a flock when the holder exits"])
    return CheckResult("store-lock", "ok", "not held")


@_register("venv")
def _check_venv(context: Context) -> CheckResult:
    """The virtual environment must exist and carry the dependencies."""
    python = context.root / "venv" / "bin" / "python"
    pip = context.root / "venv" / "bin" / "pip"
    missing = [str(path.relative_to(context.root)) for path in (python, pip) if not path.exists()]
    if missing:
        return CheckResult("venv", "fail", "the virtual environment is incomplete",
                           details=missing + ["run ./install.sh"])
    try:
        import importlib

        for name in ("fastapi", "redis", "uvicorn"):
            importlib.import_module(name)
    except ImportError as exc:
        if _aidir_running(context):
            return CheckResult(
                "venv", "fail", f"dependency missing: {exc.name}",
                details=["aidir is running; stop it before reinstalling dependencies",
                         "then run ./install.sh, or pip install -r requirements.txt"],
            )
        result = CheckResult("venv", "fail", f"dependency missing: {exc.name}",
                             details=["run ./install.sh, or pip install -r requirements.txt"])
        result.fixes.append({"action": "pip install -r requirements.txt", "class": SAFE,
                             "run": lambda: _run([str(pip), "install", "-q", "-r",
                                                  str(context.root / "requirements.txt")],
                                                 timeout=600)})
        _apply_fix(context, result.fixes[-1])
        if result.fixes[-1].get("applied") and not context.dry_run:
            return CheckResult("venv", "ok", "dependencies installed")
        return result
    return CheckResult("venv", "ok", "ready")


@_register("docker")
def _check_docker(context: Context) -> CheckResult:
    """The Docker daemon must answer."""
    code, _out = _run(["docker", "info"], timeout=10)
    if code == 0:
        return CheckResult("docker", "ok", "daemon reachable")
    result = CheckResult("docker", "fail", "the Docker daemon is not reachable",
                         details=["start it: sudo systemctl start docker"])
    if context.privileges:
        result.fixes.append({"action": "systemctl start docker", "class": PRIVILEGED})
        _apply_fix(context, result.fixes[-1],
                   command=["systemctl", "start", "docker"])
        if result.fixes[-1].get("applied") and not context.dry_run:
            code, _ = _run(["docker", "info"], timeout=10)
            if code == 0:
                return CheckResult("docker", "ok", "daemon started")
    return result


@_register("compose")
def _check_compose(context: Context) -> CheckResult:
    """The compose file must parse with the current .env."""
    code, out = _compose(context, ["config", "-q"], timeout=20)
    if code == 0:
        return CheckResult("compose", "ok", "compose file parses")
    return CheckResult("compose", "fail", "the compose file does not parse",
                       details=[line for line in out.splitlines() if line.strip()][-3:])


@_register("redis")
def _check_redis(context: Context) -> CheckResult:
    """Redis must answer on the port the configuration names, and publish it."""
    host, port, password = _redis_settings(context)
    reachable, error = _redis_ping(host, port, password)
    published = _compose_published_port(context, "redis", 6379)
    details: list[str] = []
    if published is not None and published != port:
        details.append(f"the container publishes {published}, the configuration says {port}")
    if reachable:
        return CheckResult("redis", "ok", f"{host}:{port} answers",
                           details=details) if not details else CheckResult(
            "redis", "warn", f"{host}:{port} answers, published port differs",
            details=details)
    details.insert(0, error or "no response")
    if _aidir_running(context):
        details.append("aidir is running; it will reconnect after the container is back")
    result = CheckResult("redis", "fail", f"cannot reach {host}:{port}", details=details)
    action = "docker compose up -d redis"
    if published is not None and published != port:
        action = "docker compose up -d --force-recreate redis"
    allowed = context.fix_all or (context.fix and published in (None, port))
    if allowed:
        result.fixes.append({"action": action, "class": VISIBLE if "force" in action else SAFE})
        _apply_fix(context, result.fixes[-1],
                   command=_compose_command(context, action.split()[2:]))
        if result.fixes[-1].get("applied") and not context.dry_run:
            reachable, error = _redis_ping(host, port, password)
            if reachable:
                return CheckResult("redis", "ok", f"{host}:{port} answers after the repair")
    return result


@_register("nginx")
def _check_nginx(context: Context) -> CheckResult:
    """The nginx container must be running."""
    code, out = _compose(context, ["ps", "-q", "nginx"], timeout=15)
    container = out.strip()
    if code != 0 or not container:
        result = CheckResult("nginx", "fail", "the nginx container is not running",
                             details=["start it: docker compose up -d nginx"])
        if context.fix or context.fix_all:
            result.fixes.append({"action": "docker compose up -d nginx", "class": SAFE})
            _apply_fix(context, result.fixes[-1],
                       command=_compose_command(context, ["up", "-d", "nginx"]))
            if result.fixes[-1].get("applied") and not context.dry_run:
                return CheckResult("nginx", "ok", "container started")
        return result
    code, running = _run(["docker", "inspect", "-f", "{{.State.Running}}", container], timeout=10)
    if code == 0 and running.strip() == "true":
        return CheckResult("nginx", "ok", f"running ({container[:12]})")
    return CheckResult("nginx", "fail", "the nginx container is not running")


@_register("ports")
def _check_ports(context: Context) -> CheckResult:
    """A configured port must be free, or held by aidir or nginx themselves."""
    settings = _resolved(context)
    env = _env(context)
    wanted = [
        ("webui", _str(settings, "webui.host", None) or env.get("WEBUI_HOST") or "0.0.0.0",
         _int(settings, "webui.port", 20082)),
        ("openaix", _endpoint_bind(settings, "openaix", "0.0.0.0"),
         _endpoint_port(settings, "openaix", 21434)),
        ("mcp", _endpoint_bind(settings, "mcp", "0.0.0.0"),
         _endpoint_port(settings, "mcp", 20001)),
        ("nginx", "0.0.0.0", _int_env(context, "NGINX_HTTP_PORT", 8080)),
    ]
    busy: list[str] = []
    for name, host, port in wanted:
        if port is None:
            continue
        if _port_free(host, port):
            continue
        busy.append(f"{name} {host}:{port}")
    if not busy:
        return CheckResult("ports", "ok", "configured ports are free or owned")
    running = _aidir_running(context)
    if running:
        return CheckResult("ports", "ok", "ports are owned by the running instance")
    return CheckResult("ports", "warn", "ports are in use",
                       details=[f"{item} is in use" for item in busy])


@_register("logs")
def _check_logs(context: Context) -> CheckResult:
    """The log directory must accept a write."""
    directory = context.root / "logs"
    if not directory.is_dir():
        if (context.fix or context.fix_all) and not context.dry_run:
            directory.mkdir(parents=True, exist_ok=True)
        else:
            return CheckResult("logs", "fail", "logs/ is missing")
    probe = directory / f".doctor-write-test-{os.getpid()}"
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        return CheckResult("logs", "fail", f"logs/ is not writable: {exc.strerror}")
    return CheckResult("logs", "ok", "writable")


@_register("ollama")
def _check_ollama(context: Context) -> CheckResult:
    """The Ollama upstream must answer; the doctor never starts it."""
    settings = _resolved(context)
    base = _str(settings, "ollama.baseUrl", None)
    if not base:
        env = _env(context)
        base = env.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/api/tags", timeout=3):
            return CheckResult("ollama", "ok", base)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return CheckResult("ollama", "warn", f"upstream does not answer: {base}",
                           details=[str(exc)])


@_register("systemd")
def _check_systemd(context: Context) -> CheckResult:
    """Report the service unit; restarting it is privileged."""
    seen: list[str] = []
    for scope, command in (("system", ["systemctl", "is-active", "aidir"]),
                           ("user", ["systemctl", "--user", "is-active", "aidir"])):
        code, out = _run(command, timeout=10)
        state = out.strip()
        if state not in ("active", "inactive", "failed", "activating"):
            continue
        seen.append(f"{scope}: {state}")
        if state == "active":
            return CheckResult("systemd", "ok", f"{scope} unit active")
    if not seen:
        return CheckResult("systemd", "warn", "no aidir unit found (run ./install.sh)")
    result = CheckResult("systemd", "warn", "the unit is not active", details=seen)
    if context.privileges:
        scope = seen[0].split(":", 1)[0]
        result.fixes.append({
            "action": f"systemctl {'--user ' if scope == 'user' else ''}restart aidir",
            "class": PRIVILEGED,
        })
        _apply_fix(context, result.fixes[-1],
                   command=["systemctl"] + (["--user"] if scope == "user" else [])
                   + ["restart", "aidir"])
        if result.fixes[-1].get("applied") and not context.dry_run:
            return CheckResult("systemd", "ok", f"{scope} unit restarted")
    return result


# --------------------------------------------------------------------------
# host helpers
# --------------------------------------------------------------------------
def _apply_fix(context: Context, fix: dict[str, Any], *, command: list[str] | None = None) -> None:
    """Run one fixer when the mode allows it; record whether it was applied."""
    if context.dry_run:
        return
    if fix["class"] == SAFE and not (context.fix or context.fix_all):
        return
    if fix["class"] in (VISIBLE, LOSSY) and not context.fix_all:
        return
    if fix["class"] == PRIVILEGED and not context.privileges:
        return
    if fix["class"] == PRIVILEGED and command is not None:
        fix["applied"] = _sudo(command)
        return
    runner = fix.get("run")
    if runner is not None:
        fix["applied"] = bool(runner() == 0)
        return
    if command is not None:
        code, _out = _run(command, timeout=120)
        fix["applied"] = code == 0


def _sudo(command: list[str]) -> bool:
    """Run one privileged command: direct as root, otherwise one sudo prompt."""
    if os.geteuid() == 0:
        code, _ = _run(command, timeout=120)
        return code == 0
    if not sys.stdin.isatty():
        return False
    code, _ = _run(["sudo", "-n", *command], timeout=120)
    if code == 0:
        return True
    _run(["sudo", "-v"], timeout=120)
    code, _ = _run(["sudo", "-n", *command], timeout=120)
    return code == 0


def _run(command: Sequence[str], *, timeout: int = 30) -> tuple[int, str]:
    """Run a command, returning (exit code, combined output); never raises."""
    try:
        completed = subprocess.run(
            list(command), capture_output=True, text=True, timeout=timeout, cwd=str(ROOT)
        )
        return completed.returncode, (completed.stdout or "") + (completed.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def _compose_command(context: Context, args: Sequence[str]) -> list[str]:
    """The compose invocation for this checkout."""
    return ["docker", "compose", "-f", str(context.root / "docker-compose.yml"), *args]


def _compose(context: Context, args: Sequence[str], *, timeout: int = 30) -> tuple[int, str]:
    """Run one compose subcommand."""
    return _run(_compose_command(context, args), timeout=timeout)


def _compose_published_port(context: Context, service: str, container_port: int) -> int | None:
    """Host port a compose service publishes for one container port, if any."""
    code, out = _compose(context, ["port", service, str(container_port)], timeout=15)
    if code != 0 or not out.strip():
        return None
    tail = out.strip().splitlines()[-1]
    return _port_from_address(tail)


def _port_from_address(value: str) -> int | None:
    """Port from `host:port`, `[::]:port` or `0.0.0.0:port`."""
    _, _, port = value.rpartition(":")
    return int(port) if port.isdigit() else None


def _redis_settings(context: Context) -> tuple[str, int, str]:
    """Redis host, port and password from the resolved config or .env."""
    settings = _resolved(context)
    host = _str(settings, "redis.host", None) or _env(context).get("REDIS_HOST", "127.0.0.1")
    port = _int(settings, "redis.port", None) or _int_env(context, "REDIS_PORT", 6379)
    password = _str(settings, "redis.password", None)
    if password is None:
        password = _env(context).get("REDIS_PASSWORD", "")
    return host, port, password


def _redis_ping(host: str, port: int, password: str) -> tuple[bool, str | None]:
    """One short PING; returns (reachable, error)."""
    try:
        import redis

        client = redis.Redis(
            host=host, port=port, password=password or None,
            socket_connect_timeout=2, socket_timeout=2,
        )
        client.ping()
        return True, None
    except Exception as exc:
        return False, f"{exc.__class__.__name__}: {exc}"


def _resolved(context: Context) -> dict[str, Any]:
    """Effective main configuration, or an empty mapping when it does not load."""
    if context.runtime is None:
        try:
            from core.settings.runtime import SettingsRuntime

            runtime = SettingsRuntime(root=context.root)
            try:
                runtime.build()
            except Exception:
                pass
            context.runtime = runtime
        except Exception:
            return {}
    try:
        return context.runtime.effective("main") or {}
    except Exception:
        return {}


def _env(context: Context) -> dict[str, str]:
    """Parsed `.env`, or an empty mapping."""
    try:
        return _read_env(context.root / ".env")
    except OSError:
        return {}


def _read_env(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE pairs without importing the settings subsystem."""
    values: dict[str, str] = {}
    if not path.is_file():
        raise FileNotFoundError(path)
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, separator, value = line.partition("=")
        if not separator:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _int_env(context: Context, key: str, default: int) -> int:
    """Integer from `.env`, falling back to the default."""
    try:
        return int(_env(context).get(key, "") or default)
    except ValueError:
        return default


def _walk(settings: Any, pointer: str) -> Any:
    """Value at a dotted pointer, or None."""
    current = settings
    for token in pointer.split("."):
        if not isinstance(current, dict) or token not in current:
            return None
        current = current[token]
    return current


def _int(settings: Any, pointer: str, default: int | None) -> int | None:
    """Integer at a dotted pointer."""
    value = _walk(settings, pointer)
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _str(settings: Any, pointer: str, default: str | None) -> str | None:
    """String at a dotted pointer."""
    value = _walk(settings, pointer)
    return str(value) if value is not None else default


def _endpoint_bind(settings: Any, endpoint_id: str, default: str) -> str:
    """Bind address of one `endpoints[]` entry by id."""
    for entry in settings.get("endpoints") or []:
        if isinstance(entry, dict) and entry.get("id") == endpoint_id:
            return str(entry.get("bindAddress") or default)
    return default


def _endpoint_port(settings: Any, endpoint_id: str, default: int) -> int:
    """Port of one `endpoints[]` entry by id."""
    for entry in settings.get("endpoints") or []:
        if isinstance(entry, dict) and entry.get("id") == endpoint_id:
            try:
                return int(entry.get("port") or default)
            except (TypeError, ValueError):
                return default
    return default


def _port_free(host: str, port: int) -> bool:
    """Whether the address can still be bound."""
    for family, socktype, protocol, _, address in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    ):
        try:
            with socket.socket(family, socktype, protocol) as probe:
                probe.bind(address)
        except OSError:
            return False
    return True


def _aidir_running(context: Context, proc_root: Path = Path("/proc")) -> bool:
    """Whether a long-running aidir core process owns the ports.

    `pgrep -f core/app.py` is wrong here: the doctor's own command line is
    `core/app.py doctor`, so it would always match itself and claim the ports are
    "owned by the running instance". The process table is read directly, the
    doctor's own pid is skipped, and so is every other diagnostic invocation.
    """
    if context.aidir_running is not None:
        return context.aidir_running
    own = os.getpid()
    running = False
    try:
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == own:
                continue
            try:
                raw = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            args = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
            if not any(arg.endswith("core/app.py") for arg in args):
                continue
            if "doctor" in args:
                continue
            running = True
            break
    except OSError:  # pragma: no cover - /proc is Linux
        code, out = _run(["pgrep", "-f", "core/app.py"], timeout=5)
        running = code == 0 and bool(out.strip())
    context.aidir_running = running
    return running


# Keep the advisory list exported for callers that want the startup subset.
__all__ = ["main", "run", "startup_report", "usage", "USAGE", "DOCTOR_EPILOG", "Report"]
