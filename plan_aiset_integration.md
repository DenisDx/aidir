# Plan: migrating the aidir settings subsystem to aiset

file version 1.0.0

Status: plan of record for replacing the hand-written configuration subsystem of
aidir with the `aiset` library. The controlling constraint is stated once and
never traded away: **aidir must keep working at every intermediate step.**

Delivery state: phases 0 to 3 are implemented and green; phases 4 and 5 are not.
`AIDIR_SETTINGS_BACKEND` defaults to `legacy`, so the running system is still on
the old code until the cutover in phase 5. Section 10 lists what exists today
and what each phase still owes.

---

## 1. Goal and non-goals

### Goal

aidir stores, reads, validates and edits its settings through `aiset`:

| Concern | Today | After |
|---|---|---|
| Parse | `json5.loads` after regex substitution (`core/config.py:408`) | aiset source-preserving CST (`aiset/jsonsrc.py`) |
| Schema | none (only `version == "1.0"`) | `descriptor.json5` per document, enforced on every read and write |
| Write | `json.dumps(indent=2)` + heuristic comment re-injection | atomic, revision-checked, comment-preserving store |
| Concurrency | none | `.lock` sidecar + `expected_revision` |
| Recovery | `install.sh` `.bak` rotation only | `.back` chain, LKG fallback, conflicted copies, rollback |
| Secrets | resolved into `os.environ`, shown in the UI | `${VAR}` stays in the file; `secret: true` subtrees denied by permission |
| UI | 7 hardcoded log-level inputs + a raw textarea (`webui/frontend/app.js:1384`) | generated tree editor (`aiset-tree.js`) |

### Non-goals

- Reimplementing anything that aiset already owns (snapshots, operations,
  validation, revisions, editor). `aiset/AGENT_INTEGRATION.md` section 18.
- Changing documented parameter names or semantics from `SPEC.md`.
- Changing runtime behaviour of any subsystem. The migration is a change of
  *mechanism*, not of *meaning*; every semantic difference found during the
  migration is either reverted or listed in `aiset/proposal_improvement_aidir.md`.
- A parallel new configuration file format. `config.json5` stays the file.

---

## 2. Current state of the aidir configuration subsystem

### 2.1 One reader, 158 call sites

`core/config.py:397` `class Config` is the whole subsystem:

| Member | Location | Contract used by the rest of the project |
|---|---|---|
| `Config(path)` | `core/config.py:403` | loads immediately |
| `load()` | `core/config.py:408` | read + substitute + parse, replaces the in-memory dict |
| `get(key, default=None)` | `core/config.py:569` | **dot-path** lookup, returns the default when any hop is missing **or `None`** |
| `raw()` | `core/config.py:583` | the resolved dict, by reference |
| `raw_text()` | `core/config.py:587` | file text as written |
| `validate_config(text)` | `core/config.py:414` | JSON5 syntax plus `version == "1.0"` |
| `save_config_text(text)` | `core/config.py:426` | validate, `write_text`, reload |
| `update_key(key, value)` | `core/config.py:434` | structural write |
| `update_key_text(key, text)` | `core/config.py:488` | structural write preserving `${VAR}` |
| `delete_key(key)` | `core/config.py:536` | structural write |
| `get_key_text(_or_none)` | `core/config.py:463`, `:475` | raw RHS text for GUI fields |
| module global `config` | `core/config.py:593` | the singleton imported everywhere |

28 files import it; `core/app.py:29` also imports the class. The full-config dict
is additionally injected as a plain dict into components
(`core/app.py:199`, `core/resources.py:55`, `core/hooks.py:27`,
`core/scheduler.py:45`, `core/local_server_manager.py:28`,
`core/peer_resources.py:23`).

### 2.2 The write path is lossy

Every structural write follows: extract comments → `json.dumps(indent=2,
ensure_ascii=True)` → re-inject comments by line heuristics → re-inject `${VAR}`
right-hand sides. The helpers (`core/config.py:30-394`) assume one key per line
and simple nesting. There is no lock, no revision, no atomic replace, no backup.

