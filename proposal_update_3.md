# Proposal: aidir Update Round 3

Date: 2026-10-23
Scope: the defects found while investigating two production incidents — a managed
`llama.cpp` server that hangs before opening its listener, and a task that stays
`queued` after its execution was handed to a remote aidir and terminated there.
Constraint: this document is analysis + proposals only; no existing files are
modified by it. The items marked **Fixed** were implemented in the same round and
are listed here for the record.

Item numbering uses the `V` prefix so that it does not collide with
[proposal_update1.md](proposal_update1.md) (`U1`–`U45`).

Priority meaning:

| Priority | Meaning |
|---|---|
| **P1** | A correctness problem that makes a documented behavior false or loses data |
| **P2** | A reliability problem that bites under load, after a restart or on a second instance |
| **P3** | A completeness or consistency problem that misleads a reader or an operator |

---

## 1. Summary

| # | Area | Pri | Item | State |
|---|---|---|---|---|
| V1 | Environment | P1 | The `.env` stopped reaching the process environment after the aiset migration, so a managed server no longer inherits it | **Fixed** |
| V2 | Environment | P1 | The variables of `exec_cmd` are expanded against the process environment only, so `$VAR` stays a literal | **Fixed** |
| V3 | Environment | P2 | The secrets and the fallbacks read from `os.environ` were empty after the migration | **Fixed** |
| V4 | Environment | P2 | A provider could not declare the environment its managed server needs | **Fixed** |
| V5 | Diagnostics | P1 | The stdout of a managed server is block-buffered into a file, so the startup diagnostics are lost | **Fixed** |
| V6 | Diagnostics | P1 | A failed startup is reported as "not ready within Ns" and the evidence is killed with the process | **Fixed** |
| V7 | Diagnostics | P2 | The output of a managed server never reached the aidir log | **Fixed** |
| V8 | Diagnostics | P3 | The managed server inherited the stdin of aidir | **Fixed** |
| V9 | Routing | P1 | A remote aidir that cannot run the model parks the task in `queued` for its whole queue timeout | **Fixed** |
| V10 | Routing | P1 | The affinity can pin a task to a remote candidate that cannot run, while a local candidate is ready | **Fixed** |
| V21 | Routing | P1 | A remote that stops answering is a transient condition and must not fail the task | **Fixed** |
| V22 | Task model | P1 | A request could remove the timeout of its own task with `timeout: 0` | **Fixed** |
| V23 | Task model | P1 | A task removed from the queue kept `status=queued` and became an orphan | **Fixed** |
| V24 | Task model | P2 | The cron cycle never reaped the orphaned tasks | **Fixed** |
| V11 | Routing | P1 | A remote candidate is evaluated locally at route time; its "can run now" is an assumption, not a measurement | **Fixed** |
| V12 | Diagnostics | P1 | The deferral messages are invisible: they are logged at `info` under a level the default configuration hides | **Fixed** |
| V13 | Routing | P2 | A task that waits on a remote cannot be seen to be waiting, and cannot be canceled where it actually is | **Fixed** (the waiting reason) |
| V14 | Task model | P1 | `terminate_task` is a silent no-op for a task that is not in the in-memory registry | **Fixed** |
| V15 | Task model | P2 | The cron job never reaps or reports the orphaned tasks | **Fixed** (V24) |
| V16 | Task model | P1 | A quarantined task keeps `status=queued` and becomes a new orphan | **Fixed** |
| V17 | Deployment | P2 | Two instance namespaces and one hardcoded session prefix coexist | **Fixed** |
| V18 | Diagnostics | P3 | The doctor resolved the placeholders of the inspected root against the process environment | **Fixed** |
| V19 | Diagnostics | P3 | A planned WebUI restart is logged as an error traceback | **Fixed** |
| V20 | Diagnostics | P3 | A managed server that fails to start is visible in the resource panel only | **Fixed** |
| V25 | Lifecycle | P1 | The planned restart exited with code 0, so the supervisor never restarted the service | **Fixed** |
| V26 | Lifecycle | P2 | A live log socket held the graceful shutdown back for the whole grace period | **Fixed** |
| V27 | Tests | P3 | The test suite writes into the real log files, so the log of a production instance contains test doubles | Open |

---

## 2. The environment of a managed server

### V1. The `.env` stopped reaching the process environment (P1, Fixed)

**Evidence**
- Before commit `8b967c9` ("refactoring and switching to aiset", 2026-10-17),
  `core/config.py` started with:

  ```python
  from dotenv import load_dotenv
  load_dotenv(_ROOT / ".env")
  ```

  `load_dotenv` **populates `os.environ`** for every key that is not already set.
