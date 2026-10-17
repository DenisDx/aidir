# Document Index

This index describes document responsibilities, not implementation status.
Proposals may include both planned and completed work; consult each document.

## Project and workflow

| Document | Responsibility |
|---|---|
| [README.md](README.md) | User-facing installation, configuration, operation, and troubleshooting. |
| [TEST.md](TEST.md) | Smoke, automated, regression, and manual acceptance test procedures. |
| [SPEC.md](SPEC.md) | Original project specification; read-only for agents. |
| [AGENTS.md](AGENTS.md) | Agent workflow and maintenance rules; read-only for agents. |
| [TODO.md](TODO.md) | Existing task notes and configuration reminders. |
| [plan_aiset_integration.md](plan_aiset_integration.md) | Migration plan of the settings subsystem to the aiset library: phases, descriptor rules, risks, delivery state. |
| [config.descriptor.json5](config.descriptor.json5) | Schema of the root `config.json5`: every key, its type, constraints, secret flag, apply policy and editor help text. |
| [config.types.json5](config.types.json5) | Reusable configuration shapes shared by the root and every worker descriptor. |
| [test_config_descriptor.py](test_config_descriptor.py) | Descriptor gate: both shipped configurations and every worker file must load without a single validation error. |
| [test_settings_backend.py](test_settings_backend.py) | Host adapter: the ASGI mounts, the session-to-principal mapping, and what the editor will render. |
| [test_settings_parity.py](test_settings_parity.py) | Compares the running backend against an independent reference reader over the same files, key for key. |
| [test_frontend_boot_order.py](test_frontend_boot_order.py) | Front-end load order: runs the real browser sources under a minimal DOM and checks that the settings editor appears only after sign-in. |
| [test_frontend_boot_probe.js](test_frontend_boot_probe.js) | The Node harness behind that suite; prints a JSON snapshot of the page before and after authentication. |
| `workers/<type>/<id>/descriptor.json5` | Schema of that worker's own `config.json5`. |
| [memory/context.md](memory/context.md) | Local current working context, integration notes, and open decisions. |
| [memory/history.md](memory/history.md) | Local chronological record of requests, operations, and results. |

## Specifications and integration guide

| Document | Responsibility |
|---|---|
| [aiset/SPEC_.md](aiset/SPEC_.md) | Complete editable aiset specification, maintained in the separately tracked/ignored aiset directory; finalized as SPEC.md later. |
| [aiset/proposal_improvement_aidir.md](aiset/proposal_improvement_aidir.md) | Changes the aiset library needed for this integration, ranked, each with a reproduction; all implemented in the library. |
| [aiset/proposal_improvement2.md](aiset/proposal_improvement2.md) | Further changes the aiset library needs, written after the first round landed; not applied, aiset is a separate project. |
| [spec_timeouts.md](spec_timeouts.md) | Queue, execution, outbound-call, and restart timeout semantics. |
| [openaix_spec.md](openaix_spec.md) | OpenAIx protocol extensions and request/response contracts. |
| [searxng_integration.md](searxng_integration.md) | SearXNG deployment and aidir configuration instructions. |

## Proposals and refactoring plans

| Document | Responsibility |
|---|---|
| [_proposal_embedding.md](_proposal_embedding.md) | Queue-managed OpenAIx embeddings. |
| [_proposal_log_refactoring.md](_proposal_log_refactoring.md) | Logging/audit storage and viewer refactoring. |
| [_proposal_refactoring_1.md](_proposal_refactoring_1.md) | First refactoring plan and execution record. |
| [_proposal_update_1.md](_proposal_update_1.md) | Proposed first security-update round. |
| [before_tools_worker.md](before_tools_worker.md) | Pre-tool-call context-processing hook. |
| [proposal_aidir_smart_router.md](proposal_aidir_smart_router.md) | Resource-aware smart routing design. |
| [proposal_http_api.md](proposal_http_api.md) | Token-authenticated HTTP API tool worker. |
| [proposal_hooks.md](proposal_hooks.md) | Event-driven hook loading, lifecycle API, and llama.cpp empty-response recovery. |
| [proposal_llama_cpp.md](proposal_llama_cpp.md) | llama.cpp provider and server-lifecycle integration. |
| [proposal_ollama_embed.md](proposal_ollama_embed.md) | Transparent Ollama embedding support through OpenAIx. |
| [proposal_opencode_go.md](proposal_opencode_go.md) | OpenCode Go provider and session integration. |
| [proposal_searxng.md](proposal_searxng.md) | SearXNG search/fetch worker integration. |