### 2.3 A second, independent parse path

`core/workers_loader.py:33` reads `workers/<type>/<id>/config.json5` with its own
`json5.loads`, reusing the private `_substitute` from `core/config.py:22`, and
merges the result under `workers.items.<id>` with the root config winning
(`core/workers_loader.py:62-76`, `core/config_merger.py:9`).

### 2.4 Environment handling

`load_dotenv(_ROOT / ".env")` runs at **import time** (`core/config.py:16`) and
mutates `os.environ` process-wide. `_substitute` then replaces `${VAR}` and
`${VAR:-default}` in the raw text **before** parsing, using
`os.environ.get(var, default)`. Four workers additionally read `os.environ`
directly (`workers/tool/web_search/app.py:127`,
`workers/tool/web_fetch/app.py:173`, `workers/tool/selftest/app.py:129`,
`core/endpoints/endpoint_ollama.py:460`), so `os.environ` must stay populated.

### 2.5 Documents on disk

| Document | Descriptor to add | Tracked |
|---|---|---|
| `./config.json5` | `./config.descriptor.json5` | no (`.gitignore:3`) |
| `./config.json5.example` | validated against the same descriptor | yes |
| `workers/agent/call_ollama/config.json5` | `workers/agent/call_ollama/descriptor.json5` | yes |
| `workers/agent/openaix/config.json5` | `workers/agent/openaix/descriptor.json5` | yes |
| `workers/context/context_builder/config.json5` | `workers/context/context_builder/descriptor.json5` | yes |
| `workers/tool/http_api/config.json5` | `workers/tool/http_api/descriptor.json5` | yes |
| `workers/tool/web_fetch/config.json5` | `workers/tool/web_fetch/descriptor.json5` | yes |
| `workers/tool/web_search/config.json5` | `workers/tool/web_search/descriptor.json5` | yes |

Worker descriptors are colocated with the worker they describe, so a worker and
its contract travel together. The root descriptor cannot be `descriptor.json5`
because that name is used by worker directories, so the root file is
`config.descriptor.json5`.

### 2.6 Existing settings UI

`webui/frontend/index.html:1208` `#page-settings`, with a GUI/raw radio
(`index.html:1212`), a hand-built Logging form (`index.html:1217-1293`) bound to
the hardcoded `GUI_LEVEL_FIELDS` table (`app.js:118`), a raw textarea
(`index.html:1298`) and a read-only parse dump (`index.html:1310`). Backend:
`webui/backend/app.py:1059-1123` (`GET/POST /api/config`,
`GET/POST /api/config/raw`, `GET/POST /api/config/fields`). One more writer
exists outside the UI: `POST /api/cron/embedded/enable`
(`webui/backend/app.py:894`) calls `core.config.update_key`.

---

## 3. Target architecture

```text
                        ┌──────────────────────────────────────────┐
  config.json5  ──────► │  aiset.service.Service                    │
  (document "main")     │   register / load / tree / set           │
                        │   apply_operations / rollback / env_status│
  workers/*/*/config.json5 ──► register ("worker:<type>:<id>") ...    │
                        └───────────────┬──────────────────────────┘
                                        │ one Service, one WSGI app
                    ┌───────────────────┴────────────────────┐
                    │ core/settings/  (host adapter)         │
                    │  runtime.py  service bootstrap          │
                    │  facade.py   Config compatibility layer │
                    │  documents.py  worker document registry │
                    │  wsgi.py     ASGI bridge for FastAPI    │
                    └───────────────────┬────────────────────┘
                                        │
              ┌─────────────────────────┴──────────────────────────┐
              ▼                                                    ▼
   158 existing `config.get(...)`                       webui/frontend
   call sites (unchanged)                              index.html settings tab
                                                       + aiset-tree.js
```

### 3.1 Module layout (new, isolated)