- After the migration, `core/settings/runtime.py` parses the file into a mapping and
  hands it to the resolver as data:

  ```python
  Resolver(overrides={}, dotenv=read_dotenv(env_file), environ=os.environ)
  ```

  The docstring claims the behavior is "exactly what `load_dotenv(override=False)`
  did before the migration". The **precedence** was reproduced; the **side effect**
  was not.
- `core/local_server_manager.py:89-94` starts the managed server **without `env=`**,
  so the child inherits the process environment.
- The generated systemd unit (`install.sh:682-703`) has no `Environment=` and no
  `EnvironmentFile=`, so the service environment is the login environment — without
  anything that `.bashrc` exports.

**Impact**
A `llama-server` that aidir starts no longer receives the values that `.env`
carries. When one of them matters to the upstream — `CUDA_VISIBLE_DEVICES`,
`LD_LIBRARY_PATH`, a fork-specific knob — the server behaves differently from the
same command started in an interactive shell. The observed symptom on the
investigation machine was a server that stopped right before the GPU offload,
with no output and no video memory used, while the identical command from a shell
worked.

**Fix (applied)**
- `core/settings/runtime.py` — `apply_dotenv_to_environment()` publishes the
  `.env` values into `os.environ` without overriding an existing value, and the
  application runtime calls it with `publish_environment=True`.
- The publishing is **opt-in per runtime**: a runtime built for another root — the
  doctor inspecting a different checkout — must not touch the process
  environment. See V18.

**Validation**
- `test_local_server_env.py`: the running instance publishes its own `.env`; a
  runtime for another root does not.

---

### V2. The variables of `exec_cmd` are expanded against the process environment only (P1, Fixed)

**Evidence**
- `core/local_server_manager.py:79`:

  ```python
  command = [os.path.expandvars(os.path.expanduser(part)) for part in shlex.split(exec_cmd)]
  ```

- `os.path.expandvars` leaves an unknown variable **as a literal**: it returns
  `'$MODEL_DIR/x.gguf'`, not an empty string.

**Impact**
After V1, an `exec_cmd` that names a variable from `.env` used to be expanded and
now is not: the server receives a path with a literal `$VAR` in it and fails with
a confusing "file not found" instead of the intended path.

**Fix (applied)** — the same change as V1 restores the expansion, because the
variables are in the process environment again.

**Validation**
- Covered by the V1 test: the process environment carries the `.env` values.

---

### V3. The secrets and the fallbacks read from `os.environ` were empty (P2, Fixed)

**Evidence**
- `core/opencode_session.py:32` and `core/endpoints/endpoint_ollama.py:475` read
  `AIDIR_OPENCODE_SESSION_HMAC_SECRET` from `os.environ`.
- `core/kv_affinity.py:281` reads `AIDIR_KV_AFFINITY_SECRET` from `os.environ`.
- `workers/tool/web_search/app.py:127,212` and `workers/tool/web_fetch/app.py:173,273`
  fall back to `os.getenv("BRAVE_APIKEY")`.

**Impact**
After the migration all of these returned an empty string unless the variable was
exported in the shell that started the service. The OpenCode session store then
generated a key from an empty secret, the affinity store fell back to an
unkeyed SHA-256, and the search tools lost their API key when the
configuration did not carry it.

**Fix (applied)** — the same change as V1.

**Validation**
- The environment test covers the publishing; the consequences for each consumer
  are covered by their own suites.

---

### V4. A provider could not declare the environment its managed server needs (P2, Fixed)

**Evidence**
- `config.descriptor.json5` declared `exec_cmd`, `host`, `port` and
  `startup_timeout` for a provider, and nothing for the environment.
- `core/local_server_manager.py` had no way to pass an environment to the child.

**Impact**
The only way to give a managed server a variable was to rely on the environment
of the process that started aidir. Under systemd that environment is not the
user's shell environment, so the value was unreachable.

**Fix (applied)**
- `config.descriptor.json5` — a provider may declare `env`, an open map of
  strings, applied on top of the process environment.
- `core/local_server_manager.py` — `_child_env()` builds the child environment
  from `os.environ` plus the provider's `env` map, and it is passed to
  `create_subprocess_exec(env=...)`.
- `config.json5.example` — a commented example.

**Validation**
- `test_local_server_env.py`: the provider's variables are applied on top of the
  process environment.

---

## 3. The diagnostics of a managed server

### V5. The stdout of a managed server is block-buffered into a file (P1, Fixed)

**Evidence**
- `core/local_server_manager.py:88` opened the log file with
  `log_path.open("ab", buffering=0)` and passed it as the child's stdout.
  `buffering=0` affects the **parent's** writes only.
