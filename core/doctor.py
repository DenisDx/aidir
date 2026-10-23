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
import re
import shutil
import socket
import subprocess
import sys
import time
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

the report ends with one verdict line:
  RESULT: all checks passed                  (green)
  RESULT: all checks passed, N warning(s)    (amber)
  RESULT: N problem(s) remain                (red)
Colour is used only on a terminal; --no-color or NO_COLOR=1 disables it.

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
    recommendation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "id": self.id,
            "status": self.status,
            "message": self.message,
            "details": self.details,
            "issues": self.issues,
            "fixes": self.fixes,
        }
        if self.recommendation:
            out["recommendation"] = self.recommendation
        return out


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
    service_scope: str | None = None
    service_state: str = "unknown"
    service_restarts: int = 0
    service_stopped: bool = False
    failed_checks: int = 0

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
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    aidir_running: bool = False
    service: dict[str, Any] = field(default_factory=dict)

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
            "service": self.service,
            "applied": self.applied,
            "failed": self.failed,
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
    parser.add_argument("--no-color", action="store_true",
                        help="never colour the report (also honours NO_COLOR)")
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
        color = (not args.no_color and not os.environ.get("NO_COLOR")
                 and sys.stdout.isatty())
        print(_render(report, color=color))
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
    context.service_scope, context.service_state, context.service_restarts = _service_state(context)
    report.service = {
        "scope": context.service_scope,
        "state": context.service_state,
        "restarts": context.service_restarts,
    }
    report.aidir_running = _aidir_running(context)
    if context.fix_all and _service_is_crashing(context):
        if _service_stop(context):
            context.service_stopped = True
            report.applied.append(
                f"service: stop the restart loop ({context.service_scope} unit)"
            )
        else:
            report.failed.append(
                "service: stop the restart loop -> refused; "
                + ("re-run with --root" if context.service_scope == "system"
                   else "systemctl stop failed")
            )
    for name in order:
        if name not in selected:
            continue
        try:
            result = CHECKS[name](context)
        except Exception as exc:  # a broken check is a finding, not a crash
            result = CheckResult(name, "fail", f"{exc.__class__.__name__}: {exc}")
        results.append(result)
        if result.status == "fail":
            context.failed_checks += 1
        for item in result.fixes:
            where = " ".join(
                part for part in (item.get("document"), item.get("path")) if part
            )
            label = f"{name}: {where + ' ' if where else ''}{item['action']}"
            if item.get("applied"):
                report.applied.append(label)
            elif item.get("attempted"):
                report.failed.append(
                    f"{label} -> {item.get('error', 'failed')}"
                )
            else:
                report.skipped.append(f"{label} ({item['class']})")
    return report


GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BOLD = "\033[1m"
RESET = "\033[0m"

_STATUS_COLOUR = {"ok": GREEN, "warn": YELLOW, "fail": RED}


def _paint(text: str, colour: str, enabled: bool) -> str:
    """Wrap text in one ANSI colour when the output is a terminal."""
    return f"{colour}{text}{RESET}" if enabled else text