| File | Responsibility |
|---|---|
| `core/settings/__init__.py` | re-exports the runtime entry points |
| `core/settings/aiset_path.py` | puts `aiset/src` on `sys.path` until the package is pip-installed |
| `core/settings/runtime.py` | builds `Settings`, `Resolver`, `Service`; registers the root document and every worker document; wires the three host callbacks; feature flag |
| `core/settings/documents.py` | worker discovery (scan `workers/<type>/<id>/`), descriptor lookup, merge of a local worker document under `workers.items.<id>` |
| `core/settings/facade.py` | the `Config` object the rest of aidir keeps importing; dotted `get`, `raw`, `raw_text`, `load`, and the write methods re-expressed as aiset operations |
| `core/settings/wsgi.py` | `create_asgi_app(...)` for the WebUI plus the session-to-`Principal` mapping |

The host owns no transport code and no error translation: the library answers
with its own envelope, which the WebUI already knows how to read. Nothing else
in `core/` changes, so the blast radius of the mechanism change is one package.

**Mounting, learned the hard way.** The library serves its editor assets at the
application root (`/assets/...`), *not* below the API prefix. A host that mounts
it only at `/api/settings` gets a working API and a **blank editor**, because
`/assets/aiset.css` never reaches it. The same application is therefore mounted
twice, at `/api/settings` and at `/assets`.
`WebUIMountTests` in `test_settings_backend.py` exercises the real FastAPI
mounts for exactly this reason: a test against the bare library application
passes either way, which is how the bug survived the first round of tests.

### 3.2 Compatibility facade

`core/config.py` keeps `class Config` and the module-level `config` object, but
the body delegates to the service:

| Old method | New implementation |
|---|---|
| `get(key, default)` | dot-path lookup into the cached **effective** dict |
| `raw()` | the cached effective dict (same object identity, so component injection is unchanged) |
| `raw_text()` | `service.get("main", "", view="source")` is not usable for the whole file, so the store text is read through a service-backed `reread`/`LoadResult`; the raw bytes stay the store's |
| `load()` | `service.reread("main")` + refresh of the effective cache |
| `save_config_text(text)` | `apply_operations` cannot express a whole-file replace, so this maps to a raw `set` of the root pointer, which aiset refuses in v1; the method becomes *not available* and its one caller (the raw-pane POST) moves to the editor |
| `update_key(k, v)`, `update_key_text(k, t)`, `delete_key(k)` | `apply_operations` with one `set` / one `remove` |
| `get_key_text(k)` | `service.get("main", "/dotted/as/pointer", view="source")` → `value_text` |

Two facades are needed because the library speaks JSON Pointer
(`/logging/level`) while aidir speaks dot paths (`logging.level`). The facade is
the only place that knows about the difference, exactly as
`aiset/AGENT_INTEGRATION.md` section 10 and `_AGENT_NOTES.md` require.

### 3.3 Semantics that must be preserved deliberately

| Current behaviour | aiset behaviour | Decision |
|---|---|---|
| `get()` returns the default when a value is `None` | `absent`, `null` and `default` are three distinct states | facade keeps the current "None means default" collapse, because 158 call sites rely on it; documented as a known deviation from `AGENT_INTEGRATION.md` section 11 |
| `${VAR:-d}` with `VAR` set to the empty string yields `""` | POSIX `:-`: empty yields `d` | accept the POSIX rule; the affected keys (`redis.password`) are already declared with an explicit inline default and a `default` |
| `.env` loaded into `os.environ` at import | `Resolver(dotenv=..., environ=os.environ)` | keep `load_dotenv` (four workers read `os.environ`) **and** pass `.env` as `dotenv`; the process environment outranks `.env`, which is what `load_dotenv(override=False)` already does, so the effective order is unchanged |
| root config wins over worker-local config | `merge(base, update, prefer="base")` | `merge(root_item, local_item, prefer="base")` reproduces it exactly |
| `install.sh` `.bak` rotation | aiset `.back` chain | keep both; they guard different phases |
| no revision check | `expected_revision` mandatory | the facade always sends the revision it last read, so an external edit surfaces as `409` instead of being overwritten |