- The child's libc buffers its stdout **fully** when it is a regular file (4 KiB
  blocks) and its stderr not at all.

**Impact**
The distinction is visible in the log of the investigation machine: the stderr
records of the upstream arrived (`model has unused tensor …`), but the stdout
records did not (`common_params_print_info: verbosity = 3` is missing). The
missing lines were not lost — they sat in the child's buffer.

**Fix (applied)**
- The child's output is read through a **pipe** and copied into the provider's
  log file and into the aidir log, line by line. The stderr of the upstream is
  unbuffered, so it reaches the aidir log immediately.

**Validation**
- `test_local_server_env.py`: the output of a managed server reaches its log file.

---

### V6. A failed startup is reported as "not ready within Ns" (P1, Fixed)

**Evidence**
- `core/local_server_manager.py` — on a startup timeout the manager raised
  `UPSTREAM_UNREACHABLE` with the message
  `llama.cpp provider '…' was not ready within 120s` and then killed the process.
- The kill discarded the child's buffered stdout, and the provider's own log was
  never read.

**Impact**
The operator saw "the server did not start" and nothing else. The one piece of
evidence that would have named the cause — the upstream's own output — was
destroyed by the very code that reported the failure.

**Fix (applied)**
- The exit status and the last lines of the provider's log are collected and
  logged at `error` level **before** the process is stopped.
- The error message names the log file.

**Validation**
- `test_local_server_env.py`: a failed startup reports the exit code and the
  output.

---

### V7. The output of a managed server never reached the aidir log (P2, Fixed)

**Evidence**
- The child's output went only to the provider's log file.

**Impact**
The WebUI shows the provider's log file, but the operator looking at
`logs/all.log` — the place where a system event is expected — saw nothing.

**Fix (applied)**
- Every line of the managed server is also written to the aidir log with the tag
  `llama_cpp`.

**Validation**
- Covered by the V5 test.

---

### V8. The managed server inherited the stdin of aidir (P3, Fixed)

**Evidence**
- `core/local_server_manager.py` did not pass `stdin=`, so the child inherited
  aidir's standard input.

**Impact**
An upstream that reads its standard input — or a wrapper script that does —
would block forever on a descriptor that never produces data.

**Fix (applied)** — the child is started with
`stdin=asyncio.subprocess.DEVNULL`.

---

## 4. The task that stayed in `queued`

### V9. A remote aidir that cannot run the model parks the task forever (P1, Fixed)

**Evidence**
- `core/scheduler.py` — the dispatch gate asked the remote:

  ```python
  if not await self._remote_route_can_run(task, provider_id, model_id):
      task.next_retry_at = time.time() + 1
      deferred_tasks.append(task)
      log("system", "info", f"Task {task.id} delayed: remote aidir cannot run the model now")
  ```

- `_remote_route_can_run` returned `False` for three very different reasons: the
  remote answered "cannot run now", the remote did not answer at all, and the
  model id was empty. All three led to the same one-second deferral.
- The deferral had **no limit**: the task waited until its `queue_timeout`
  expired — 2900 seconds in the observed case.

**The observed task**

```
aidir111:task:0c309fa6-76be-45d5-950f-ec7edcba3d79
  created_at  2026-10-23T07:40:05     queue_timeout 2900     run_timeout 1900
  route       resolved_provider=remote_aidir_110  resolved_model=qwen3.8-27b-local
              selection_reason=affinity
```

The remote answered:

```json
{"provider":"llama_local","model":"qwen3.8-27b-local","priority":5,
 "can_run_now":false,"resource_ready":false,"model_loaded":false,
 "admission":"base_memory",
 "free":{"local_machine":{"VRAM":13795.0}},
 "needed":{"local_machine":{"VRAM":21500}}}
```

The remote had 13795 MiB free and the model needs 21500 MiB. The local candidate
was ready at the moment of the routing (`can_run_now: true`, `model_loaded: true`)
but the affinity had pinned the task to the remote.

**Impact**
The client waited 48 minutes for an answer that could have been given in
milliseconds: the local candidate was warm and idle.

**Fix (applied)**
- The refusal is now a **reason**, not a boolean: `_remote_route_status()` returns
  the admission, the free capacity and the needed capacity of the remote.
- A refusing candidate is **excluded** and the route is resolved again, so the
  task moves to a candidate that can run — the local one, in the observed case.
  The exclusion also drops the affinity record that pointed at the refused
  candidate, so the pin does not come back.
- The number of refusals is **bounded**: after `_MAX_REMOTE_REFUSALS` (5) the
  task fails with `REMOTE_CANNOT_RUN` and a message that names the provider, the
  model and the reason. The client gets an answer instead of a silence.
