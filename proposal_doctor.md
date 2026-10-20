# `doctor` for aidir

file version 1.0.0

Proposal for a diagnostic command in the host: validate every configuration
document through the aiset `doctor` API, then check what aiset cannot see -
the virtual environment, Redis, the containers, the ports - and repair the
repairable part with `--fix`.

Status: implemented on 2026-10-20 (`core/doctor.py`, `core/app.py`,
`test_doctor.py`). `--config-only`, `--quick`, `--fix`, `--fix-all`, `--root`,
`--json`, `--only` and `--dry-run` are in place. A missing required key is
report-only: the library grammar gives it no default to add.

---

## 1. Why

Two real failures after the settings migration, on two machines:

| Machine | Symptom | Real cause |
|---|---|---|
| 040 | `SettingsError: main: undeclared_field at /endpoints/0/context_builder_worker` | A local `config.json5` still carried a key from a removed feature. One restart per stale key. |
| 110 | `redis.exceptions.TimeoutError: Timeout connecting to server` | `.env` named port 6380; the container was not publishing it. |

Neither is exotic. Both took a person reading a traceback and then inspecting
Docker, the `.env` and the descriptor by hand. A doctor turns that into one
command with a list and an `--fix`.

---

## 2. Command

```sh
venv/bin/python core/app.py doctor                 # report: config + host checks
venv/bin/python core/app.py doctor --config-only   # library doctor only; touches nothing
venv/bin/python core/app.py doctor --quick         # cheap subset, used at startup
venv/bin/python core/app.py doctor --fix           # safe repairs
venv/bin/python core/app.py doctor --fix-all       # + visible and lossy repairs, with warnings
venv/bin/python core/app.py doctor --root          # allow privileged repairs (sudo)
venv/bin/python core/app.py doctor --json          # machine-readable
venv/bin/python core/app.py doctor --only redis,config
venv/bin/python core/app.py --help                 # or -h: commands and options
venv/bin/python core/app.py doctor --help          # doctor options only
```

Exit codes, matching the library:

| Code | Meaning |
|---|---|
| `0` | healthy, or warnings only |
| `1` | failures remain |
| `2` | repairs were applied, but something still needs a human |

The same entry point as the service is deliberate: an operator should not have to
remember a second script, and `install.sh` and monitoring can call the same
thing.

### 2.1 Modes

| Mode | What runs | Why it exists |
|---|---|---|
| default | config documents + host checks, read-only | the full picture |
| `--config-only` | config documents only | validate a new configuration while aidir keeps running; no Docker, no network, no restart |
| `--quick` | `aiset`, `config` warnings, `.env`, directories, store lock, venv | one cheap summary at startup |
| `--fix` | default checks, then safe repairs | the common repair |
| `--fix-all` | `--fix` plus visible and lossy repairs, announcing each | the operator has read the plan |
| `--root` | allows the privileged repairs; no effect without `--fix`/`--fix-all` | see 2.2 |

`--config-only` wins over `--only`, and it never runs a host fixer: its purpose is
to answer "is this configuration valid" without touching the running system. It
still calls the library doctor with the requested fix classes, so
`--config-only --fix` repairs documents and nothing else.

### 2.2 Privileged repairs and `--root`

Some repairs need root: starting the Docker daemon, or installing/restarting the
systemd unit. They are never attempted implicitly.

- Without `--root`, a privileged check reports the exact command and stays
  `warn`/`fail`. That is a complete answer; the operator runs it.
- With `--root` and `EUID == 0`, the actions run.
- With `--root` and a normal user, the doctor uses `sudo -n` per action; when the
  credentials are not cached it runs `sudo -v` **once** (the usual password
  prompt) and retries. Without a TTY and without cached credentials it reports
  `fail` with the command instead of hanging.
- The doctor never re-executes itself under `sudo`. Only the individual
  privileged commands run elevated, so the report, the JSON and any file the
  unprivileged fixes create stay owned by the invoking user.

### 2.3 Help, and a hint at startup

The command has to be discoverable, or only the person who wrote it will use it.
Two small things, both cheap:

**`--help` / `-h`** prints the commands and doctor options, and `doctor --help`
prints the doctor options alone. Help must be dispatched in the same early block
as `doctor` (2.4), so it also works when the configuration does not load - an
operator who cannot start aidir is exactly the person who needs `--help`.