---

## 4. Descriptors

### 4.1 The rule that makes or breaks the migration

`aiset/validator.py:290` `_validate_object` emits an **error**
(`undeclared_field`) for every key of a validated object that the descriptor
does not declare, and `aiset/service.py:1022` `_analyze` discards the whole state
when any error-severity issue exists. A single undeclared key therefore makes the
document `invalid_on_disk` and the effective dict empty. The same applies to a
node that carries a `${VAR}` placeholder but lacks `env: {allowed: true}`
(`env_not_allowed`, `aiset/resolver.py:152`).

So the descriptor is not documentation, it is a **completeness contract**, and
completeness is measured, not asserted.

### 4.2 Authoring rules

1. **Cover both shipped files.** `config.json5` and `config.json5.example` differ;
   a key present in either must be declared. Verified by
   `test_config_descriptor.py`.
2. **`env.allowed: true` wherever a placeholder may appear**, with
   `convert: "int"` for every unquoted numeric placeholder (`redis.port`,
   `tasks.*`, `webui.port`, `envids.*`, `request_timeout`, `safesearch`, ...).
3. **`apply` on every node.** `logging.*` is `live` (the logger re-reads it),
   `redis`, `workers`, `models`, `users`, `envids`, `middleware`, `logging.audit`
   are `reload` (SIGHUP), everything that decides a bind address or a process is
   `restart`. Leaving `apply` unset silently means `restart`
   (`aiset/descriptor.py:1070`).
4. **`secret: true`** on `redis.password`, `webui.auth.users[].password`,
   `users.items[].token`, `workers.items.http_api.connectors.*.auth.key`,
   `workers.items.external_mcp.auth.token`, `workers.items.external_mcp.servers.*.auth.token`,
   `workers.items.*.providers[].apiKey`, `models.providers.*.auth.token`, and the
   `envids.items.*.context.*.token` family.
5. **`read_only: true`** on `version` (`const: "1.0"`) and on derived identifiers
   (`resources[].id`, `endpoints[].id`).
6. **Dynamic keys** through `key_pattern` + `additional_children`:
   `models.providers.*`, `workers.items.*`, `endpoints[].middlewares`,
   `resources[].limits`, `resources[].units`, `resources[].telemetry.sensors`,
   `models.providers.*.models[].resources`, `envids.items`,
   `workers.items.*.tools`, `middleware.items`, `logging.levels`, `logging.alllevels`.
7. **One permissive item node, not `children_options`**, for every array whose
   items differ (`models.providers.*.models`, `workers.items.*.providers`).
   `children_options` on an array is resolved *positionally*, so a union would
   attach the wrong shape to the wrong index. A single item node with
   `additional_children` validates all shapes and keeps one env resolution path.
   `children_options` is used where the alternatives really are positional or
   closed and short (`logging.level` string-or-int, the eight worker families).
   `type: "any"` covers a region whose shape aidir owns (`inputSchema`, an
   internal tool declaration) instead of a hand-written nesting depth.
8. **`typedef` for the repeated shapes**: `log_level`, `provider_ref`,
   `worker_tool`, `search_provider`, `http_connector`, `http_operation`,
   `mcp_server`, `env_context_workspace`, `worker_logging`, and the eight worker
   families `worker_agent` … `worker_plain`.
9. **Self-documenting.** Every node has a `description`; every non-obvious node
   has `ui.help`, `ui.placeholder` or `ui.group`; the file header explains the
   contract, how to validate it and which nodes must not be changed by a UI user.

### 4.3 Validation gate

`test_config_descriptor.py` fails the build unless all of the following hold for
`config.json5`, `config.json5.example` and every worker-local `config.json5`:

- `aiset validate-descriptor <file> --strict` exits `0`;
- `Service.register` + `Service.load` returns `invalid_on_disk == False`;
- every leaf of the document is reachable through `descriptor.node_at`;
- the WebUI-editable surface contains no `read_only` node a user must change.