- The refusal is logged at `warning` instead of `info`.

**Validation**
- `test_remote_refusal.py`: the reason names the remote's capacity; the route
  moves to the local candidate; the task fails with a readable reason when no
  candidate is left; the task is deferred only for a bounded number of rounds.

---

### V10. The affinity can pin a task to a candidate that cannot run (P1, Fixed)

**Evidence**
- The observed task carried `selection_reason: "affinity"` while the local
  candidate was `can_run_now: true` and `model_loaded: true`.
- `core/smart_router.py:203-210` — the preferred candidate is taken from the
  affinity record and kept when it is `probe_ok` and `routing_eligible`.
- The remote candidate's `admission` was computed **locally** (see V11), so it
  said `ready` and the affinity kept it.

**Impact**
The affinity's purpose is to keep a conversation on the executor that holds its
KV cache. When that executor cannot run the task, the affinity turns a working
local candidate into a wait.

**Fix (applied)** — the exclusion of V9 removes the affinity record that points
at the refused candidate, so the router falls back to the ordinary order.

**Validation** — `test_remote_refusal.py`.

---

### V11. A remote candidate is evaluated locally at route time (P1, Fixed)

**Evidence**
- The observed task's probe record:

  ```json
  {"index":1,"provider":"remote_aidir_110","model":"qwen3.8-27b-local",
   "probe_ok":true,"can_run_now":true,"probe_source":"local","probe_latency_ms":0}
  ```

  `probe_source: "local"` and a latency of 0 ms mean **the remote was never
  contacted**. The "can run now" of a remote candidate is an assumption.
- `core/smart_router.py` — the local branch of `evaluate_candidate` applied to
  every candidate whose model declares `resources`, including the model of a
  **remote** instance. The remote branch, which asks the other instance, came
  after it and was never reached.

**Impact**
The route was chosen on a value nobody measured. The first real measurement
arrived later, at the dispatch gate, and the task was already pinned. The local
branch also read the local resource numbers of a model that lives on another
machine.

**Fix (applied)**
- The local branch no longer applies to a provider with `api: "openaix"`: that is
  another aidir instance, its capacity belongs to it, and the router asks it
  over its queue-state route. A remote that cannot be asked is not ready.
- The probe record now names the real source (`remote_openaix`) and the real
  latency.

**Validation**
- `test_remote_candidate_measurement.py`: the remote is probed over the network,
  its own answer decides, and a remote that does not answer is not preferred.

---

### V12. The deferral messages are invisible at the default log level (P1, Fixed)

**Evidence**
- The dispatch gates log at `info`; the shipped configuration sets
  `logging.level: ${LOG_LEVEL:-error}` and `logging.alllevels` for `core`,
  `workers`, `http` and `webui` only.
- The `system` type is not in `alllevels`, so it falls back to the global level:
  **even a `warning` of the `system` type is dropped at the default level.**

**Impact**
Every deferral — the resource shortage, the unavailable remote, the changed
admission — was invisible in the logs. The investigation of the stuck task had to
read the Redis hash and probe the remote by hand, because the log said nothing.

**Fix (applied)**
- The deferrals that indicate a problem are logged at `warning` instead of
  `info`: the model that is not admissible, the resources that changed during the
  admission, the insufficient resources.
- **The log is not the only channel any more.** The scheduler records the reason
  of every waiting task, and the reason travels to the operator through the
  status route and the dashboard: the health card lists how many tasks wait and
  why, per reason. The channel that is always visible replaces the channel that
  the log level hides.

**Validation**
- `test_execution_visibility.py`: the reason of every waiting task is recorded,
  counted per reason, and reaches the runtime health.

---

### V13. A task that waits on a remote cannot be seen to be waiting (P2, Fixed)

**Evidence**
- The user's expectation was that the task had been handed to the remote aidir
  and could be terminated there. It had **not**: the local task was still
  `queued`, waiting for the dispatch gate, and no request had ever reached the
  remote.
- The route trace (`X-Aidir-Route-Id`, `X-Aidir-Visited-Instances`) is only
  added when the request is actually forwarded.
- The task summary carried the status `queued` and nothing else, so the operator
  could not tell a task that waits for a remote from a task that waits for
  memory.

**Impact**
The operator acted on the wrong instance. The task kept waiting because nothing
had been sent anywhere, and nothing in the interface said so.

**Fix (applied)**
- The scheduler records why every waiting task waits, together with the provider
  and the model it waits for.
