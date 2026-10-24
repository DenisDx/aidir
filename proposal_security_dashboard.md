# Proposal: Dashboard Security Panel

Status: proposed. This is a non-update proposal and is independent of the
`proposal_update` series.

## Problem

Security findings are currently visible only through the `doctor` output. The
dashboard shows the execution health and the task queue, but an operator who
does not run the doctor can miss an open surface such as an MCP endpoint that
publishes its tools without a token.

W1 keeps `require_auth` at its existing default of `false`. That makes the
finding advisory, so it needs a visible place in the operator's normal view.

## Proposal

Add a security panel to the dashboard, placed to the left of the system-health
card. The panel lists the open findings from the same source the doctor uses:

- MCP endpoints without authentication, highest severity;
- OpenAIx and Ollama endpoints that publish queue depth and model lists without
  a token;
- peer providers without credentials;
- any other finding the doctor's `auth` check can report.

Each row names the surface, states the exposure in one sentence and gives the
configuration change that closes it. The panel is read-only. It does not change
`require_auth` or any other setting.

When there are no findings, the panel shows a short "no open security findings"
state rather than disappearing, so the layout stays stable.

## Data source

The backend should expose the `auth` check findings in the status payload. The
doctor already computes them in `core/doctor.py`. The panel must use the same
data rather than a second hand-written list, so the two views cannot drift.

## Acceptance

- The panel appears left of the system-health card.
- A configuration with an MCP endpoint and no token produces a visible finding.
- A configuration with no findings shows the empty state.
- Setting `require_auth` to `true` removes the finding after the next status
  refresh.
- The panel writes nothing.
- The doctor and the panel report the same set of findings.
- A frontend test covers both the finding and the empty state.

## Open questions

- The exact severity labels and the ordering of the findings.
- Whether the findings should also be written to the audit log on every status
  refresh or only on change.
- Whether a future `require_auth` default change should make this panel a
  blocking warning instead of an advisory list.