def _render(report: Report, *, color: bool = False) -> str:
    """Checks, repairs, recommendations, then one explicit verdict."""
    lines: list[str] = []
    width = max((len(check.id) for check in report.checks), default=0)
    for check in report.checks:
        status = _paint(f"{check.status:<4}", _STATUS_COLOUR.get(check.status, ""), color)
        lines.append(f"{check.id:<{width}}  {status}  {check.message}")
        for detail in check.details:
            lines.append(f"{'':<{width}}        {detail}")
    if report.applied:
        lines += ["", "applied:"]
        lines += [f"  {item}" for item in report.applied]
    if report.failed:
        lines += ["", _paint("failed repairs:", RED, color)]
        lines += [f"  {item}" for item in report.failed]
    if report.skipped:
        lines += ["", "skipped:"]
        lines += [f"  {item}" for item in report.skipped]

    advice = [c for c in report.checks if c.recommendation and c.status != "ok"]
    if advice:
        lines += ["", "recommendations:"]
        lines += [f"  {check.id}: {check.recommendation}" for check in advice]

    failures = [c for c in report.checks if c.status == "fail"]
    warnings = [c for c in report.checks if c.status == "warn"]
    if failures:
        verdict = (f"RESULT: {len(failures)} problem(s) remain"
                   + (f", {len(warnings)} warning(s)" if warnings else ""))
        colour = RED
    elif warnings:
        verdict = f"RESULT: all checks passed, {len(warnings)} warning(s)"
        colour = YELLOW
    else:
        verdict = "RESULT: all checks passed"
        colour = GREEN
    lines += ["", _paint(verdict, BOLD + colour, color)]
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
        result.recommendation = "git submodule update --init --recursive"
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

    runtime = SettingsRuntime(root=context.root, environ={})
    try:
        runtime.build(refresh=False)
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
            if issue.severity != "error" or entry.get("resolved"):
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
    if not runtime.service.documents:
        result = CheckResult("config", "fail",
                             "no configuration documents are registered", details=details)
        result.recommendation = "run ./install.sh; it creates config.json5 and .env"
        return result
    if not failed:
        # Populate the effective cache for the checks that read settings, now
        # that every document is valid (after a repair, if one was applied).
        try:
            runtime.refresh()
        except Exception:
            pass
    errors = [item for item in issues if item["severity"] == "error"]
    if failed and context.config_error:
        # Only a still-broken document needs the pre-repair build error.
        details.insert(0, context.config_error.splitlines()[0])
    if failed:
        status = "fail"
        message = f"{len(errors)} issue(s)"
        if fixes:
            message += f", {len(fixes)} repaired"
        recommendation = ("--fix clamps a range violation; --fix-all also drops unknown keys; "
                          "values that need a decision must be edited by hand")
        result = CheckResult("config", status, message, details=details,
                             issues=issues, fixes=fixes)
        result.recommendation = recommendation
        return result
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
        result = CheckResult("env-file", "fail", ".env is missing",
                             details=["install.sh owns .env"])
        result.recommendation = "run ./install.sh"
        return result
    try:
        values = _read_env(path)
    except OSError as exc:
        result = CheckResult("env-file", "fail", f".env cannot be read: {exc.strerror}")
        result.recommendation = "check the file permissions, or re-create it with ./install.sh"
        return result
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
        result = CheckResult("env-root", "fail", "AIDIR_ROOT is empty",
                             details=[f"install.sh should set it to {actual}"])
        result.recommendation = "run ./install.sh"
        return result
    if Path(value).resolve() != context.root:
        result = CheckResult("env-root", "warn",
                             f"AIDIR_ROOT={value}, this checkout is {actual}",
                             details=["install.sh owns .env"])
        result.recommendation = "run ./install.sh to write the correct AIDIR_ROOT"
        return result
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
        if unwritable:
            result.recommendation = ("make them writable: chmod u+w logs logs/audit")
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
        result = CheckResult("store-lock", "warn", "held by a running writer",
                             details=held + ["the kernel releases a flock when the holder exits"])
        result.recommendation = ("another process is writing; wait, or check for a stuck aidir "
                                 "process before removing anything")
        return result
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
            result = CheckResult(
                "venv", "fail", f"dependency missing: {exc.name}",
                details=["aidir is running; stop it before reinstalling dependencies"],
            )
            result.recommendation = ("stop aidir, then run ./install.sh or "
                                     "venv/bin/pip install -r requirements.txt")
            return result
        result = CheckResult("venv", "fail", f"dependency missing: {exc.name}",
                             details=["install it from requirements.txt"])
        result.recommendation = "run ./install.sh, or venv/bin/pip install -r requirements.txt"
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
    result = CheckResult("docker", "fail", "the Docker daemon is not reachable")
    result.recommendation = ("start it: sudo systemctl start docker, or start Docker Desktop; "
                             "on WSL, 'wsl --shutdown' resets a wedged engine")
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
    result = CheckResult("compose", "fail", "the compose file does not parse",
                         details=[line for line in out.splitlines() if line.strip()][-3:])
    result.recommendation = ("check the values .env supplies to docker-compose.yml, "
                             "then run: docker compose config")
    return result