- The task summary of the dashboard and of the task viewer carries
  `waiting_reason`, `waiting_since`, `waiting_provider` and `waiting_model`, and
  both tables show a **Waiting for** column.
- The health card lists the waiting tasks per reason.

**Still open:** the terminate action of the WebUI does not offer the remote
instance, so a cancel still has to be repeated on the instance that actually
holds the task. The dashboard now says which one that is, but the action itself
is not remote-aware.

**Validation**
- `test_execution_visibility.py` covers the recorded reason; the tables are
  covered by the existing front-end suites.

---

### V25. The planned restart exited with code 0 (P1, Fixed)

**Evidence**
- The first version of the V19 fix caught the planned restart and did not
  re-raise it, so `asyncio.run()` returned normally and the process left with
  code 0.
- The unit is generated with `Restart=on-failure` (`install.sh:682-703`), so a
  clean exit is taken for "the service is fine": the supervisor did not restart
  it, and the service stayed down after the Restart button.
- The journal of the affected machine showed the process leaving and nothing
  after it.

**Impact**
The Restart button stopped the service instead of restarting it. The WebUI became
unreachable and the instance looked hung.

**Fix (applied)**
- The planned restart is re-raised, so the process leaves with code 1 and the
  supervisor restarts the service.
- `run_entrypoint()` was extracted so the exit code is covered by a test.

**Validation**
- `test_restart_exit.py` covers the exit code of the planned restart and of a
  real crash.
- The live restart: `Main process exited, code=exited, status=1/FAILURE`,
  `Scheduled restart job`, and the service answered again.

---

### V26. A live log socket held the graceful shutdown back (P2, Fixed)

**Evidence**
- `webui/backend/app.py` — the log WebSocket handler looped forever and never
  looked at the shutdown state, so the connection kept uvicorn waiting for its
  whole `timeout_graceful_shutdown` before it was cancelled.
- The cancellation surfaced as an exception in the journal:

  ```
  File "/home/denis/aidir/webui/backend/app.py", line 1310, in ws_logs
      await asyncio.sleep(1)
  asyncio.exceptions.CancelledError: Task cancelled, timeout graceful shutdown exceeded
  ```

**Impact**
Every shutdown of an instance with an open dashboard cost the full grace period
and ended with a traceback that looked like a failure of the service.

**Fix (applied)**
- The streaming loop leaves as soon as the service reports that a restart is in
  progress, and a cancellation is not an error any more.
- The loop was extracted into `_stream_log_lines()` so it can be tested.

**Validation**
- `test_restart_exit.py`: the handler returns instead of looping forever.
- The live restart: no `CancelledError` in the journal, and the shutdown took
  about a second instead of the full grace period.

---

### V27. The test suite writes into the real log files (P3, Open)

**Evidence**
- `core.log` writes into the project's `logs/` directory, and the test suite
  calls the code paths that log. The log of a running instance therefore
  contains the doubles' messages:

  ```
  2026-10-23T09:53:04 [WARN] [system] cleanup_expired_tasks: cannot read the agent queue: '_Redis' object has no attribute 'zrange'
  2026-10-23T09:52:57 [ERROR] [system] close check 3
  2026-10-23T09:52:56 [ERROR] [system:llama_cpp] llama.cpp provider 'llama_local' startup output (still running): first-line
  ```

**Impact**
An operator reading the log of a production instance sees test messages, and
cannot tell them from a real failure. It also makes the log grow with every test
run.

**Proposed modification**
1. Point the logger at a temporary directory while the tests run: an environment
   variable, or a fixture that redirects the log root.
2. Or make the logger silent unless a test asks for it.

**Validation**
- A test that the log files of the project are not touched by the suite.

---

## 5. The orphaned tasks

### V21. A remote that stops answering must not fail the task (P1, Fixed)

**Evidence**
- `_remote_route_status` returned one boolean for three different situations:
  the remote answered "cannot run now", the remote did not answer at all, and
  the model id was empty.
- `_handle_remote_refusal` treated all three the same: exclude the candidate,
  count the refusal, fail after five.

**Impact**
A remote that is **restarting** for thirty seconds would fail every task that
waited for it. The user asked for exactly the opposite: *"если удалённый aidir
перестанет быть доступным в течение долгого времени, задача должна быть
переназначена"*.

**Fix (applied)**
- The refusal now carries a **kind**:
  - `capacity` — the remote answered and cannot run the model. That is a stable
    fact about its free memory, so the candidate is excluded **for good** and
    the route is resolved again. If nothing else can take the task, it fails
    with `REMOTE_CANNOT_RUN` after a few attempts, instead of waiting for the
    whole queue timeout for a fact that will not change.
  - `unreachable` — the remote did not answer. That is transient. The candidate
    is skipped **for this attempt only**: the task is re-routed to a candidate
    that can take it, and if there is none it keeps waiting for the remote, and
    goes back to it as soon as it answers. The queue timeout is the bound.