---

## 5. Implementation phases

Every phase ends green on the existing test suite and is separately revertable.

> Phases 0 to 5 below describe the plan as it was written. Section 10 records
> what actually shipped and supersedes them where the two differ: the backend
> flag is gone, and phase 4 turned out to be automatable.

### Phase 0 - Inventory and guards (no behaviour change)

- `test_config_descriptor.py`: path inventory of both root configs and the six
  worker configs, frozen as an explicit list, so any new configuration key fails
  the suite until the descriptor covers it.
- `.gitignore`: aiset sidecars (`*.lock`, `*.back`, `*.conflicted.*.back`).
- `requirements.txt`: nothing yet; aiset has zero runtime dependencies.

### Phase 1 - Descriptors only

- `config.descriptor.json5` + six worker descriptors.
- Green gate of section 4.3.
- aidir keeps using `core/config.py`. Nothing else changes.

### Phase 2 - Runtime service behind a flag

- `core/settings/` as in section 3.1, selected by
  `AIDIR_SETTINGS_BACKEND=aiset` (default `legacy` until phase 5).
- `core/config.py:Config` gains a thin `_backend` indirection: identical
  signatures, `legacy` implementation unchanged, `aiset` implementation delegating.
- Startup validation: when the aiset backend is selected and any document reports
  `invalid_on_disk`, the process **refuses to start** with the exact issue list.
  Failing loudly is the only acceptable behaviour for a settings subsystem.
- `core/workers_loader.py` reads worker documents from the registry instead of
  parsing JSON5 itself.

### Phase 3 - WebUI settings tab

- `index.html`: keep the page, replace the body of `#page-settings` with a
  document selector plus the container for `aiset.mountTree`; keep the raw-text
  mode as the per-node raw editing the library already provides.
- `app.js`: drop `TEXT_PICKERS`, `LEVEL_LIST_OPTIONS`, `GUI_LEVEL_FIELDS`,
  `bindTextPicker`, `loadConfig`, `saveRawConfig`, `saveGuiConfig`; mount the
  editor once and keep it alive across page switches.
- Assets are served by the library, not by the host: its editor files live at
  the application root (`/assets/...`), **not** below the API prefix, so the same
  library application is mounted a second time at `/assets`. No copy of
  `aiset.js`, `aiset-tree.js` or `aiset.css` enters `webui/frontend/`.
- Transport: `aiset.asgi.create_asgi_app` is mounted at `/api/settings`. The
  library builds the environ, bounds the body and assembles the response; the
  host only supplies a `Principal` for the request. `starlette.middleware.wsgi`
  is deprecated and would in any case not have carried an asynchronous
  principal.
- Authentication: `principal_from_scope` may be an `async def`, so the session
  is resolved in `SettingsPrincipalResolver` (`webui/backend/app.py`) against
  Redis and handed over as a finished `Principal`.
  Without a session the resolver returns a principal with no permissions, which
  yields `401`/`403` from the library itself.
- Permissions: `webui.auth.users[].permissions` is `["all"]` today. `all`
  maps to the full permission set including `settings_read_secret`; a user
  without it gets every secret subtree denied as a whole, as the library
  requires.
- Lifecycle buttons: `POST /api/restart` already exists
  (`webui/backend/app.py:944`) and maps to a `lifecycle` entry; a new
  `POST /api/settings/reload` host route calls `service.reread` plus the existing
  `Core.reload_config` (`core/app.py:327`) for the `reload_required` state.
- Backend: keep `GET /api/config` for compatibility, drop the three write
  endpoints, and move `POST /api/cron/embedded/enable` (`webui/backend/app.py:894`)
  to a one-operation `apply_operations` call.

### Phase 4 - Dual run

- Both backends loaded, aiset the only one serving the UI; a nightly check that
  the legacy effective dict and the aiset effective dict are equal.
- Any divergence is a defect in the descriptor or the facade, not a reason to
  keep the old code.

### Phase 5 - Cutover and deletion