@_register("redis")
def _check_redis(context: Context) -> CheckResult:
    """Redis on the host, in the container, and the mapping between them.

    Four independent things can be wrong, and they need different fixes:

    | Observation | Cause | Fix |
    |---|---|---|
    | connection refused, no container | the container is not running | start it |
    | timeout, container running, Redis answers inside | the host port mapping is broken (WSL/Docker proxy) | restart Docker |
    | timeout, Redis does not answer inside | the process is down or crash-looping | read the log, often volume permissions |
    | the bind fails with `address already in use` | a host process owns the port | free it or move the port |

    The check answers which one it is, and repairs the first three when the mode
    allows it.
    """
    host, port, password = _redis_settings(context)
    reachable, error = _redis_ping(host, port, password)
    published = _compose_published_port(context, "redis", 6379)
    container = _compose_container(context, "redis")
    details: list[str] = []
    if published is not None and published != port:
        details.append(f"the container publishes {published}, the configuration says {port}")
    elif published is None:
        details.append("the container's published port is unknown (docker compose port failed)")
    if reachable:
        if not details or published == port:
            return CheckResult("redis", "ok", f"{host}:{port} answers")
        result = CheckResult("redis", "warn",
                             f"{host}:{port} answers, published port differs", details=details)
        result.recommendation = (
            "recreate the container so it publishes the configured port: "
            "docker compose up -d --force-recreate redis"
        )
        return result

    details.insert(0, error or "no response")
    state = _docker_state(context, container) if container else "no container"
    inside, _inside_output = _container_redis_ping(context) if container else ("no container", "")
    holder = None if _port_free(host, port) else _port_listener(port)
    details.append(f"container: {state}")
    if container:
        details.append(f"redis inside the container: {inside}")
    if holder:
        details.append(f"host port {port} is already held by {holder}")
    if _service_is_crashing(context):
        details.append(
            f"aidir is in a restart loop ({context.service_state}, "
            f"{context.service_restarts} restart(s)) until Redis answers"
        )
    elif _aidir_running(context):
        details.append("aidir is running; it will reconnect after Redis is back")
    result = CheckResult("redis", "fail", f"cannot reach {host}:{port}", details=details)

    docker_holder = bool(holder) and any(
        word in holder.lower() for word in ("docker-proxy", "dockerd", "containerd")
    )
    foreign_holder = bool(holder) and not docker_holder
    logs = _container_log_lines(context, "redis") if container and not context.dry_run else []
    permission_denied = any("permission denied" in line.lower() for line in logs)
    listening_inside = inside.startswith(("pong", "auth-required"))

    if context.fix_all and foreign_holder:
        # Recreating cannot succeed and would destroy a possibly usable container.
        result.details.append("repair skipped: the host port must be freed first")
    elif context.fix_all and permission_denied and not listening_inside:
        volume = context.root / "docker" / "volumes" / "redis"
        chown = {
            "action": f"chown -R 999:999 {volume.relative_to(context.root)}",
            "class": PRIVILEGED,
        }
        result.fixes.append(chown)
        _apply_fix(context, chown, command=["chown", "-R", "999:999", str(volume)])
        if chown.get("applied"):
            result.fixes.append({"action": "recreate redis", "class": VISIBLE})
            _apply_fix(context, result.fixes[-1],
                       command=_compose_command(context, ["up", "-d", "--force-recreate", "redis"]))
    elif context.fix_all:
        result.fixes.append({"action": "docker compose up -d --force-recreate redis",
                             "class": VISIBLE})
        _apply_fix(context, result.fixes[-1],
                   command=_compose_command(context, ["up", "-d", "--force-recreate", "redis"]))
    elif context.fix and not container:
        result.fixes.append({"action": "docker compose up -d redis", "class": SAFE})
        _apply_fix(context, result.fixes[-1],
                   command=_compose_command(context, ["up", "-d", "redis"]))

    if any(fix.get("applied") for fix in result.fixes) and not context.dry_run:
        reachable, error = _wait_redis(host, port, password)

    if (not reachable and listening_inside and not foreign_holder
            and context.fix_all and context.privileges and not context.dry_run):
        # Redis is healthy inside the container, so only the host mapping is left.
        result.fixes.append({"action": "restart Docker (host port mapping is broken)",
                             "class": PRIVILEGED})
        _apply_fix(context, result.fixes[-1], command=["systemctl", "restart", "docker"])
        if result.fixes[-1].get("applied"):
            reachable, error = _wait_redis(host, port, password)

    if reachable:
        return CheckResult("redis", "ok", f"{host}:{port} answers after the repair")

    # Re-read the state and the log after the attempts: a crash loop usually
    # explains itself in the last lines.
    state = _docker_state(context, container) if container else state
    if container and not context.dry_run:
        logs = _container_log_lines(context, "redis")
        for line in logs:
            result.details.append(f"log: {line}")
    attempted = bool(result.fixes and result.fixes[-1].get("attempted"))
    output = str(result.fixes[-1].get("error", "")) if result.fixes else ""
    result.recommendation = _redis_advice(
        state=state,
        output=output,
        attempted=attempted,
        aidir_running=_aidir_running(context),
        crashing=_service_is_crashing(context),
        port=port,
        holder=holder,
        inside=inside,
        permission_denied=any("permission denied" in line.lower() for line in logs),
        foreign_holder=foreign_holder,
    )
    return result