```text
usage: core/app.py [--help] <command>

  (no command)   start aidir
  doctor         diagnose the configuration and this machine
                 doctor --help for the options

  --help, -h     this text
```

**A startup hint**, two lines, printed once by a normal start (and therefore
captured by journald) after the quick check:

```text
aidir: started; help: 'venv/bin/python core/app.py --help'   doctor: 'venv/bin/python core/app.py doctor'
```

The hint is not a log-level warning and not a banner. It exists because the
doctor is only useful if an operator remembers it exists; the full report stays
on demand.

### 2.4 The one structural change this forces

`core/config.py` builds the process-wide configuration **at import**
(`config = Config(runtime=get_runtime())`), and `core/app.py` imports it at module
level. So today `python core/app.py doctor` would die in the import of
`core.config` before any doctor code runs - exactly when the command is needed
most.

`core/app.py` therefore dispatches the subcommand **before** its internal
imports:

```python
_ROOT = ...
sys.path.insert(0, ...)

if __name__ == "__main__" and sys.argv[1:2] in (["-h"], ["--help"]):
    from core.doctor import usage
    print(usage())
    raise SystemExit(0)

if __name__ == "__main__" and sys.argv[1:2] == ["doctor"]:
    from core.doctor import main as doctor_main
    raise SystemExit(doctor_main(sys.argv[2:]))

# existing eager imports follow, unchanged
from core.audit_log import AuditLog
from core.config import Config, config as _global_config
...
```

Everything else stays as it is. The alternative - moving every eager import into
`main()` - is a larger change for the same result and is not proposed here.

---

## 3. Two layers, one report

```text
core/app.py doctor
        |
        +-- library layer: aiset doctor for main + each worker document
        |       issues, fixability, repairs (clamp, coerce, drop_unknown)
        |
        +-- host layer: checks aiset cannot make
                aiset checkout, .env, directories, lock, venv,
                Docker, Redis, nginx, ports, upstreams
```

The library layer is `aiset/proposal_doctor.md`. The host **must not** reimplement
validation or file repair: it calls the library, renders the result, and owns only
process and environment state.

A useful consequence: the config layer can be report-only from day one, while the
host checks ship independently.

---

## 4. Checks and fixes

Each check has an id, a status (`ok` / `warn` / `fail`), a message, and zero or
more fixes. `Quick` marks the checks that `--quick` runs at startup.

| id | Check | Failure means | `--fix` | Class | Quick |
|---|---|---|---|---|---|
| `aiset` | `import aiset.asgi` succeeds; the submodule is registered | library missing or not checked out | `git submodule update --init --recursive` | safe (network) | yes |
| `config` | aiset `doctor` on `main` and every worker document | stale keys, bad ranges | `clamp`; `--fix-all` adds `coerce`, `drop_unknown` | safe / lossy | yes (warnings) |
| `env-file` | `.env` exists and parses | first run or a damaged file | **none**: owned by `install.sh` | report only | yes |
| `env-root` | `AIDIR_ROOT` is set and points at this checkout | the tree was moved, or the value is empty | **none**: owned by `install.sh`; report the value to write | report only | yes |
| `dirs` | `logs/`, `logs/audit/`, `docker/volumes/redis` exist and are writable | a fresh checkout, or a permission change | create and verify writability | safe | yes |
| `store-lock` | the sidecar `*.json5.lock` can be flocked | another process holds the store lock (usually a write in flight) | **none**: the kernel releases flock when the holder dies | report only | yes |
| `venv` | `venv/bin/python`, `pip`, and `requirements.txt` satisfied | half-installed environment | `pip install -r requirements.txt` | safe | yes |
| `docker` | `docker info` answers | daemon down | `systemctl start docker` with `--root`; otherwise print it | privileged | no |
| `compose` | `docker compose config` parses | broken compose file or missing `.env` values | **none**: report the parse error | report only | no |
| `redis` | TCP connect + `PING` at `redis.host:redis.port` | container stopped, wrong port, wrong password | start a stopped container; `--fix-all` recreates it | visible (recreate) | no |
| `nginx` | container running; upstream port equals `WEBUI_PORT` | stale container after a port change | start a stopped container; `--fix-all` recreates it | visible (recreate) | no |
| `ports` | WebUI, OpenAIx, MCP, nginx ports free or owned by this instance | a foreign process holds a port | **none**: print the conflict and a free suggestion | report only | no |
| `logs` | `logs/` writable by this user | ownership or disk problem | create directories; report a needed `chmod` | safe | no |
| `ollama` | `${OLLAMA_BASE_URL}/api/tags` answers | upstream not started | **none** | report only | no |
| `systemd` | unit installed, enabled, running | not installed or crashed | `systemctl restart aidir` with `--root`; otherwise print it | privileged | no |