- The distinction is visible in the log message and in the reason.

**Validation**
- `test_remote_refusal.py`: an unreachable remote never fails the task, is not
  blocked for good, and the task is re-routed to a candidate that can take it.

---

### V22. A request could remove the timeout of its own task (P1, Fixed)

**Evidence**
- `core/endpoints/endpoint_openaix.py` — `_resolve_timeout` accepted `0` and
  returned it as-is:

  ```python
  if timeout < 0 or str(value).strip() not in {str(timeout), f"+{timeout}"}:
      raise HTTPException(...)
  return timeout
  ```

- In the task model `0` means "no limit":
  `core/scheduler.py` — `_queue_timeout_expired` returns `False` when the limit
  is `<= 0`, and the run timeout is passed to `asyncio.wait_for` as
  `task.run_timeout or None`.

**Impact**
A client could send `"timeout": 0` and get a task that **waits in the queue
forever and runs forever**. The `tasks.queue_timeout` minimum of 1 in the
descriptor protects the configuration, but not the request.

**Fix (applied)**
- A request timeout of `0` falls back to the configured default. The internal
  "no limit" semantic stays, but a client cannot reach it.

**Validation**
- `test_queue_bounds.py`.

---

### V23. A task removed from the queue kept `status=queued` (P1, Fixed)

**Evidence**
- `core/queue_manager.py` — `delete_task` kept the Redis hash of an external
  task with whatever status it had, and removed it from the ZSETs.
- The endpoint calls `core.delete_task(task.id)` in its `finally` block, so a
  **client disconnect** during a long queue wait removed the task from the
  queue while its hash kept `status: queued`.

**Impact**
The task was now in no queue and could never be dispatched, and nothing would
ever finish it: the cron job only removes terminal external tasks. The
dashboard showed a task that waits forever, and it was invisible to the
scheduler.

**Fix (applied)**
- `delete_task` marks a waiting external task as canceled with the reason
  `TASK_REMOVED` before it drops it.

**Validation**
- `test_queue_bounds.py`.

---

### V24. The cron cycle never reaped the orphaned tasks (P2, Fixed)

**Evidence**
- `core/cron.py` — `cleanup_expired_tasks` removed only the hashes with
  `external == "1"`, a **terminal** status and an old `finished_at`. A hash
  that says `queued` was never touched.

**Impact**
Every orphan — from V23, from a crash between the pop and the re-queue, from
an older version of the code — stayed visible as a waiting task forever. On the
investigated machine there were 35 of them in the old namespace.

**Fix (applied)**
- The same cycle reads the members of both queues once and then fails every
  hash that says `queued` and is in no queue, with the reason `ORPHANED_TASK`.
  The result is logged at `warning` level.

**Validation**
- `test_orphaned_tasks.py`.

---

### V14. `terminate_task` is a silent no-op for a task outside the registry (P1, Fixed)

**Evidence**
- `core/app.py` — `terminate_task()` started with:

  ```python
  task = self.queue.get_task(task_id)
  if task is None:
      return None
  ```

  A task that exists as a Redis hash but not in the in-memory registry was
  reported as absent, and nothing happened.
- The WebUI route answered with that result, so the operator saw a successful
  cancel.

**Impact**
A task whose hash says `queued` but which no queue holds is invisible to the
scheduler and to the cancel path. The operator believes it was stopped; it stays
visible as queued forever.

**Fix (applied)**
- `core/queue_manager.py` — `get_persisted_task()` returns a task from its
  Redis hash even when it is not in memory.
- `core/app.py` — `terminate_task()` falls back to it and cancels the persisted
  task, so the cancel reaches the hash.

**Validation**
- `test_terminate_persisted.py`.

---

### V15. The cron job never reaps or reports the orphaned tasks (P2, Fixed by V24)

**Evidence**
- 35 task hashes in the `aidir1` namespace carry `status=queued` while both
  queue ZSETs are empty. They are from 2026-08-09 to 2026-09-28.
- `core/cron.py` — `cleanup_expired_tasks` removes only the hashes with
  `external == "1"`, a **terminal** status and an old `finished_at`. A hash that
  says `queued` is never touched.
- `core/queue_manager.py` — `recover_startup_tasks` re-adds such a hash to the
  queue at startup, and the scheduler then fails it with `QUEUE_TIMEOUT`. The
  state is therefore repaired only by a restart.