def _redis_advice(*, state: str, output: str, attempted: bool, aidir_running: bool,
                  crashing: bool = False, port: int | None = None,
                  holder: str | None = None, inside: str | None = None,
                  permission_denied: bool = False,
                  foreign_holder: bool = False) -> str:
    """The one or two actions worth taking for an unreachable Redis."""
    parts: list[str] = []
    if crashing:
        parts.append(
            "aidir is in a restart loop; --fix-all stops it before repairing and "
            "starts it again afterwards"
        )
    elif aidir_running:
        parts.append(
            "stop aidir while repairing (systemctl --user stop aidir), then start it again"
        )

    if permission_denied:
        parts.append(
            "the volume is not writable by Redis: run "
            "'sudo chown -R 999:999 docker/volumes/redis', then ./doctor.sh --fix-all"
        )
        return "; ".join(parts)
    if foreign_holder:
        parts.append(
            f"host port {port} is held by {holder}: stop that process, or change "
            "REDIS_PORT through ./install.sh"
        )
        return "; ".join(parts)

    lowered = output.lower()
    already_bound = ("address already in use" in lowered
                     or "port is already allocated" in lowered)
    if already_bound:
        if holder:
            docker_holder = any(
                word in holder.lower() for word in ("docker-proxy", "dockerd", "containerd")
            )
            if docker_holder:
                parts.append(
                    f"host port {port} is held by a Docker proxy ({holder}): check "
                    f"'docker ps -a --filter publish={port}' for the container that owns it; "
                    "if there is none, restart Docker (on WSL also 'wsl --shutdown')"
                )
            else:
                parts.append(
                    f"host port {port} is held by {holder}: stop that process, or change "
                    "REDIS_PORT through ./install.sh"
                )
        else:
            parts.append(
                f"host port {port} is already in use: find the owner with "
                f"'ss -ltnp | grep :{port}', then stop it or change REDIS_PORT "
                "through ./install.sh"
            )
        return "; ".join(parts)

    if inside and inside.startswith(("pong", "auth-required")):
        parts.append(
            "Redis answers inside the container but not on the host: the port "
            "mapping is broken. Restart Docker: sudo systemctl restart docker; on WSL "
            "also run 'wsl --shutdown' from Windows"
        )
        return "; ".join(parts)

    engine_failure = any(word in lowered for word in ("connectivity", "iptables", "proxy"))
    if attempted and engine_failure:
        parts.append(
            "Docker could not program the port: sudo systemctl restart docker; on WSL also "
            "run 'wsl --shutdown' from Windows, then start Docker again"
        )
    elif state.startswith(("restarting", "exited", "dead")):
        parts.append(
            "the container does not stay up: fix the cause shown in its log lines "
            "(volume permissions and port conflicts are the usual ones)"
        )
    elif attempted:
        parts.append("still unreachable after the repair: read its log lines above")
    elif not attempted:
        parts.append("run ./doctor.sh --fix-all to recreate the container")
    return "; ".join(parts)


