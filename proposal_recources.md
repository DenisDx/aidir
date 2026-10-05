# Proposal: Observed Resource Availability and Peer Monitoring

## Status

Implemented. This document defines the delivered resource-monitoring and
scheduling behavior, with the future authentication-default migration retained
as a TODO.

## Summary

Resource availability is currently estimated from configured limits minus aidir's
own active and soft task reservations. That was a useful initial approximation,
but it cannot see memory used outside aidir, actual provider behavior, or remote
machines.

This proposal adds an observed availability source to every resource. A resource
may obtain that observation in priority order from:

1. an explicitly configured command;
2. a remote aidir peer associated with its provider;
3. the existing reservation-based calculation when neither source is configured
   or available.

Resources deliberately remain location-neutral. There is no `local` versus
`remote` resource class: a command runs where this aidir instance runs, and a
peer probe obtains the observation from the aidir instance that owns the
resource.

## Goals

1. Allow a resource to report its actual available capacity through a configured
   command, for example free GPU VRAM from `nvidia-smi`.
2. Refresh that observation before scheduling every task that needs the resource.
3. Refresh observations periodically in the existing resource-monitor cycle so
   the WebUI/API shows current availability.
4. Detect OpenAIx providers that are aidir peers and, when no command overrides
   the resource, obtain their resource state before every forwarded task and
   periodically for display.
5. Expose resource state to peer aidir instances with configurable
   authentication.
6. Retain local reservation accounting as concurrency protection rather than
   treating it as the authoritative view of the machine.
7. Provide an extensible resource-state protocol for later remote telemetry such
   as temperature and power data.

## Non-Goals for the First Implementation

1. Cluster-wide locking or distributed reservations between multiple aidir
   instances.
2. Automatic discovery of all aidir instances on a network.
3. Executing a configured availability command on a remote host over SSH.
4. Replacing existing sensor configuration or threshold reactions.
5. Deriving precise process-level GPU usage or attributing external usage to a
   model.
6. Persisting peer discovery across a restart.

## Current Baseline

The repository already has useful foundations:

1. `Resource` tracks configured limits, active reservations, soft model
   reservations, and public sensor state.
2. `ResourceMonitor` polls command-based sensors at
   `resources[].monitoring.poll_interval`.
3. The scheduler resolves model resource requirements, re-checks availability
   before dispatch, and creates/release reservations around a task.
4. Smart routing already probes an `api: "openaix"` candidate through a
   model-only queue-state endpoint before selecting it.
5. OpenAIx already exposes provider/model and model-only queue-state endpoints.

The new work must extend those mechanisms instead of adding a second monitoring
loop or a second resource registry.

## Proposed Configuration

### Resource availability probe

Add an optional `availability` object to any resource:

```json5
{
  "id": "gpu_0",
  "type": "cuda",
  "limits": {"VRAM": 22000},
  "availability": {
    "command": "nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits",
    "metric": "VRAM",
    "unit": "MiB",
    "command_timeout": 5
  },
  "monitoring": {
    "poll_interval": 10,
    "sensors": []
  }
}
```

`command` is an administrator-configured, trusted shell command, with the same
execution and timeout discipline as existing sensor commands. Its trimmed
standard output must be one finite numeric value. `metric` must name a metric
already present in `limits`; `unit` documents the command output and must match
the configured limit's unit convention when both units are provided. An omitted
unit is represented as an empty string and does not block the probe; in that
case the administrator is responsible for ensuring values are comparable. The
reported number is **available** capacity, not used capacity.

The v1 command probe is intentionally one metric per resource. A later format
may accept a JSON map when one command can reliably report several limits.

### Peer mapping

When no `availability.command` is configured, a resource with a provider whose
API is `openaix` can obtain its state from the peer:

```json5
{
  "id": "remote_gpu_0",
  "type": "cuda",
  "limits": {"VRAM": 22000},
  "provider": "remote_aidir",
  "availability": {
    "peer_resource_id": "gpu_0",
    "request_timeout_ms": 1500
  },
  "monitoring": {
    "poll_interval": 10
  }
}
```

