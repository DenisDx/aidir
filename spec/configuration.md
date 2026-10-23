# aidir — The configuration subsystem

> Satellite document of [SPEC_NEW.md](../SPEC_NEW.md). See
> [§9 Configuration](../SPEC_NEW.md#9-configuration) there for the overview.
>
> This document describes the `aiset` integration, the descriptor format, the
> document model, the worker-local configuration, the generated settings editor and
> the configuration assistant.

---

## 1. The `aiset` library

### 1.1 What it is

`aiset` is a **separate project** (`https://github.com/DenisDx/aiset.git`) — a
shared, encapsulated JSON5 settings library. It is consumed by aidir as a **Git
submodule pinned to one commit**; aidir ships no copy and does not vendor it.

```
aiset/src/aiset/
  errors.py      stable error codes and their HTTP status mapping
  pointer.py     JSON Pointer parsing and traversal
  jsonsrc.py     the source-preserving JSON5 template engine
  descriptor.py  descriptor load, validate, compose, compile
  validator.py   value validation and the operational limits
  resolver.py    environment resolution and effective snapshots
  store.py       revisions, the lock, atomic writes, backups, the last known good
  service.py     the shared read/validate/set pipeline
  config_obj.py  the Config object model and the mount grammar
  merge.py       the pure merge(base, update)
  httpd.py       the WSGI adapter and the shared dispatcher
  asgi.py        the ASGI adapter for async hosts
  cli.py         `aiset validate-descriptor`, `aiset validate-document`
  assets/        the reusable UI assets (aiset.js, aiset-tree.js, aiset.css)
```

### 1.2 What it provides to aidir

| Capability | Effect in aidir |
|---|---|
| **Descriptor-driven configuration** | A descriptor is compiled at process start and becomes the allowlist. An undeclared key is an `undeclared_field` **error**, and the whole document becomes `invalid_on_disk`. |
| **Source-preserving edits** | Comments, quoting, trailing commas, CRLF/BOM, Unicode and the final newline survive a write byte for byte outside the edited region. |
| **Atomic, revision-checked writes** | A write takes a sidecar `flock` lock, writes a temporary file, `fsync`s and `os.replace`s it. A bounded `.back` chain keeps the last known good. Every write requires **both** `expected_revision` and `expected_descriptor_revision`; a missing one is `REVISION_REQUIRED` (428), a stale one is `REVISION_CONFLICT` (409). |
| **Secret handling** | A secret subtree is denied **as a whole** to a principal without `settings_read_secret`; a write needs `settings_edit_secret`. It is not redacted field by field. |
| **`${VAR}` substitution** | One global precedence: host overrides → the process environment → the supplied `.env` mapping → the inline `:-` default → the descriptor default. POSIX `:-` only, single pass. A resolved value is data, never structure. |
| **Batch operations** | `set` / `add` / `remove` / `move` / `rename` / `drop` are applied in order to one in-memory candidate and committed as one write. A failing operation leaves the file byte-for-byte unchanged. |
| **Tree snapshots** | One authorized snapshot per revision, with per-node `capabilities`, `origin`, `comments`, `template` and `access_denied`. |
| **A generated tree editor** | The browser editor is built from the descriptor: it stages changes, validates them, and commits one batch. |
| **A doctor** | `service.doctor()` diagnoses a drifted document and can repair it with the `clamp`, `coerce` and `drop_unknown` classes. |
| **Two transports** | The WSGI and the ASGI adapters share one dispatcher, so they cannot drift. |

### 1.3 The submodule

`.gitmodules` declares:

```
path = aiset
url = https://github.com/DenisDx/aiset.git
```

`core/settings/aiset_path.py` computes `AISET_SRC = <root>/aiset/src`. If the
package is not importable, `ensure_aiset_importable()` inserts `aiset/src` on
`sys.path` and re-verifies it by importing `aiset.resolver`, `aiset.service`,
`aiset.store`, `aiset.asgi` and `aiset.merge`.

Two distinct failure messages are produced on purpose:

| Situation | Message |
|---|---|
| The checkout is absent | "Not available" — names the three ways to get a checkout |
| The checkout is present but cannot be imported | "Present but cannot be imported" — names the incomplete-checkout cause and the upgrade commands |

Two traps are handled:

- the bare `aiset/` directory at the project root is a PEP 420 **namespace package**
  that shadows the real one, so the cached modules are purged before retrying;
- the damaged-checkout failure is `ImportError`, not `ModuleNotFoundError`.

`install.sh` runs `git submodule update --init --recursive`, so a plain clone
installs correctly.

**A submodule checkout is on a detached HEAD.** `git pull` there fails with "You
are not currently on a branch". Run `git switch main` first. A commit made while
detached belongs to no branch.

To move the pin:

```bash
cd aiset
git switch main
git pull --ff-only
# ...edit, test, commit and push aiset itself...
cd ..
git add aiset
git commit -m "aiset: <what changed>"
```

**`aiset` is never edited from aidir.** Outstanding wishes go into a new
`aiset/proposal_improvementN.md`, applied by that project's owner. A proposal that
has been written is frozen.

---

## 2. The aidir settings layer

### 2.1 The module map

| Module | Responsibility |
|---|---|
| `core/settings/aiset_path.py` | Make `aiset/src` importable; fail with an instruction, not a traceback |
| `core/settings/runtime.py` | The single `Service`; registers every document; owns the effective cache; the callbacks |
| `core/settings/documents.py` | Find the worker-local documents; the root-wins merge |
| `core/settings/facade.py` | `AisetConfig` — the `config` object the rest of the code already uses |
| `core/settings/wsgi.py` | The ASGI application and the session → principal mapping |
| `core/settings/agent.py` | The configuration assistant |

### 2.2 `core/config.py` — the address

`core/config.py:39` creates one process-wide object:

```python
config = Config(runtime=get_runtime())
```

Because `get_runtime()` builds **and refreshes** at import time, a file that does
not match its descriptor fails **at import**, with the list of issues — not later,
empty. That is why `core/app.py` dispatches the `doctor` sub-command before
importing `core.config`.

### 2.3 `AisetConfig` — the facade

`core/settings/facade.py`. Three deliberate deviations from the library contract,
documented at the top of the file:

1. `get()` returns the default for an **explicit `null`**, not only for an absent
   key.
2. `raw()` returns the **effective (resolved)** tree, not the stored tree.
3. The facade writes with the **host principal**; a browser session never reaches
   this object.

| Member | Behavior |
|---|---|
| `get(key, default=None)` | A dotted-path walk over the effective tree; `None` at any level yields the default |
| `raw()` | The whole resolved configuration |
| `raw_text()` | The file exactly as stored on disk |
| `revision()` | An opaque hash of the stored bytes |
| `update_key(key, value)` | One atomic `set`; the value is serialized with `json.dumps` |
| `update_key_text(key, text)` | One atomic `set` with **JSON5 source text**, keeping `${VAR}` intact |
| `delete_key(key)` | One atomic `remove` |
| `get_key_text(key)` / `get_key_text_or_none(key)` | The stored source text of one path |
| `dotted_to_pointer` / `pointer_to_dotted` | `a.b.c` ⇄ `/a/b/c`, with `~0`/`~1` escaping; the root is `""`, not `"/"` |

### 2.4 `SettingsRuntime`

`core/settings/runtime.py`. One `Service` owns every document.

| Document id | File | Descriptor |
|---|---|---|
| `main` | `config.json5` | `config.descriptor.json5` |
| `worker:<type>:<id>` | `workers/<type>/<id>/config.json5` | `workers/<type>/<id>/descriptor.json5` |

The document table is the runtime's private knowledge — the file path never crosses
the API.

**The resolver.** `Resolver(overrides={}, dotenv=read_dotenv(env_file),
environ=os.environ)`. The `.env` file is parsed by aidir's own `read_dotenv` and
injected as **data**, so the process environment still wins — reproducing the old
`load_dotenv(override=False)` behavior. The same resolver instance is shared by
every document.

**The store settings.** `backup_depth=5`, `lock_timeout_seconds=10.0`.

**The audit.** `Service(..., audit=self._audit)` forwards the library's audit events
into the aidir log as `settings <name>: ...`.

**The callbacks**, registered exactly once:

| Callback | Behavior |
|---|---|
| `on_after_save(previous, current, event)` | Reloads the document and refreshes the effective cache, so an HTTP write is visible to the running process at once |
| `on_probe_changes(...)` | **No veto.** aidir validates through the descriptor instead |
| `on_application(previous, current, changed_paths)` | Logs `settings applied: paths=...`; called only for `live`/`reload` changes |

**The load path.** `load(document_id)` raises `SettingsError` when the document is
`invalid_on_disk`, listing **every** error path. `refresh()` reloads every document
and rebuilds the effective cache. `effective(document_id)` returns the cached
resolved dict.

**The principals.**

| Principal | Where | Permissions |
|---|---|---|
| `CORE_PRINCIPAL` (`aidir-core`) | `core/settings/runtime.py:52` | All — the host itself |
| The session principal | `core/settings/wsgi.py:56-71` | Derived from `webui.auth.users[].permissions` |
| The agent principal (`aidir-settings-agent`) | `core/settings/agent.py:167-173` | `settings_read_source`, `settings_read_effective`, `settings_validate` |

### 2.5 The reload path

| Trigger | Path |
|---|---|
| **SIGHUP** | `core/app.py:876` → `core.reload_config()` → `self.config.load()` → `runtime.refresh()` |
| **The WebUI "Reload settings" button** | `POST /api/settings/reload` → `runtime.refresh()` **and** `core.reload_config()`; returns the new revision; a `SettingsError` is a 422 |
| **After any committed write** | The library's `on_after_save` → the document is re-loaded and the cache re-cached |
| **The doctor** | `SettingsRuntime(root=...).build(refresh=False)` — registers without loading, because an invalid document *is* the finding |

**The library does not poll the file.** An external edit is adopted only on
`refresh()` or `POST /load`.

**Known limitation:** `Core.reload_config` refreshes only the settings cache. The
workers, the endpoints and the resource model are not rebuilt, so a `reload`-policy
change to `workers.*` or `models.providers.*` effectively needs a restart.

---

## 3. The descriptor system

### 3.1 The files

| File | Role |
|---|---|
| `config.descriptor.json5` | The schema of `config.json5` — 19 top-level sections |
| `config.types.json5` | A declaration-only descriptor holding 20 reusable shapes |
| `workers/<type>/<id>/descriptor.json5` | The schema of that worker's `config.json5` |

Every worker descriptor is a thin wrapper over a shared typedef. For example
`workers/agent/openaix/descriptor.json5` is `root: { type: "worker_agent", apply:
"restart", ... }`, so a key edited locally and the same key edited in the root can
never disagree.

### 3.2 The descriptor entry

| Property | Meaning | Example |
|---|---|---|
| `type` | `string`, `int`, `number`, `bool`, `null`, `object`, `array`, `union`, `any`, plus the `log_level` shape | `config.descriptor.json5:346` |
| constraints | `minimum`/`maximum`, `min_length`/`max_length`, `pattern`, `enum`, `const`, `min_items`/`max_items`/`unique_items`, `nullable`, `required`, `key_pattern`/`key_enum` | `config.descriptor.json5:104-108` |
| `default` | The effective-view value; never written back; mutually exclusive with `required` | `ai.timeout_seconds` |
| `secret: true` | The subtree is denied without `settings_read_secret`; a write needs `settings_edit_secret`; inherited by the descendants | `redis.password` |
| `read_only: true` | Every write below it is refused | `version` |
| `apply` | `live` / `reload` / `restart`; inherited from the nearest ancestor | throughout |
| `env: {allowed, convert}` | Whether `${VAR}` is permitted on the node, and how it converts (`string`/`int`/`number`/`bool`) | `config.descriptor.json5:274-277` |
| `description` | The help text; also the editor's node description | every node has one |
| `ui: {widget, rows, placeholder, help, group}` | The editor hints | `config.descriptor.json5:294-297` |
| `children` / `additional_children` / `children_options` | The object members, the dynamic member type, the union alternatives | `config.descriptor.json5:898-902` |
| `typedef` | A reference to a shape declared in `config.types.json5` | `config.types.json5:44` |

### 3.3 The reusable shapes

`config.types.json5` declares 20 shapes:

| Shape | Kind | Purpose |
|---|---|---|
| `log_level` | union | A level name **or** a journald number 0–7 |
| `worker_tool` | union | A tool reference (string) **or** a full internal tool declaration |
| `worker_logging` | object | Per-worker `level` / `alllevel` / `save_llm_request` |
| `http_operation` | object | One remote operation of the `http_api` worker |
| `http_connector` | object | One authenticated remote service plus its operations |
| `mcp_server` | object | One remote MCP server (http / sse / stdio) plus its auth |
| `search_provider` | object | One outbound search/fetch provider (brave / searxng / duckduckgo / tavily) |
| `provider_ref` | object | One upstream candidate of a `first_available` model |
| `env_context_workspace` | object | One workspace reachable from an environment's tasks |
| `worker_entry` | object | The flat worker entry used by `workers.items.<id>` |
| `worker_agent` | object | The agent family (`openaix`) |
| `worker_llm` | object | The forwarding family (`call_ollama`, `call_openai`, `call_llama_cpp`) |
| `worker_search` | object | The network tool family (`web_search`, `web_fetch`) |
| `worker_http_api` | object | The `http_api` tool worker |
| `worker_mcp_client` | object | The `external_mcp` tool worker |
| `worker_context` | object | The context workers |
| `worker_reject` | object | The `reject_agent` family |
| `worker_plain` | object | The `echo_agent` / `selftest` family |

The `type: "any"` convention marks a region whose shape aidir owns (a tool
`inputSchema`, an internal tool declaration): no type rule, no constraint, no
recursion; the operational limits still apply and a `${VAR}` inside stays literal.

### 3.4 The `worker_entry` compromise

`workers.items.<id>` was originally meant to be a union of the eight worker
families. The editor showed eight identical "object" choices with no way to tell
them apart, so it was flattened into one `worker_entry` object that carries every
field any family accepts.

**The cost:** a field read by one family validates in *any* worker. The per-worker
local descriptor is the only place the exact family is enforced.

### 3.5 The undeclared-key rule

The library's validator emits an **error** for every key the descriptor does not
declare, and the service discards the whole state when any error-severity issue
exists.

Consequences:

- the document becomes `invalid_on_disk` and the effective dict is **empty**;
- every write to it is refused;
- `core/settings/runtime.py` raises at import, so the **process stops** with a
  precise diagnostic listing every offending path.

The same applies to a node carrying a `${VAR}` without `env: {allowed: true}`
(`env_not_allowed`).

The rule is measured, not asserted, by `test_config_descriptor.py`:

| Test | What it checks |
|---|---|
| `test_every_configured_path_is_declared` | Every configured path is declared |
| `test_root_config_loads_without_issues` | The active config loads cleanly |
| `test_the_failure_lists_every_undeclared_field` | The diagnostic is complete |

**The contributor's rule.** Adding a key to a configuration file without adding it
to the descriptor fails the build. The converse also holds: a key that no code
reads should be **deleted**, not declared — declaring a dead key hides the next
typo.

---

## 4. The document model

### 4.1 The main document

`config.json5` has 19 top-level sections. See
[§9.2](../SPEC_NEW.md#92-top-level-sections-of-configjson5) for the table.

### 4.2 Worker-local documents

A worker directory becomes a document **only if it carries both** `config.json5`
and `descriptor.json5`. The document id is `worker:<type>:<id>`; the file path never
crosses the API.

Six workers currently have one: `call_ollama`, `openaix`, `context_builder`,
`http_api`, `web_fetch`, `web_search`.

### 4.3 The merge

The worker-local configuration is merged **under** `workers.items.<id>`, and the
**root entry wins** on every key present in both:

```python
merge(root_entry or {}, local_entry or {}, prefer="base")
```

`prefer="base"` means: on a conflict the **base** survives, objects merge
recursively, arrays and scalars are replaced by the winner, and an update can never
delete a base field.

aidir uses the library's own `aiset.merge` rather than a private deep merge, so the
behavior cannot drift.

The merge is applied in `core/workers_loader.py`:

```python
local_cfg = _read_worker_local_config(worker_dir, worker_id)   # the effective values
root_cfg  = items_cfg.get(worker_id, {})                        # config workers.items.<id>
workers_cfg[worker_id] = _merge_worker_entry(local_cfg, root_cfg)
```

`_read_worker_local_config` reads the **effective** values from the settings
runtime, not from a second JSON5 parse.

### 4.4 The discovery rules

Both the worker loader and the document discovery walk two levels under
`workers/`, **skipping the directories whose name starts with `_` or `.`**, in
sorted order. The loader overwrites `instance.id` with the directory name, so the
directory name **is** the worker id.

---

## 5. The settings HTTP surface

### 5.1 The host routes

`webui/backend/app.py:1643-1765`. These live **outside** `/api/settings`, which
the library owns.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/settings-info` | The document ids, the asset base, the current revision, the assistant description |
| POST | `/api/settings/chat` | One turn of the configuration assistant |
| GET | `/api/settings/chat/history` | The caller's own conversation turns |
| DELETE | `/api/settings/chat` | Forget the caller's conversation |
| POST | `/api/settings/reload` | Re-read every document and re-apply it to the process |

`GET /api/settings-info` returns:

```jsonc
{"ok": true,
 "documents": ["main", "worker:agent:call_ollama", ...],
 "assets": "/assets",
 "revision": "<opaque hash>",
 "assistant": { "enabled": true, "model": "local-only", ... }}
```

The front end builds the document selector from this list, so a worker that gains
its own `config.json5` appears without a front-end change.

### 5.2 The library routes

The `aiset` ASGI app is mounted at `/api/settings` **and** at `/assets` (the library
serves its editor assets at the application root).

| Method | Path under `/api/settings` | Purpose |
|---|---|---|
| GET | `/health` | The library version |
| GET | `/descriptor` | The descriptor of a document (`view=authorized\|source\|effective`) |
| GET | `/value` | One value by JSON pointer |
| GET | `/tree` | The tree snapshot the editor renders |
| GET | `/documents` | The document ids (requires `settings_read_source`) |
| POST | `/validate` | Validate one staged value |
| POST | `/document/validate` | Validate a whole document source |
| POST | `/document/doctor` | Diagnose, and optionally fix, a document |
| POST | `/value` | Write one value |
| POST | `/operations` | **Apply** a batch |
| POST | `/operations/validate` | Dry-run a batch |
| POST | `/env` | Which environment variables are present (booleans only) |
| POST | `/load` | Load a document |
| POST | `/load/lkg` | Load the last known good |
| POST | `/descriptor/validate` | Validate the descriptor |
| POST | `/merge` | Merge a document |
| GET | `/assets/<name>` | `aiset.css`, `aiset.js`, `aiset-tree.js`, the host theme |

### 5.3 The principal mapping

`SettingsPrincipalResolver` (`webui/backend/app.py:707-730`) reads the token from
the ASGI scope — the `aidir_token` cookie or `Authorization: Bearer` — resolves the
session, and maps it with `principal_for(session["permissions"], session["login"])`
(`core/settings/wsgi.py:56-71`):

| The account's permissions | The resulting principal |
|---|---|
| `all`, `admin` or `root` | `FULL_PERMISSIONS`: read_source, read_effective, validate, write, load_lkg, read_secret, edit_secret, descriptor_raw |
| anything else | `SESSION_PERMISSIONS ∩ granted`, or `READ_ONLY_PERMISSIONS` when the intersection is empty |
| no token, or an unknown session | `ANONYMOUS` — zero permissions; the library refuses it |

The permission sets are validated against the library's own `PERMISSIONS` at import
time with an explicit `raise` — deliberately **not** an `assert`, which `python -O`
would strip.

---

## 6. The generated settings editor

### 6.1 The ownership split

| Owned by the library | Owned by this project |
|---|---|
| The markup and the DOM structure | The palette (`--aiset-*` variables) |
| `aiset.css` — the light-theme defaults and the structural rules | `webui/frontend/settings.css` |
| The 47 emitted `aiset-tree-*` / `aiset-form-*` classes | The badges, the toolbar buttons, the two-pane workspace, the assistant pane |

The library cannot know the host's palette, and a generated interface that ships no
design has no failure mode: 391 green tests, every route 200, and an unreadable
page. Only a screenshot showed it.

### 6.2 The host stylesheet

`webui/frontend/settings.css` is mostly a variable block, deliberately:

```css
:root {
  --aiset-surface: ...;
  --aiset-surface-strong: ...;
  --aiset-input-bg: ...;
  --aiset-border: ...;
  --aiset-text: ...;
  --aiset-text-soft: ...;
  --aiset-muted: ...;
  --aiset-accent: ...;
  --aiset-ok: ...;  --aiset-pending: ...;  --aiset-warn: ...;  --aiset-err: ...;
  --aiset-type-int: ...;  --aiset-type-string: ...;  --aiset-type-bool: ...;  --aiset-type-other: ...;
  --aiset-indent: ...;  --aiset-guide: ...;  --aiset-row-gap: ...;  --aiset-row-pad: ...;
}
```

Overriding forty selectors is a specificity contest that only the load order can
decide; overriding fourteen variables cannot be lost at all.

The stylesheet also styles what the library names but does not paint:

| Group | What |
|---|---|
| The page chrome | `#cfg-aiset-status`, `#cfg-aiset-card .cfg-row`, `#cfg-document`, `.cfg-check`, `#cfg-aiset-lifecycle`, the two buttons |
| The badges | The apply policy (`.type`, `.apply`), the access rules (`.req`, `.lock`, `.secret`), the staged-edit states (`.absent`, `.default`, `.unsupported`, `.dirty`, `.origin`, `.env`) |
| The toggle | A 20×20 square, so the `+`/`-` glyphs do not shift the column |
| The toolbar | The Apply / Reload / Revert buttons; Apply is the single primary control |
| The helper dialog | **Global rules** — the dialog is appended to `document.body`, not to the editor host, so those rules must not be scoped to `.aiset-tree-editor` |
| The values | The readonly, secret, shape, union and placeholder treatments, and the per-node control-state colors |
| The workspace | The two-pane grid (`minmax(0,1fr) minmax(320px,34%)`), the ≤1100 px stack, the two independent scrollers |
| The assistant pane | The history, the per-role bubbles, the input, the buttons |

### 6.3 The load order

```html
<link rel="stylesheet" href="/api/settings/assets/aiset.css">
<link rel="stylesheet" href="settings.css">
```

The host sheet must come **after** the library sheet. Equal specificity is decided
by order, and `aiset.css` contains no `var()` of its own — every colour is a
literal — so a host theme is otherwise a specificity contest.

### 6.4 The mount

```js
settingsEditor = window.aiset.mountTree('#settings-tree', {
  document: documentId,
  apiBase: '/api/settings',
  view: 'source',
  showAbsent: ...,
  valuesAsText: ...,
});
settingsEditor.on('status', ...);
settingsEditor.on('applied', ...);
```

`mountTree` fills the host element directly and creates **no wrapper**, so the host
stylesheet must scope its rules to `.aiset-tree-editor` — that class has to be on
`#settings-tree`, or the rules silently stop applying and only the library's
structure shows.

### 6.5 The three buttons

| Button | Owner | Effect |
|---|---|---|
| **Apply** | the library toolbar | `POST /operations/validate` (a dry run), then `POST /operations` (one atomic batch). On a 409 the staged batch is **kept** and the user is told to press Reload |
| **Reload** | the library toolbar | Confirm discarding the staged changes, then `GET /tree` |
| **Revert all** | the library toolbar | **Purely client-side**: it clears the staged batch. No server call |

The host's separate **Reload settings** button calls `POST /api/settings/reload`,
which re-reads the documents **and** re-applies them to the process.

### 6.6 The layout

The Settings page sizes its two columns from a measurement, not a constant.
`sizeSettingsWorkspace()` measures the space below the workspace and sets
`--cfg-height` after the mount, on every visit to the page and on resize. Below
1100 px the columns stack and the variable is removed.

Both cards are `flex: 1 1 auto; min-height: 0`, and the two scrollers absorb the
rest. A hard-coded height was tried and abandoned: it stopped matching the moment
the editor header changed — a guess about another element's height is not a layout.

### 6.7 The guardrails

`test_settings_styles.py` fails when:

- a class the editor emits is left undesigned;
- the host sheet loads before the library sheet;
- a host rule escapes the editor scope;
- a host control carries an inline style (an inline declaration outranks any
  stylesheet rule — an unscoped `input[type="number"]` rule once stripped the
  steppers from aidir's own `#log-interval` and `#agent-timeout-input`);
- a library rule outranks the theme.

---

## 7. The configuration assistant

`core/settings/agent.py`.

### 7.1 What it is

An OpenAI-compatible assistant for the Settings page. **The model never writes the
configuration.** It reads authorized nodes through the `aiset` service, proposes
operations, and hands them back for the browser to stage; the user reviews the
batch and presses **Apply**.

The provider call is made in the backend, so the API key never reaches the browser.
The assistant uses **this project's own OpenAI-compatible endpoint**, so it needs no
second provider and no second set of credentials.

A conversation belongs to the WebUI session that started it. The agent is cached on
the app state and rebuilt when the signature of the `ai` block changes — a fresh
instance per request would lose the history.

### 7.2 The configuration

The `ai` section of `config.json5`:

```json5
"ai": {
  "enabled": true,
  "provider": {
    "type": "openai",
    "base_url": "${AIDIR_BASE_URL:-http://127.0.0.1:21434/v1}",
    "api_key": "${AI_API_KEY:-}"      // secret
  },
  "model": "local-only",
  "document": "main",
  "timeout_seconds": 60,
  "temperature": 0.2,
  "max_tool_rounds": 8,
  "history_messages": 24,
  "require_change_confirmation": true  // read_only: the assistant may only stage
}
```

`build_agent(service, effective)` returns `None` — it never raises — when `ai.enabled`
is false or the base URL / model is missing. A misconfigured switch disables the
pane without breaking the WebUI.

### 7.3 The provider call

`_complete(messages)` is a plain `urllib.request` POST to
`{base_url}/chat/completions` with `{model, messages, tools, tool_choice: "auto",
temperature}`. An `Authorization: Bearer` header is added only when a key exists.

| Failure | `AgentError.code` | HTTP |
|---|---|---|
| `HTTPError` | `PROVIDER_HTTP_ERROR` (the body truncated to 400 characters, the provider's message extracted) | 502 |
| `URLError` | `PROVIDER_UNREACHABLE` | 503 |
| `TimeoutError` | `PROVIDER_TIMEOUT` | 504 |
| A bad JSON body or no `choices` | `PROVIDER_PROTOCOL` | 502 |

**A provider failure is never turned into an answer.** The route returns a failure
envelope, not a success-shaped one.

### 7.4 The bounded tool loop

`handle(payload)`:

- `max_tool_rounds` (default 8, descriptor range 1–32) bounds the loop. On
  exhaustion the assistant answers *"The tool round limit was reached before a final
  answer."* and returns the accumulated staged operations.
- A **malformed tool call is recoverable**: the model is asked to resend the
  arguments as a JSON object and the loop continues.
- `_parse_arguments` tolerates a fenced code block and a single-quote repair;
  anything else raises `AGENT_BAD_ARGUMENTS`.
- The history is trimmed by the turn count **and** by a character budget
  (`MAX_HISTORY_CHARS = 24000`, `MAX_TOOL_CONTENT = 16000`, `MAX_TREE_DEPTH = 3`).

### 7.5 The permission set

```python
AGENT_PERMISSIONS = ("settings_read_source", "settings_read_effective", "settings_validate")
```

The assistant's principal holds exactly these. Consequences:

- it **cannot read a secret** — a secret subtree is denied by the library;
- it **cannot write** — `settings_write` is absent, and `settings_stage_operations`
  runs `apply_operations(..., dry_run=True)`, which needs only `settings_validate`;
- a dry run writes nothing.

### 7.6 The four tools

| Tool | Behavior |
|---|---|
| `settings_get` | `service.get(document, path, view="source"\|"effective")` |
| `settings_get_tree` | `service.tree(...)`, bounded by depth and a character budget, marked `truncated` rather than cut mid-JSON |
| `settings_validate_operations` | `apply_operations(..., dry_run=True)` → `{valid, issues}` |
| `settings_stage_operations` | The same dry run; on success the operations are appended to the staged list |

On a `NOT_FOUND`, the tool result additionally carries the available sections and a
hint that the root is the **empty** path `""` — a model naturally writes `"/"`.

The assistant scrubs `api_key` out of any logged tool arguments.

### 7.7 The staging flow

1. The assistant returns `staged_operations` in the chat response.
2. The front end calls `settingsEditor.stageOperations(staged, 'assistant')` and
   tells the user to review the changes and press Apply.
3. The editor validates (`POST /operations/validate`) and commits the whole batch
   with one `POST /operations`.
4. The write is validated against the descriptor and committed atomically under the
   store lock.

So the assistant's operations reach disk **only** when the user presses Apply.

---

## 8. Validation and tooling

### 8.1 The commands

```bash
# The descriptor grammar (must print nothing and exit 0)
aiset/.venv/bin/aiset validate-descriptor config.descriptor.json5 --strict \
    --typedef config.types.json5

# Every descriptor, including the worker ones
for f in config.types.json5 config.descriptor.json5 workers/*/*/descriptor.json5; do
  aiset/.venv/bin/aiset validate-descriptor "$f" --strict --typedef config.types.json5
done

# Both shipped configuration files against their descriptor
venv/bin/python -m unittest -v test_config_descriptor.py

# The host adapter
venv/bin/python -m unittest -v test_settings_backend.py

# The parity against an independent reference reader
venv/bin/python -m unittest -v test_settings_parity.py
```

### 8.2 Validating a whole local configuration

```bash
aiset/.venv/bin/aiset validate-document config.json5 --env .env --strict
```

It lists **every** issue, not the first.

### 8.3 The parity reference reader

`test_settings_parity.py` compares the running backend against an **independent**
reference reader over the same files, key by key. The reference reader applies the
descriptor's documented `env.convert` itself instead of sharing the resolver, so
the two readers stay independent — otherwise the check would only prove that one
parser agrees with itself. That is why `json5` is in `requirements.txt`: the
reference reader must use a parser the service does not use.

---

## 9. Known limitations

| # | Limitation |
|---|---|
| 1 | No file polling. An external edit is adopted only on SIGHUP, on `POST /api/settings/reload`, or on the next write. |
| 2 | The whole document must be valid, or nothing is served. One undeclared key empties the effective tree and refuses every write. |
| 3 | `workers.items.<id>` is a flat `worker_entry`, not a union of the eight families. |
| 4 | The non-admin permission groups are effectively read-only. `principal_for` intersects the account's group names with the permission names; since the groups are things like `all`, any other group name falls back to `READ_ONLY_PERMISSIONS`. There is no documented vocabulary of group names beyond `all` / `admin` / `root`. |
| 5 | `endpoints[].request_timeout` and `endpoints[].tools` are declared although the code ignores them, so old local configurations keep loading — but they also hide typos. |
| 6 | `endpoints[].peer_resources` is read by the endpoint and documented in the README but **not declared** — adding it to a configuration file is a startup error. |
| 7 | `envids.check_period_minutes` and `envids.store_duration_minutes` are declared and shipped but read by nobody. |
| 8 | `logging.logs` / `logging.log_overrides` is read by the cron job but not declared, so it cannot be set through the editor. |
| 9 | `config.json5` is git-ignored at **any** depth, so a *new* worker `config.json5` would be silently ignored by `git add`. The six existing ones are tracked. |
| 10 | `reload_config` only refreshes the cache — it does not re-resolve the workers, the resources or the endpoints. |
| 11 | The browser acceptance pass (TEST.md 7.1) is still open. Everything an HTTP client can verify is a test, but the rendered page must be opened **through nginx**. |
| 12 | The front-end asset path is fragile: the page must ask for `/api/settings/assets/aiset.js` and nginx must forward both prefixes with `^~`. A backend-only test cannot catch this. |
| 13 | The library's MCP adapter and its lifecycle-hook dispatcher are not implemented. A hook-bound node is accepted by the descriptor compiler, but any write touching it is refused with `UNSUPPORTED_CAPABILITY`. |
| 14 | The generated forms, the helper micro-forms, the history/restore and the revision diff of `proposal_improvement2.md` item 5 are still later. |
| 15 | There is no ABI handshake. The descriptor must declare `descriptor_version: "1.0"`; a library upgrade requires re-running the host's tests. |