@_register("namespaces")
def _check_namespaces(context: Context) -> CheckResult:
    """Redis must not hold the data of an instance that is no longer configured.

    Two instances on one machine keep their own namespace, so the data of an
    old instance is harmless but confusing: it occupies space, it shows up in
    the statistics, and the instance id in the URLs does not describe it. The
    check only reports; removing another instance's data is the operator's
    decision.
    """
    settings = _resolved(context)
    instance = str((settings.get("instance") or "aidir") or "aidir")
    host = _str(settings, "redis.host", None) or _env(context).get("REDIS_HOST", "127.0.0.1")
    port = _int(settings, "redis.port", None) or _int_env(context, "REDIS_PORT", 6379)
    password = _str(settings, "redis.password", None) or _env(context).get("REDIS_PASSWORD", "")

    try:
        import redis

        client = redis.Redis(
            host=host, port=port, password=password or None,
            socket_connect_timeout=2, socket_timeout=2,
        )
        client.ping()
        keys = list(client.scan_iter(match="*:task:*", count=500))
    except Exception as exc:
        return CheckResult(
            "namespaces", "warn", f"cannot read the namespaces: {exc.__class__.__name__}"
        )

    counts: dict[str, int] = {}
    for key in keys:
        name = key.decode("utf-8", "replace") if isinstance(key, bytes) else str(key)
        name = name.split(":", 1)[0]
        if not name or name == instance:
            continue
        counts[name] = counts.get(name, 0) + 1

    if not counts:
        return CheckResult("namespaces", "ok", f"only the namespace '{instance}' holds task data")

    other = ", ".join(f"{name} ({count} task(s))" for name, count in sorted(counts.items()))
    result = CheckResult(
        "namespaces",
        "warn",
        f"Redis also holds the task data of another instance: {other}",
    )
    result.details.append(
        f"The configured instance is '{instance}'. The data above belongs to an "
        "instance that is not configured here; it can be removed with "
        f"'redis-cli --scan --pattern \"<name>:task:*\"' once you are sure it is not needed."
    )
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
    busy: list[tuple[str, str, int]] = []
    for name, host, port in wanted:
        if port is None:
            continue
        if _port_free(host, port):
            continue
        busy.append((name, host, port))
    if not busy:
        return CheckResult("ports", "ok", "configured ports are free or owned")

    # The nginx container legitimately holds its public port whenever it runs;
    # reporting that as a conflict would make every healthy run yellow.
    nginx_running = bool(_compose_container(context, "nginx"))
    unexpected = [
        (name, host, port) for name, host, port in busy
        if not (name == "nginx" and nginx_running)
    ]
    if not unexpected:
        return CheckResult("ports", "ok", "ports are owned by nginx and this instance")
    if _aidir_running(context) and not any(
        name in ("nginx",) for name, _host, _port in unexpected
    ):
        return CheckResult("ports", "ok", "ports are owned by the running instance")
    return CheckResult("ports", "warn", "ports are in use",
                       details=[f"{name} {host}:{port} is in use"
                                for name, host, port in unexpected])


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
        result = CheckResult("logs", "fail", f"logs/ is not writable: {exc.strerror}")
        result.recommendation = "chmod u+w logs logs/audit"
        return result
    return CheckResult("logs", "ok", "writable")


