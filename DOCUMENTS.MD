# Document Index

This index describes document responsibilities, not implementation status.
Proposals may include both planned and completed work; consult each document.

## Project and workflow

| Document | Responsibility |
|---|---|
| [README.md](README.md) | User-facing installation, configuration, operation, and troubleshooting. |
| [SPEC.md](SPEC.md) | Original project specification; read-only for agents. |
| [AGENTS.md](AGENTS.md) | Agent workflow and maintenance rules; read-only for agents. |
| [TODO.md](TODO.md) | Existing task notes and configuration reminders. |
| [memory/context.md](memory/context.md) | Local current working context, integration notes, and open decisions. |
| [memory/history.md](memory/history.md) | Local chronological record of requests, operations, and results. |

## Specifications and integration guide

| Document | Responsibility |
|---|---|
| [aiset/SPEC_.md](aiset/SPEC_.md) | Complete editable aiset specification, maintained in the separately tracked/ignored aiset directory; finalized as SPEC.md later. |
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
| [proposal_llama_cpp.md](proposal_llama_cpp.md) | llama.cpp provider and server-lifecycle integration. |
| [proposal_ollama_embed.md](proposal_ollama_embed.md) | Transparent Ollama embedding support through OpenAIx. |
| [proposal_opencode_go.md](proposal_opencode_go.md) | OpenCode Go provider and session integration. |
| [proposal_searxng.md](proposal_searxng.md) | SearXNG search/fetch worker integration. |
