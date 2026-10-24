# Document Index

This index describes document responsibilities, not implementation status.
Proposals may include both planned and completed work; consult each document.

Document precedence: `SPEC_.md` is the editable overlay over `SPEC.md` and wins
when they disagree. The user periodically merges it into `SPEC.md`. Only the
highest-numbered `proposal_updateN.md` is open; older `proposal_update*` files
are closed. Non-update proposals keep their own status. Deferred work lives in
`ROADMAP.md`.

## Specifications

| Document | Responsibility |
|---|---|
| [SPEC.md](SPEC.md) | **The base technical specification and the entry point for agents and developers**: purpose, architecture, domain model, component interfaces, implementation decisions, file system, configuration model, security, testing, known problems. Read-only for agents. |
| [SPEC_.md](SPEC_.md) | Editable overlay over `SPEC.md`: approved changes, decisions and additions. Wins over `SPEC.md` until the user merges it and deletes the overlay. |
| [spec/protocols.md](spec/protocols.md) | The complete HTTP surface: every route, the request and response contracts, the headers, the error envelopes, the streaming rules. |
| [spec/scheduling.md](spec/scheduling.md) | The task lifecycle, the queue semantics, the scheduler algorithm, the resource model, the smart router, the KV affinity mechanism. |
| [spec/configuration.md](spec/configuration.md) | The `aiset` integration, the descriptor format, the document model, the worker-local configuration, the generated settings editor, the configuration assistant. |
| [spec/operations.md](spec/operations.md) | Installation, systemd, Docker/Compose, nginx, the doctor, the cron cycle, the logs, the audit retention, the operational test protocol. |
| [openaix_spec.md](openaix_spec.md) | OpenAIx protocol extensions and request/response contracts (normative). |
| [spec_timeouts.md](spec_timeouts.md) | Queue, execution, outbound-call, and restart timeout semantics (normative). |
| [aiset/SPEC_.md](aiset/SPEC_.md) | Complete editable aiset specification, maintained in the separately tracked/ignored aiset directory; finalized as SPEC.md later. |

## Project and workflow

| Document | Responsibility |
|---|---|
| [README.md](README.md) | User-facing installation, configuration, operation, and troubleshooting. |
| [TEST.md](TEST.md) | Smoke, automated, regression, and manual acceptance test procedures. |
| [AGENTS.md](AGENTS.md) | Agent workflow and maintenance rules; read-only for agents. |
| [TODO.md](TODO.md) | Existing task notes and configuration reminders. |
| [ROADMAP.md](ROADMAP.md) | Deferred work and ideas that need discussion before they become a proposal. |
| [plan_aiset_integration.md](plan_aiset_integration.md) | Migration plan of the settings subsystem to the aiset library: phases, descriptor rules, risks, delivery state. |
| [proposal_doctor.md](proposal_doctor.md) | Proposal for `core/app.py doctor`: library `doctor` for configuration documents plus host checks (aiset checkout, `.env`, directories, venv, Docker, Redis, nginx, ports), with fix classes and open questions. |
| [config.descriptor.json5](config.descriptor.json5) | Schema of the root `config.json5`: every key, its type, constraints, secret flag, apply policy and editor help text. |
| [config.types.json5](config.types.json5) | Reusable configuration shapes shared by the root and every worker descriptor. |
| [test_config_descriptor.py](test_config_descriptor.py) | Descriptor gate: both shipped configurations and every worker file must load without a single validation error. |
| [test_settings_backend.py](test_settings_backend.py) | Host adapter: the ASGI mounts, the session-to-principal mapping, and what the editor will render. |
| [test_settings_parity.py](test_settings_parity.py) | Compares the running backend against an independent reference reader over the same files, key for key. |
| [webui/frontend/settings.css](webui/frontend/settings.css) | Host design of the generated settings editor: the design of the tree, its badges, buttons and controls, built on the project palette. |
| [test_aiset_class_inventory.js](test_aiset_class_inventory.js) | Dumps the library's `aiset.classes()` inventory, so coverage is checked against the contract and not against the library source. |
| [test_settings_styles.py](test_settings_styles.py) | Every class the editor emits must be designed by the host, loaded after the library, with no library rule surviving the theme. |
| [test_kv_affinity.py](test_kv_affinity.py) | Context fingerprint, anchor policy, bounded store, and readiness-history contract. |
| [test_kv_affinity_routing.py](test_kv_affinity_routing.py) | Affinity selection, context-only identity, queue diversion, resource wait, scheduler refresh, and execution fallback. |
| [test_smart_router_direct.py](test_smart_router_direct.py) | Direct smart-model routing: one candidate only, normal admission and queueing, and configuration rejection. |
| [test_webui_sessions.py](test_webui_sessions.py) | WebUI session namespace: instance-scoped keys and rejection of the removed legacy key. |
| [test_session_resources.py](test_session_resources.py) | Model-aware parallel admission, base/session accounting, memory caps, and unload invalidation. |
| [core/settings/agent.py](core/settings/agent.py) | The configuration assistant: the provider call, the bounded tool loop and the permission set that keeps it away from secrets and from writing. |
| [doctor.sh](doctor.sh) | Wrapper: runs the doctor through `venv/bin/python core/app.py doctor` and forwards every argument, so `./doctor.sh --help` shows all options. |
| [core/doctor.py](core/doctor.py) | Host diagnostics: the aiset doctor over every configuration document plus checks for the aiset checkout, `.env`, directories, the store lock, venv, Docker, Redis, nginx, ports, Ollama and systemd, with scheduled repairs. |
| [test_doctor.py](test_doctor.py) | The doctor contract: a drifted key is reported and repaired only by the named class, a dry run writes nothing, and an unreachable Redis is found quickly. |
| [test_aiset_path.py](test_aiset_path.py) | How a missing or incomplete aiset checkout is reported: the two distinct messages and the namespace-package trap behind them. |
| [test_settings_agent.py](test_settings_agent.py) | The assistant contract: it never writes, never sees a secret, never turns a failure into an answer, and stops at its round limit. |
| [test_frontend_boot_order.py](test_frontend_boot_order.py) | Front-end load order: runs the real browser sources under a minimal DOM and checks that the settings editor appears only after sign-in. |
| [test_frontend_boot_probe.js](test_frontend_boot_probe.js) | The Node harness behind that suite; prints a JSON snapshot of the page before and after authentication. |
| `workers/<type>/<id>/descriptor.json5` | Schema of that worker's own `config.json5`. |
| [memory/context.md](memory/context.md) | Local current working context, integration notes, and open decisions. |
| [memory/history.md](memory/history.md) | Local chronological record of requests, operations, and results. |