@_register("auth")
def _check_auth(context: Context) -> CheckResult:
    """The surfaces that answer without a token must be the ones that are meant to.

    The MCP tool endpoint can spend money and reach the network, the queue depth
    describes the capacity of the machine, and the peer surfaces publish the
    state of this instance. The check reports the ones that are open, and the
    providers that cannot read a peer because the peer publishes its state only
    with a token.
    """
    settings = _resolved(context)
    endpoints = settings.get("endpoints") or []
    findings: list[str] = []
    for endpoint in endpoints if isinstance(endpoints, list) else []:
        if not isinstance(endpoint, dict):
            continue
        endpoint_id = str(endpoint.get("id") or "")
        api = str(endpoint.get("api") or "")
        requires_auth = bool(endpoint.get("require_auth"))
        if requires_auth:
            continue
        if api == "mcp":
            findings.append(
                f"the MCP endpoint '{endpoint_id}' publishes its tools without a token; "
                "set endpoints[].require_auth to true and add a token to users.items"
            )
        elif api in {"openaix", "ollama"}:
            findings.append(
                f"the endpoint '{endpoint_id}' publishes its queue depth and its model list "
                "without a token; set endpoints[].require_auth to true on a machine that "
                "is reachable from outside"
            )

    providers = ((settings.get("models") or {}).get("providers") or {})
    for provider_id, provider in (providers.items() if isinstance(providers, dict) else []):
        if not isinstance(provider, dict) or str(provider.get("api") or "") != "openaix":
            continue
        auth = provider.get("auth")
        has_token = False
        if isinstance(auth, dict):
            token = auth.get("token")
            has_token = isinstance(token, str) and bool(token.strip())
            has_token = has_token or bool(auth.get("authorization")) or bool(auth.get("headers"))
        if not has_token:
            findings.append(
                f"the peer provider '{provider_id}' carries no credentials, so this instance "
                "cannot read the state the peer publishes with a token; add auth to the provider"
            )

    if not findings:
        return CheckResult("auth", "ok", "every surface that needs a token has one")
    result = CheckResult("auth", "warn", f"{len(findings)} surface(s) are open")
    result.details.extend(findings)
    result.recommendation = (
        "Add a token to users.items and set endpoints[].require_auth, or accept the exposure "
        "on a machine that is not reachable from outside"
    )
    return result


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
        result = CheckResult("ollama", "warn", f"upstream does not answer: {base}",
                             details=[str(exc)])
        result.recommendation = "start Ollama (ollama serve) or correct OLLAMA_BASE_URL"
        return result