Notes that matter:

- **`redis` is the reason the command exists.** It must read the host and port
  from the *same resolved configuration* the service uses, not from `.env`
  directly, and it must compare that with what the container actually publishes.
  The 110 failure was exactly this comparison: `.env` said 6380, the container
  said 6379.
- **`--fix` does not recreate containers.** Starting a stopped container is
  enough for the common case and causes no outage. Recreating is reserved for
  `--fix-all`, because it briefly stops Redis and nginx, and because an operator
  validating a configuration may not want the running service disturbed at all -
  that is what `--config-only` is for.
- **`.env` belongs to `install.sh`.** The doctor reads it and reports what it
  finds, including a wrong `AIDIR_ROOT`, but never writes it. A second writer of
  the same file is how the port drift in section 1 happened.
- **`store-lock` has nothing to fix.** The sidecar file is created once and stays;
  the lock is a `flock`, released by the kernel when the holder exits, so a
  "stale lock" cannot exist. The check only answers whether some process holds it
  right now, which is normal during a write.
- **`ports` never moves a port.** Choosing a different port rewrites `.env` and
  changes the public surface; that stays an explicit `install.sh` decision.

---

## 5. Fix classes

| Class | Contents | When |
|---|---|---|
| `safe` | create directories, install dependencies, `clamp`, start a stopped container | `--fix` |
| `visible` | recreate a container (brief outage) | `--fix-all` |
| `lossy` | library `drop_unknown` and `coerce` | `--fix-all`, with a warning per change |
| `privileged` | start the Docker daemon, restart the systemd unit | only with `--root`; otherwise the command is printed |

Rules:

1. Every fixing mode is dry-run first: each fixer builds a plan, the plan is
   printed, and only then is it executed.
2. Order is fixed and dependency-driven: `aiset` -> `config` -> `.env` checks ->
   `dirs` -> `store-lock` -> `venv` -> `docker` -> `compose` -> `redis` ->
   `nginx` -> `ports` -> `logs` -> `ollama` -> `systemd`.
3. Every host fixer is idempotent.
4. `--fix-all` announces each visible or lossy change before making it, in the
   text form as a warning and in the JSON as `"class": "lossy"`. The operator
   asked for `--fix-all`, so no extra confirmation prompt is required - except
   when stdin is a TTY and `drop_unknown` would delete more than one key; then a
   single `yes/no` covers the batch.
5. A failed fix does not abort the run: it becomes `fail` with the reason, and
   the remaining checks still run.
6. The doctor never edits `config.json5` itself. That is the library's job and the
   library's backup chain.
7. The doctor never writes `.env`. It reports the value to change and leaves the
   edit to `install.sh`.

---

## 6. Output

```text
$ venv/bin/python core/app.py doctor
aiset        ok      library 0.1.0 from ./aiset/src
config       fail    main: 3 issues (2 fixable), 6 worker documents ok
                       /endpoints/0/context_builder_worker [undeclared_field] drop_unknown
                       /endpoints/0/port [maximum] 70000 -> 65535 (clamp)
                       /tasks/queue_timeout [required] no default, needs a human
env-root     warn    AIDIR_ROOT=/srv/old/aidir, this checkout is /home/denis/aidir
                       install.sh owns .env; run ./install.sh to correct it
redis        fail    cannot reach 127.0.0.1:6380; container publishes 6379
                       fix: docker compose up -d --force-recreate redis   (--fix-all)
nginx        ok
store-lock   ok      not held
ports        warn    openaix 21434 is held by a foreign process (pid 812)
ollama       ok
```

At startup, `--quick` writes one line to the log, before the servers bind. The
two-line hint from 2.3 is a separate message, so `--quick` itself stays one line:

```text
doctor: ok
doctor: 2 warnings, 1 failure - run 'venv/bin/python core/app.py doctor'
```

`--json` returns:

```json
{
  "schema_version": 1,
  "ok": false,
  "mode": "default",
  "checks": [
    {"id": "config", "status": "fail", "message": "main: 3 issues (2 fixable)",
     "issues": [{"path": "...", "rule": "...", "fix": "clamp", "before": "...", "after": "..."}]},
    {"id": "redis", "status": "fail", "message": "cannot reach 127.0.0.1:6380",
     "fixes": [{"action": "docker compose up -d --force-recreate redis",
                "class": "visible", "requires": "--fix-all"}]}
  ],
  "applied": [],
  "skipped": [],
  "exit_code": 1
}
```

The `issues` array is the library report unchanged, so a host UI can render the
same rows it already renders for the editor.

---

## 7. Implementation plan

| Step | Change | File |
|---|---|---|
| 1 | Early dispatch of `doctor` and `-h`/`--help` before eager imports, plus `usage()` | `core/app.py`, `core/doctor.py` |
| 2 | Diagnostic runtime: build documents without raising, so an invalid file is inspectable | `core/settings/runtime.py` (`build(raise_on_invalid=False)` or a `diagnose()` method) |
| 3 | Check registry, fixers, `--config-only` / `--quick` / `--root`, text and JSON rendering, exit codes | `core/doctor.py` (new) |
| 4 | Privileged action runner: `sudo -n`, one `sudo -v` fallback, per-action, never a whole-process re-exec | `core/doctor.py` |
| 5 | Call the library `doctor` when it exists; until then, report `Service.load().issues` | `core/doctor.py` |
| 6 | `--quick` at service start, **before the servers bind**, plus the two-line startup hint; never blocking | `core/app.py` |
| 7 | Tests: broken config, `--config-only` touches nothing, `--help` works with a broken config, Redis down, port mismatch, `--root` without TTY, fixes idempotent, exit codes | `test_doctor.py` (new) |
| 8 | Run the report at the end of installation and print the summary | `install.sh` |
| 9 | Document the command, modes and exit codes | `README.md`, `TEST.md`, `DOCUMENTS.MD` |

Steps 1, 5 and 6 can ship first: a report-only doctor that calls
`Service.load().issues` and logs one line at startup already solves the 040 class
of failure with no library work.

---

## 8. Required library support

The host needs `aiset/proposal_doctor.md` for the repair half. Specifically:

- `Service.doctor(document_id, principal=..., fix=...)` returning issues with a
  fix class and a plan;
- it must work on an invalid document (it is the point);
- a principal without `settings_read_secret` must get a complete report;
- a missing required key is report-only: the descriptor grammar rejects
  `required` together with `default`, so there is no value the library could add;
- `--config-only` must be served entirely by this call, with no host check.

Until then the host can report and print a suggested `set`, but must not edit the
file itself: a second write path is exactly the drift the migration removed.

---

## 9. Decisions

Answers given on 2026-10-20, now part of the design:

| Question | Decision |
|---|---|
| Privileged repairs | An explicit `--root` (2.2). Never implicit; `sudo` per action, one password prompt, no whole-process re-exec. |
| Recreate containers | Not with `--fix`: start a stopped container only. Recreate under `--fix-all`. `--config-only` avoids services entirely, so a new configuration can be checked while aidir runs. |
| `drop_unknown` via `--fix-all` | Allowed, with a warning per change (5.4). |
| Stale lock | There is no such thing: the sidecar is `flock`ed and the kernel releases it. The check reports whether it is currently held; nothing is fixed. |
| `AIDIR_ROOT` | The doctor never rewrites `.env`; it reports the value and points at `install.sh`. |
| `.env` writes | `install.sh` only. |
| Startup report | `--quick` on every start, **before the servers bind**, so the warning is visible even when a listener fails; the full report on demand. |
| `--root` without a TTY | Refused: the privileged action is reported as `fail` with the exact command to run. No hanging password prompt. |
| Privileged ordering | With `--fix-all --root`, all unprivileged repairs run first and the privileged actions last, so a failure there cannot leave the rest half-applied. |
| WebUI surface | Out of scope for the first release. The JSON report exists so a Settings tab button can be added later without changing the contract. |
| Discoverability | `-h`/`--help` and `doctor --help`; a two-line hint on every normal start (2.3). Help and the hint must work when the configuration does not load. |

## 10. Remaining questions

1. **Hint wording and frequency.** Two lines on every start is the proposal; on a
   host that restarts often the journal will repeat it. Acceptable, or only when
   stdout is a TTY?
2. **`--version`.** `--help` lists commands; a version line would help bug
   reports. Not requested, and trivial to add later.

Everything else that was open has been decided in section 9.