`provider` resolves the peer base URL and authentication from
`models.providers.<provider>`. `peer_resource_id` defaults to the local
resource `id`. It is needed when two peers use different names for the same
resource. A peer probe is eligible only when the provider API is exactly
`openaix`.

The resource does not need an `availability` object when its local and peer IDs
are equal; its `provider` and `monitoring` configuration are enough. The object
is needed only to override the peer resource id or probe timeout.

### Peer resource API access

Add peer-resource access policy to the OpenAIx endpoint configuration:

```json5
{
  "id": "openaix",
  "api": "openaix",
  "peer_resources": {
    "auth_mode": "disabled"
  }
}
```

`auth_mode` accepts:

1. `disabled` (the default): resource endpoints are readable without an
   OpenAIx bearer token;
2. `endpoint`: resource endpoints use the endpoint's ordinary authentication
   and environment authorization rules.

The peer client continues to send a configured provider bearer token when one
exists; a server with `disabled` mode simply does not require it. Deployments
that expose aidir outside a trusted network should set `auth_mode: "endpoint"`.

TODO: Change the default `peer_resources.auth_mode` to `endpoint` in a future
major version after existing trusted-network deployments have migrated.

### Selection precedence

For every resource, the source is selected as follows:

1. A non-empty `availability.command` is authoritative. No peer request is
   attempted.
2. Otherwise, an associated provider confirmed as an aidir peer is used.
3. Otherwise, aidir uses the current configured-limit-minus-reservations
   behavior.

This makes an administrator-supplied command the explicit override required for
machines where it is more accurate or where the upstream is not aidir.

## Resource State Model

Add serializable runtime state separate from `used`, `soft_used`, and sensors:

```json
{
  "source": "command",
  "status": "ok",
  "available": {"VRAM": 22173},
  "observed_at": "2026-10-05T10:00:00+00:00",
  "error": null
}
```

`source` is `command`, `peer`, or `estimated`; `status` is `ok`, `pending`, or
`error`. For an error, the previous successful observation remains visible with
its original timestamp, while `status` and `error` make the stale/failed probe
explicit. A failed probe must never be presented as a fresh successful value.

The public resource snapshot should include:

1. existing limits, calculated `used`, soft usage, consumers, and sensor state;
2. `availability`: source, status, available values, observation timestamp, and
   error;
3. `telemetry`: an initially empty extension object reserved for peer-provided
   sensor/telemetry data.

Executable commands, authentication values, and peer base URLs must not appear
in snapshots or peer responses.

## Scheduling Semantics

### Measured availability with reservation protection

The measured result is the scheduling source of truth, but measured state alone
is unsafe when two local tasks are dispatched between measurements. The
implementation must preserve reservations as an overlay:

1. Refresh all required resources immediately before a task is admitted.
2. Under the resource admission lock, compare each requested metric with the
   latest observed available capacity.
3. Subtract only reservations created **after** that successful observation.
   Reservations already active at measurement time are physically represented by
   the command or peer result and must not be subtracted twice.
4. On successful admission, record the reservation with the observation
   generation it follows.
5. A later successful observation starts a new generation; completed
   reservations remain useful for diagnostics but no longer reduce that observed
   capacity.

This prevents oversubscription caused by concurrent scheduler iterations while
avoiding the current double-counting of work that is visible in the real
measurement. Releasing a task does not increase an observed value locally; the
next pre-dispatch probe or periodic poll supplies the new physical value.

Admission of all resources for one task must be atomic with respect to other
local admissions. If one resource fails its refresh or capacity check, no
reservation may be left on the others.

### Dispatch behavior

Before every dispatch:

1. Resolve or refresh a smart route as today.
2. Resolve that route's resource requirements.
3. Refresh each requirement that has a command or peer availability source.
4. Apply the availability decision under the atomic admission lock.
5. If capacity is unavailable, retain the existing defer/retry behavior and
   route refresh, allowing smart routing to select another candidate.