@_register("systemd")
def _check_systemd(context: Context) -> CheckResult:
    """Report the unit, and start it again if the doctor stopped a restart loop."""
    label = f"{context.service_scope or 'no'} unit: {context.service_state}"

    if context.service_stopped:
        if not _should_start_after_repair(context):
            # Starting it now would rebuild the restart loop that was just stopped.
            result = CheckResult(
                "systemd", "warn",
                f"{context.service_scope} unit stopped; not started because "
                f"{context.failed_checks} problem(s) remain",
                details=["fix them, then start the unit"],
            )
            result.recommendation = _service_command(context, "start")
            return result
        if _service_start(context):
            return CheckResult("systemd", "ok",
                               f"{context.service_scope} unit started after the repairs")
        result = CheckResult("systemd", "fail",
                             f"{context.service_scope} unit was stopped and did not start",
                             details=[f"state: {context.service_state}"])
        result.recommendation = _service_command(context, "start")
        return result

    if context.service_scope is None:
        result = CheckResult("systemd", "warn", "no aidir unit found")
        result.recommendation = "run ./install.sh to install the unit"
        return result

    if context.service_state.startswith("active/running"):
        return CheckResult("systemd", "ok", label)

    details = [f"restarts: {context.service_restarts}"]
    if _service_is_crashing(context):
        result = CheckResult("systemd", "fail", label + " (restart loop)", details=details)
        result.recommendation = (
            "fix the dependency shown above, then start it: "
            + _service_command(context, "start")
        )
        return result

    result = CheckResult("systemd", "warn", label, details=details)
    result.recommendation = "start it: " + _service_command(context, "start")
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
    fix["attempted"] = True
    if fix["class"] == PRIVILEGED and command is not None:
        ok = _sudo(command)
        fix["applied"] = ok
        if not ok:
            fix["error"] = "privileged command failed or was refused without a TTY"
        return
    runner = fix.get("run")
    if runner is not None:
        applied = bool(runner() == 0)
        fix["applied"] = applied
        if not applied:
            fix["error"] = "the fix command returned a non-zero status"
        return
    if command is not None:
        code, output = _run(command, timeout=300)
        fix["applied"] = code == 0
        if code != 0:
            lines = [line for line in output.splitlines() if line.strip()]
            fix["error"] = lines[-1][:300] if lines else f"exit status {code}"


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


def _parse_service_show(output: str) -> tuple[str, int]:
    """`ActiveState/SubState` and `NRestarts` from `systemctl show` output."""
    fields: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key.strip()] = value.strip()
    state = fields.get("ActiveState", "unknown")
    substate = fields.get("SubState", "")
    label = f"{state}/{substate}" if substate else state
    try:
        restarts = int(fields.get("NRestarts") or 0)
    except ValueError:
        restarts = 0
    return label, restarts


def _looks_crashing(state: str, restarts: int) -> bool:
    """Whether a unit state means systemd is restarting it instead of running it."""
    if state.startswith(("activating/auto-restart", "failed")):
        return True
    return restarts > 0 and not state.startswith("active/running")


def _choose_service(candidates: list[tuple[str, str, int]]) -> tuple[str | None, str, int]:
    """Pick the unit that matters: an active one, then a crash loop, then any.

    A host may have both a system and a user unit installed. The inactive system
    unit used to win because it was probed first, which hid the active user unit
    and would have hidden a user-scope restart loop too.
    """
    for item in candidates:
        if item[1].startswith("active/running"):
            return item
    for item in candidates:
        if _looks_crashing(item[1], item[2]):
            return item
    if candidates:
        return candidates[0]
    return None, "unknown", 0


def _service_state(context: Context) -> tuple[str | None, str, int]:
    """Scope, `ActiveState/SubState` and restart count of the aidir unit."""
    candidates: list[tuple[str, str, int]] = []
    for scope, flags in (("system", []), ("user", ["--user"])):
        code, out = _run(
            ["systemctl", *flags, "show", "aidir",
             "-p", "ActiveState", "-p", "SubState", "-p", "NRestarts"],
            timeout=10,
        )
        if code != 0:
            continue
        state, restarts = _parse_service_show(out)
        if state == "unknown":
            continue
        candidates.append((scope, state, restarts))
    return _choose_service(candidates)


def _service_is_crashing(context: Context) -> bool:
    """Whether systemd is restarting the unit instead of running it."""
    return _looks_crashing(context.service_state, context.service_restarts)


def _service_command(context: Context, action: str) -> str:
    """The systemctl command for the unit in its scope."""
    flags = "--user " if context.service_scope == "user" else ""
    return f"systemctl {flags}{action} aidir"


def _should_start_after_repair(context: Context) -> bool:
    """Start the unit only when nothing else is still failing."""
    return context.service_stopped and context.failed_checks == 0