## Specifications and integration guide

| Document | Responsibility |
|---|---|
| [aiset/proposal_improvement_aidir.md](aiset/proposal_improvement_aidir.md) | Changes the aiset library needed for this integration, ranked, each with a reproduction; all implemented in the library. |
| [aiset/proposal_improvement2.md](aiset/proposal_improvement2.md) | Further changes the aiset library needs, written after the first round landed; not applied, aiset is a separate project. |
| [searxng_integration.md](searxng_integration.md) | SearXNG deployment and aidir configuration instructions. |

## Proposals and refactoring plans

| Document | Responsibility |
|---|---|
| [_proposal_embedding.md](_proposal_embedding.md) | Queue-managed OpenAIx embeddings. |
| [_proposal_log_refactoring.md](_proposal_log_refactoring.md) | Logging/audit storage and viewer refactoring. |
| [_proposal_refactoring_1.md](_proposal_refactoring_1.md) | First refactoring plan and execution record. |
| [_proposal_update_1.md](_proposal_update_1.md) | Proposed first security-update round. |
| [before_tools_worker.md](before_tools_worker.md) | Pre-tool-call context-processing hook. Deferred; see SPEC.md §14.4. |
| [proposal_update1.md](proposal_update1.md) | Closed first work list: the 45 confirmed defects and design gaps from SPEC.md §14, each with its evidence, the proposed modification and its validation. |
| [proposal_security_dashboard.md](proposal_security_dashboard.md) | Dashboard security panel, left of the system-health card, listing open security findings such as MCP without authentication. |
| [proposal_update_3.md](proposal_update_3.md) | The defects found while investigating a managed llama.cpp server that hangs before opening its listener, and a task that stays `queued` after its execution was handed to a remote aidir. Items V1-V28; the applied ones are marked as fixed. |
| [proposal_update4.md](proposal_update4.md) | **The current open ledger**: every item that survived the two previous rounds, each with its source. Items W1-W49, plus the record of the items that are closed. It is the latest `proposal_update` and stays open until a higher-numbered proposal supersedes it. |
| [proposal_aidir_smart_router.md](proposal_aidir_smart_router.md) | Resource-aware smart routing design. |
| [proposal_kv_keeper.md](proposal_kv_keeper.md) | KV-affinity smart routing: prefix fingerprinting, sticky candidate selection, queue-imbalance diversion, and per-request session resources. |
| [plan_kv_keeper.md](plan_kv_keeper.md) | Implementation plan and live state for KV-affinity routing and parallel session admission. |
| [proposal_http_api.md](proposal_http_api.md) | Token-authenticated HTTP API tool worker. |
| [proposal_hooks.md](proposal_hooks.md) | Event-driven hook loading, lifecycle API, and llama.cpp empty-response recovery. |
| [proposal_llama_cpp.md](proposal_llama_cpp.md) | llama.cpp provider and server-lifecycle integration. |
| [proposal_ollama_embed.md](proposal_ollama_embed.md) | Transparent Ollama embedding support through OpenAIx. |
| [proposal_anthropic.md](proposal_anthropic.md) | Anthropic Messages endpoint, provider transport, per-model protocol routing, and OpenCode Go Messages models. |
| [proposal_opencode_go.md](proposal_opencode_go.md) | OpenCode Go provider and session integration. |
| [proposal_searxng.md](proposal_searxng.md) | SearXNG search/fetch worker integration. |