**Impact**
The dashboard shows tasks that will never run. The operator has no way to tell
them apart from the tasks that are genuinely waiting.

**Proposed modification**
1. Extend the cron job: a hash whose status is `queued` and which is in no queue
   is an **orphan**; report it and, after a grace period, mark it failed with
   `ORPHANED_TASK`. **Implemented — see V24.**
2. Record the orphan in the health panel. **Still open.**
3. Make the scheduler report an orphan it pops but cannot find in memory
   (the `popped but not in memory` branch) — the message exists, but at `warn`
   in a log that is empty at the default level. See V12.

**Validation**
- `test_orphaned_tasks.py` covers the reaping; the health panel is open.

---

### V16. A quarantined task keeps `status=queued` and becomes a new orphan (P1, Fixed)

**Evidence**
- `core/queue_manager.py` — the quarantine branch of `recover_startup_tasks`
  writes the reason into `{ns}:queue:quarantine` and leaves the task's own hash
  untouched:

  ```python
  await self._redis.hset(f"{self._ns}:queue:quarantine", task_id, json.dumps({...}))
  quarantined += 1
  ```

  The hash keeps `status: queued`, it is in no ZSET, and the task is inert.

**Impact**
A quarantined task is displayed as queued and is never executed. The quarantine
is the correct place to put it, but the hash must not claim it is still waiting.

**Fix (applied)**
- A quarantined hash is marked `failed` with the reason `QUARANTINED` and a
  `finished_at`, so it is terminal like every other task that left the queue.

**Validation**
- `test_execution_visibility.py`: the quarantined hash does not keep `queued`.

---

### V17. Two instance namespaces and one hardcoded session prefix coexist (P2, Fixed)

**Evidence**
- On the investigated machine the Redis holds two namespaces: `aidir1:task:*`
  (6519 hashes) and `aidir111:task:*` (14598 hashes). The active configuration
  says `"instance": "${AIDIR_INSTANCE:-aidir111}"`.
- `webui/backend/app.py` — the session prefix is the constant `aidir:session:`,
  not the instance id.

**Impact**
The WebUI sessions are **shared between the instances**: a session created on
one instance is valid on the other. The old data occupies space and shows up in
the statistics, and the instance id in the URLs does not describe the data.

**Fix (applied)**
- The sessions are namespaced by the instance:
  `aidir:session:{instance}:{token}`. A session written before the change is
  still accepted, so an upgrade does not log every operator out.
- A new doctor check, `namespaces`, reports the namespaces that hold task data
  next to the configured one, with the command that removes them. It only
  reports: the data of another instance is the operator's decision.

**Validation**
- `test_doctor.py` passes; the check was run against the live instance and
  reported the old `aidir1` namespace with its 6519 task hashes.

**Still open:** the check reports the foreign namespace but does not remove it,
and the old data of the previous instance is still on the machine.

---

## 6. The diagnostics of the doctor and of the logs

### V18. The doctor resolved the placeholders of the inspected root against the process environment (P3, Fixed)

**Evidence**
- `core/doctor.py` built the runtime of the inspected root with
  `SettingsRuntime(root=context.root)`, and the resolver used
  `environ=os.environ`.
- With the process environment carrying the `.env` of the **running** instance
  (V1), the process environment won over the `.env` of the **inspected** root:
  the doctor diagnosed the Redis of the running instance instead of the one of
  the root it was asked to inspect.

**Impact**
The doctor reported a healthy Redis for a broken checkout, and the
`test_doctor.py` cases that simulate an unreachable Redis started to fail.

**Fix (applied)**
- `core/settings/runtime.py` — `SettingsRuntime(root=..., environ={})` builds a
  resolver whose environment is empty, so the inspected root's own `.env` decides
  the placeholders.
- `core/doctor.py` — both runtime constructions pass `environ={}`.

**Validation**
- `test_doctor.py` passes; `test_local_server_env.py` covers the isolation.

---

### V19. A planned WebUI restart is logged as an error traceback (P3, Fixed)

**Evidence**
- `logs/system.log` begins with repeated tracebacks:

  ```
  Traceback (most recent call last):
    File "/home/denis/aidir/core/app.py", line 614, in <module>
      asyncio.run(main())
    ...
    File "/home/denis/aidir/core/app.py", line 608, in main
      raise SystemExit(1)
  SystemExit: 1
  ```

- `core/app.py` — the restart path raises `SystemExit(1)` so that the supervisor
  restarts the process, and the top-level handler logs it as an exception.

**Impact**
The first lines of the system log are a stack trace of a normal operation. An
operator looking for a real failure has to read past it, and the log looks
broken.