- Default `AIDIR_SETTINGS_BACKEND=aiset`; remove the legacy branch of
  `core/config.py` and the comment re-injection helpers
  (`core/config.py:30-394`), `json5` and `python-dotenv` imports where they are
  no longer needed, and the three legacy settings endpoints.
- `README.md`, `README_RU.md` (if present), `TEST.md`, `SPEC_.md` corrections for
  the semantics that changed, `DOCUMENTS.MD`, `memory/context.md`,
  `memory/history.md`.

---

## 6. Testing

| Level | What | Command |
|---|---|---|
| descriptor | grammar, strict, no unknown property | `aiset/.venv/bin/aiset validate-descriptor config.descriptor.json5 --strict` |
| descriptor | both root configs load clean | `venv/bin/python -m unittest -v test_config_descriptor.py` |
| facade | `get`, `raw`, `raw_text`, writes, revision conflict | same file |
| runtime | existing project suite | `venv/bin/python -m unittest discover -s . -p 'test_*.py'` |
| UI | editor mounts, stages, applies, rejects | manual, added to `TEST.md` |
| secrets | a principal without `settings_read_secret` gets no subtree | unit test in `test_config_descriptor.py` |

New test file: `test_config_descriptor.py` at the project root, matching the
existing convention (all aidir tests live there).

---

## 7. Risks

| Risk | Consequence | Mitigation |
|---|---|---|
| Undeclared key in a live config | every write fails, `effective` empty | gate of section 4.3 runs in CI before any wiring; `undeclared_field` is an error by design, so the failure is loud and immediate |
| Placeholder on a node without `env.allowed` | same as above | section 4.2 rule 2, plus the same gate |
| `secret` newly hides values the WebUI showed today | an admin can no longer read a token in the settings page | `settings_read_secret` granted to `permissions: ["all"]`; the raw-source mode remains available to that principal |
| `${VAR:-d}` POSIX change | an empty variable now yields the default | only affects keys with an inline default; verified key by key in the descriptor PR notes |
| `.back`/`.lock` sidecars in the project root | clutter, accidental commits | `.gitignore` entries in phase 0 |
| ASGI/WSGI mismatch | the editor cannot be mounted | solved by the library: `aiset.asgi.create_asgi_app` (proposal P4), which also removes an awaited-principal hazard |
| Heterogeneous `workers.items` shapes | descriptor becomes enormous or too permissive | a union of eight documented worker families; families repeat their common switches because the grammar has no inheritance |
| A `union` under an object member | edits below it were refused as undeclared paths | library fix (proposal P1, extended to `descriptor.node_at`); the union is used and the tree and editor get per-family shapes |
| Whole-document validation cost on every write | slow UI on a 42 KB file | measured in phase 2; aiset already validates the full candidate, and the file is far below the 8 MiB limit |

---

## 8. Order of work

1. `plan_aiset_integration.md` (this file).
2. `test_config_descriptor.py` inventory test, phase 0.
3. `config.descriptor.json5`, phase 1.
4. Six worker descriptors, phase 1.
5. Green gate, phase 1.
6. `core/settings/`, phase 2.
7. WebUI settings tab, phase 3.
8. Dual run, cutover, deletion, phase 4-5.

## 9. Decisions that need the user

| # | Question | Default if unanswered |
|---|---|---|
| Q1 | May the WebUI no longer show resolved secret values to a session without `settings_read_secret`? | yes, deny the subtree |
| Q2 | Is `${VAR:-d}` with an empty `VAR` allowed to change meaning to POSIX? | yes |
| Q3 | Should `GET /api/config` (whole resolved config, secrets included) survive the migration? | no, remove it; the tree snapshot replaces it |
| Q4 | Root descriptor file name: `config.descriptor.json5` (planned) vs `descriptor.json5` | `config.descriptor.json5` |
---

## 10. Delivery state