6. Reserve only after a successful admission and release the reservation on all
   existing task terminal paths.

For a peer resource, step 3 is required immediately before forwarding the task,
even if it was also polled moments earlier. The remote aidir still performs its
own admission check; this local probe improves routing but is not a replacement
for remote enforcement.

For resources that use the fallback estimate, existing active/soft reservation,
force-unload, reuse, and keep-alive behavior remains unchanged.

## Periodic Monitoring

`ResourceMonitor` becomes the single periodic refresh owner:

1. It keeps the existing per-resource `poll_interval`.
2. During a due resource poll it refreshes availability once, then polls all
   configured sensors.
3. Command and peer failures are isolated per resource and logged with resource
   ID, source, and sanitized error details.
4. The monitor never silently changes to a successful estimated result after a
   configured command or confirmed peer probe fails. A failed pre-dispatch
   refresh fails closed for that resource: the scheduler defers or reroutes the
   task and does not use a stale observation for admission.
5. A failed resource remains due for later periodic refreshes. A successful
   command or peer response restores it automatically, without restart or
   manual intervention.

Availability refreshes requested by the scheduler reuse the same probe code but
are not delayed behind a periodic cycle.

## Peer Discovery and Protocol

### Discovery

The first time an `openaix` provider is needed as a peer, aidir calls the new
resource endpoint and sends provider authentication when configured. A
successful compatible response marks that provider as an aidir peer in an
in-memory capability cache. A `404`, incompatible schema, or authentication
failure marks it as not a peer for the remainder of the process lifetime.
Timeouts and transport failures are transient probe errors and are retried by a
later due poll or pre-dispatch probe; they are not cached as incompatibility.

This fulfils the required "try through OpenAIx and remember until reboot"
behavior without changing ordinary OpenAI-compatible providers. Discovery state
is not persisted; a restart re-evaluates the provider.

### New endpoints

Add read-only OpenAIx extensions:

1. `GET /v1/resources`
2. `GET /api/resources`
3. `GET /v1/resources/{resource_id}`
4. `GET /api/resources/{resource_id}`

They apply `peer_resources.auth_mode`: `disabled` permits a read without an
OpenAIx bearer token, while `endpoint` uses ordinary endpoint authentication and
environment authorization. No response includes a command, provider
credential, active consumer ID, or internal model activity.

The collection response is:

```json
{
  "object": "list",
  "data": [
    {
      "id": "gpu_0",
      "type": "cuda",
      "limits": {"VRAM": 22000},
      "availability": {
        "source": "command",
        "status": "ok",
        "available": {"VRAM": 22173},
        "observed_at": "2026-10-05T10:00:00+00:00",
        "error": null
      },
      "telemetry": {}
    }
  ]
}
```

The single-resource response returns the same resource object. The protocol
version should be included in a response header or payload field so future
telemetry additions can be negotiated without guessing.

### Remote telemetry

The peer response is designed to carry a `telemetry` object later. The first
implementation uses it only as `{}` and does not copy arbitrary sensor command
definitions. A later version may expose a safe whitelist of readings, for
example GPU temperature, with units, timestamps, and error state.

## Error Handling and Observability

1. Invalid availability configuration is a configuration error, logged once per
   distinct resource/error and shown as `availability.status: "error"`.
2. Command exit failures, parse failures, timeouts, invalid peer responses, and
   network failures are explicit probe errors.
3. Peer credentials are sent only to the configured provider URL. They are
   never stored in runtime snapshots, task metadata, or logs.
4. Probe logs must state whether the source was command or peer and include
   duration, but must not log command output beyond a bounded sanitized error.
5. The WebUI should show source, freshness timestamp, available values, and an
   error/stale indicator beside existing resource and sensor information.
6. A resource endpoint with `peer_resources.auth_mode: "disabled"` exposes only
   the intentionally minimal public resource response. Operators are
   responsible for enabling `endpoint` mode outside a trusted network.

## Implementation Plan