**Fix (applied)**
- The planned restart raises a dedicated `_PlannedRestart`, which the entry point
  reports as an event. The exit code stays 1, so the supervisor still restarts
  the service.
- The entry point was extracted into `run_entrypoint()` so that its behavior can
  be tested without starting the service.

**The regression this introduced (V25)**
The first version of the fix caught `_PlannedRestart` and did **not** re-raise it.
The process then left with code 0, the unit's `Restart=on-failure` treated the
clean exit as "the service is fine", and the service stayed down after the Restart
button. The exception must be re-raised: the exit code is a contract with the
supervisor, not a detail of the log.

**Validation**
- `test_restart_exit.py`: the planned restart leaves with code 1, it is not
  logged as a crash, and a real crash still is.
- The live service was restarted through the WebUI button: the process left with
  `status=1/FAILURE`, systemd scheduled the restart, and the service was back.

---

### V20. A managed server that fails to start is visible in the resource panel only (P3, Fixed)

**Evidence**
- `core/resources.py` — `_snapshot_resource` folds the startup errors of the
  llama.cpp providers into `snapshot["startup_errors"]`.
- The provider's `startup_error()` is otherwise only used by the worker that
  tries to start it.

**Impact**
The failure of a managed server is reported to the client that needed it, and in
the resource panel, but the operator has to know to look there.

**Fix (applied)**
- The local server manager publishes the startup error of every provider, and
  the health card of the dashboard lists them with the provider id, the code and
  the reason. The health card is the place the operator already looks.

**Validation**
- `test_execution_visibility.py` covers the health block; the local server
  manager exposes `startup_errors()`.

---

## 7. Already sound — no action needed

- The affinity store keeps a truncated HMAC and a byte length, never prompt text.
- The route trace refuses a loop (`ROUTING_LOOP`) and a hop overflow
  (`ROUTING_HOPS_EXCEEDED`).
- The peer resource and peer task payloads are sanitized: no bodies, no sensor
  commands, no credentials.
- The managed server is only ever stopped when the recorded PID and its start
  ticks still match, so a recycled PID is never acted upon.
- The atomic, revision-checked configuration write with the bounded backup chain
  is intact.
- The task's parent chain has a depth guard.

---

## 8. Suggested implementation order

| Round | Items | Why |
|---|---|---|
| **1** | V1–V4, V5–V8, V9, V10, V14, V18 | **Done.** The environment, the diagnostics of a managed server, the stuck task and the silent cancel. |
| **2** | V21–V24 | **Done.** The bounds: an unreachable remote no longer fails the task, a request cannot remove the timeout of its task, a removed task does not keep a waiting status, and the cron cycle reaps the orphans. |
| **3** | V11, V12, V13, V16, V17, V19, V20 | **Done.** The visibility: a remote candidate is measured before it is chosen, the waiting reason reaches the dashboard, the quarantined task is terminal, the sessions belong to the instance, the doctor sees the foreign namespaces, a planned restart is not a traceback, and a provider that failed to start is in the health card. |
| **4** | V25, V26 | **Done.** The lifecycle: the planned restart keeps the exit code the supervisor needs, and a live log socket no longer holds the graceful shutdown back. |

**Everything in this proposal is now applied.** The items that stay open are
recorded in the sections themselves: the terminate action is not remote-aware
(V13), the doctor reports the foreign namespace but does not remove it (V17), the
orphan count is not yet in the health panel of the dashboard (V15), and the test
suite still writes into the real log files (V27).

---

## 9. Testing plan

- The applied items carry a **Validation** clause and a test module:
  `test_local_server_env.py` (V1–V8), `test_remote_refusal.py` (V9, V10, V21),
  `test_remote_candidate_measurement.py` (V11),
  `test_execution_visibility.py` (V12, V13, V16, V20),
  `test_terminate_persisted.py` (V14), `test_queue_bounds.py` (V22, V23),
  `test_orphaned_tasks.py` (V24), `test_restart_exit.py` (V19, V25, V26),
  `test_doctor.py` (V17, V18).
- The open items need a test each, in the module that owns the area:
  `test_kv_affinity_routing.py` for V11, `test_cron_logging.py` for V15 and V16,
  `test_settings_backend.py` for V17, `test_cron_logging.py` for V19, and
  `test_resource_reuse.py` for V20.
- After each round: `python -m unittest discover -s . -p 'test_*.py' -v`, then
  the smoke checks of `TEST.md` §5, then the manual acceptance matrix of
  `TEST.md` §7 for anything that touches the HTTP surface.
- A live check for the remote refusal: point a smart model at a remote aidir whose
  model does not fit, and confirm that the task is served by the local candidate
  instead of waiting.