| Phase | State | What exists |
|---|---|---|
| 0 - inventory and guards | done | `test_config_descriptor.py` freezes the placeholder inventory and loads both root files against the descriptor; `.gitignore` covers the aiset sidecars |
| 1 - descriptors | done | `config.descriptor.json5` (1264 lines), `config.types.json5`, six `workers/<type>/<id>/descriptor.json5`; strict grammar clean, both shipped files load with zero errors |
| 2 - runtime behind a flag | done | `core/settings/` (6 modules, no transport code of its own), `AIDIR_SETTINGS_BACKEND`, facade parity verified: 522 leaf values, zero differences against the legacy loader, and the same for the merged entries of all 15 workers |
| 3 - WebUI settings tab | done | the library's ASGI application mounted twice, at `/api/settings` and at `/assets` (its assets live at the app root, not below the API prefix), session-to-principal resolver, generated tree editor with a document selector, `POST /api/settings/reload` |
| 4 - dual run | done, automated half | `test_settings_parity.py` compares the backend against an independent reference reader over the same files: 522 leaf values and all 15 merged worker entries, zero differences. The half that needs a live instance under load is a manual step in `TEST.md` 7.1. |
| 5 - cutover and deletion | done | the loader and its comment-preservation helpers are gone from `core/config.py`; `core/config.py` is now a 45-line address that owns nothing; the `AIDIR_SETTINGS_BACKEND` flag is gone with the last thing it selected; `/api/config`, `/api/config/raw` and `/api/config/fields` are removed; `smoke_test.py`, `test_context_overflow.py` and `core/workers_loader.py` read through the facade |

Verification so far: **366 unit tests pass**, including the descriptor gate, the
host adapter and the parity suite. `test_task_priority_defaults` was failing
before this work; the test was stale (commit `b063de4` retuned the priority
scale from `5/10/20` to `0/5/10`) and is now aligned with the code.

There is no longer a second settings backend to run the suite against, so the
comparison is now between the backend and an independent reference reader
rather than between two loaders.

The library changes requested in phase 3 were implemented in the aiset project.
aiset is a separate repository and is not modified from here; the two adapter
workarounds this plan carried while waiting for them have been removed. What is
still worth asking the library for is collected in
`aiset/proposal_improvement2.md`, with the round that landed in
`aiset/proposal_improvement_aidir.md`.

The browser acceptance protocol in `TEST.md` 7.1 is the one item that still
needs a person: everything an HTTP client can verify about the editor is now a
test, but the rendered page itself has not been opened.

### Deviations from section 4.2 of this plan

Two descriptor decisions changed while writing them, both recorded in
`aiset/proposal_improvement_aidir.md`:

Both descriptor compromises made while writing the files are gone, because the
library changes requested in `aiset/proposal_improvement_aidir.md` landed:

1. **`workers.items.<id>` is a union of the eight worker families**, as rule 7
   intended. The first version had to flatten it into one `worker_item` object
   because a union used as an object member type was walked by neither the
   resolver nor `descriptor.node_at`, so the document would not load and every
   edit below it was refused. Both are fixed, so a key that only another family
   reads is an error again and the editor shows the form of the right family.
2. **Free-form regions are `type: "any"`**, not a hand-written nesting depth.
   `inputSchema`, `parameters` and an internal tool declaration are opaque JSON;
   the operational limits still apply to them.

### Decisions taken where section 9 asked

| # | Decision |
|---|---|
| Q1 | Yes. A session without `settings_read_secret` receives the whole secret subtree as denied. `permissions: ["all"]` keeps full access, so no existing administrator loses the ability to read a token. |
| Q2 | Yes. `${VAR:-d}` with an empty `VAR` now yields `d`. The only affected key is `redis.password`, whose inline default is already the empty string, so the resolved value does not change. |
| Q3 | `GET /api/config` is kept and unchanged; it is what the read-only dump on the Settings page uses. It still returns the resolved configuration to an authenticated session, which is the pre-existing behaviour. |
| Q4 | `config.descriptor.json5` for the root, `descriptor.json5` inside each worker directory. |