def _service_stop(context: Context) -> bool:
    """Stop the unit before repairs; a system unit needs --root."""
    if context.dry_run or context.service_scope is None:
        return False
    command = ["systemctl"] + (["--user"] if context.service_scope == "user" else [])
    if context.service_scope == "system":
        if not context.privileges:
            return False
        return _sudo(command + ["stop", "aidir"])
    code, _out = _run(command + ["stop", "aidir"], timeout=60)
    return code == 0


def _service_start(context: Context) -> bool:
    """Start the unit after repairs; a system unit needs --root."""
    if context.dry_run or context.service_scope is None:
        return False
    command = ["systemctl"] + (["--user"] if context.service_scope == "user" else [])
    if context.service_scope == "system":
        if not context.privileges:
            return False
        return _sudo(command + ["start", "aidir"])
    code, _out = _run(command + ["start", "aidir"], timeout=60)
    return code == 0


def _docker_state(context: Context, container: str) -> str:
    """One word on the container state: running, restarting, exited(1), dead."""
    code, out = _run(
        ["docker", "inspect", "-f",
         "{{.State.Status}}|{{.State.Restarting}}|{{.State.ExitCode}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}",
         container],
        timeout=15,
    )
    if code != 0 or not out.strip():
        return "unknown"
    parts = out.strip().splitlines()[0].split("|")
    status = parts[0] or "unknown"
    restarting = len(parts) > 1 and parts[1] == "true"
    exit_code = parts[2] if len(parts) > 2 else ""
    health = parts[3] if len(parts) > 3 else ""
    if status == "running" and health:
        status = f"running ({health})"
    elif status == "exited" and exit_code:
        status = f"exited ({exit_code})"
    if restarting:
        status += ", restarting"
    return status


def _container_log_lines(context: Context, service: str, limit: int = 8) -> list[str]:
    """The last log lines of one compose service; empty when unavailable."""
    code, out = _compose(context, ["logs", "--tail", str(limit), service], timeout=20)
    if code != 0:
        return []
    lines = [line.rstrip() for line in out.splitlines() if line.strip()]
    return lines[-limit:]


def _compose_container(context: Context, service: str) -> str:
    """Container id of one compose service, or an empty string."""
    code, out = _compose(context, ["ps", "-q", service], timeout=15)
    return out.strip() if code == 0 else ""


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


def _container_redis_ping(context: Context) -> tuple[str, str]:
    """Whether Redis answers *inside* its container: pong, auth-required, no."""
    code, out = _compose(context, ["exec", "-T", "redis", "redis-cli", "ping"], timeout=15)
    text = out.strip()
    lowered = text.lower()
    if "pong" in lowered:
        return "pong", text
    if "noauth" in lowered:
        return "auth-required (listening)", text
    if code != 0:
        return "not answering", text
    return "unknown", text


def _wait_redis(host: str, port: int, password: str, seconds: int = 20) -> tuple[bool, str | None]:
    """Poll Redis for a bounded time after a repair."""
    deadline = time.monotonic() + seconds
    while True:
        reachable, error = _redis_ping(host, port, password)
        if reachable or time.monotonic() >= deadline:
            return reachable, error
        time.sleep(1)


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

            runtime = SettingsRuntime(root=context.root, environ={})
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


def _port_listener(port: int) -> str | None:
    """Who holds a listening port, as `name (pid N)`; None when unknown."""
    code, out = _run(["ss", "-ltnp"], timeout=10)
    if code != 0:
        return None
    return _parse_listener(out, port)


def _parse_listener(output: str, port: int) -> str | None:
    """Extract the owning process from one `ss -ltnp` table."""
    for line in output.splitlines():
        if f":{port} " not in line and not line.rstrip().endswith(f":{port}"):
            continue
        match = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
        if match:
            return f"{match.group(1)} (pid {match.group(2)})"
        if "LISTEN" in line:
            return "a process whose pid is not visible (run with sudo)"
    return None


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