1. Extend resource configuration validation and `Resource` runtime state with
   availability configuration, observation generations, and public snapshots.
2. Extract a shared availability probe service used by both scheduler admission
   and `ResourceMonitor`.
3. Add atomic multi-resource refresh/admission/reservation APIs to `Resources`;
   migrate scheduler dispatch to use them.
4. Add OpenAIx resource endpoints with configurable peer-resource access and
   regression tests.
5. Add the in-memory peer capability client/cache and route peer refreshes
   through it.
6. Integrate the new availability fields into WebUI/API resource displays.
7. Extend smart-route diagnostics so a rejected candidate records unavailable,
   stale, or probe-error resource state without leaking secrets.

## Validation

The implementation must add focused tests for:

1. numeric command output, timeout, non-zero exit, malformed output, and an
   unavailable metric;
2. command precedence over a configured eligible peer;
3. periodic availability and sensor polling under one resource interval;
4. two simultaneous admissions after one observation, proving that only
   post-observation reservations are subtracted;
5. no partial reservation when a multi-resource admission fails;
6. fallback behavior for a resource with no command and no eligible peer;
7. successful, unauthorized, incompatible, and transient-failure peer
   discovery, including retry after a transient failure and reboot-lifetime
   caching only for definitive incompatibility;
8. mandatory peer refresh before a remote dispatch;
9. redaction of commands, URLs, credentials, consumer IDs, and internal
   bookkeeping from peer/API snapshots;
10. smart-route rerouting when a candidate's observed resource is unavailable.

## Phased Implementation Plan

This work must not be delivered as one phase. The local scheduling change,
inter-instance protocol, and presentation/authentication policy have independent
failure modes and need separately deployable regression coverage.

### Phase 1: Local observed availability

**Status: implemented.** Command-backed runtime state, periodic refresh,
pre-dispatch refresh, fail-closed admission, atomic multi-resource reservation,
configuration examples, and focused tests are implemented.

1. Add command availability configuration and public runtime state to
   `Resource`.
2. Reuse `ResourceMonitor` for command refreshes during its normal polling
   cycle and immediately before scheduler admission.
3. Make command-backed resources fail closed after a failed pre-dispatch probe,
   while retaining periodic recovery attempts.
4. Preserve reservation accounting only as a post-observation concurrency
   overlay, including multi-resource admission safety.
5. Add configuration examples and focused resource, monitor, and scheduler
   tests.

**Exit criterion:** a configured local availability command controls task
admission and public status without regressing estimated-only resources.

### Phase 2: aidir peer resource protocol

**Status: implemented.** OpenAIx resource endpoints, configurable access mode,
definitive-negative capability caching, transient retry behavior, and monitor
integration for periodic and pre-dispatch peer probes are implemented.

1. Add the minimal `GET /v1/resources` and `GET /api/resources` OpenAIx
   extensions, followed by single-resource variants.
2. Implement `peer_resources.auth_mode`, defaulting to `disabled`.
3. Add provider capability discovery, definitive-negative reboot-lifetime
   caching, and transient-failure retry behavior.
4. Refresh peer-backed resource state before remote dispatch and during the
   existing monitor cycle.
5. Add endpoint, authentication, client, and smart-routing tests.

**Exit criterion:** two aidir instances can exchange sanitized resource
availability and route work using fresh remote capacity.

### Phase 3: Operator experience and telemetry extension

**Status: implemented.** The WebUI shows source, freshness, errors, observed
capacity, and whitelisted telemetry. Peer responses carry the versioned
telemetry extension with only explicitly selected safe sensor readings.

1. Add WebUI/API presentation for source, freshness, errors, and observed
   capacity.
2. Expose the versioned empty telemetry contract and add safe optional
   temperature telemetry.
3. Add migration guidance for `peer_resources.auth_mode: "endpoint"` and
   prepare the future major-version default change.
4. Run focused integration coverage and update end-user documentation.

**Exit criterion:** operators can diagnose local and peer resource state without
seeing commands, credentials, or internal scheduling data.
