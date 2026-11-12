# Roadmap

Status: active. This file holds deferred work and ideas that are not yet ready
for a proposal. Items move out of here only when they get a concrete design.

## Deferred security work

### W3: WebUI authentication without transmitting the password

Status: deferred. The former interim `sha256$<hex>` proposal is withdrawn.

Current state: `webui.auth.users[].password` is stored in plaintext, the login
endpoint receives the password, and failed attempts are not rate-limited
(`webui/backend/app.py:691-698`, `webui/backend/app.py:860-876`).

Preliminary direction:

- Enroll each WebUI user with a per-user random salt. The browser derives a
  deterministic signing key pair from the password and the salt, and sends only
  the public key; the server stores `login`, `salt`, KDF parameters and the
  public key. It stores no password and no password hash.
- At sign-in the server issues a short-lived random challenge. The browser
  derives the private key from the entered password and the stored salt, signs
  the challenge, and sends the signature. The server verifies it with the
  public key and then creates the normal session cookie.
- The password never leaves the browser. TLS is still mandatory: it protects
  the session cookie, the public key and the signatures, not the password.
- The signed value must include a fresh server challenge, not only the static
  salt; otherwise a captured signature can be replayed.
- Login rate limiting is still required on the challenge and verification
  endpoints, by login name and by client address.

Open design questions:

- KDF and signature algorithm. A memory-hard KDF (Argon2id or scrypt) is
  preferable; WebCrypto support and a PBKDF2 fallback need to be checked. The
  signature scheme also needs a browser-support decision.
- Enrollment and migration. Existing plaintext users cannot silently become
  key-pair users without entering their password once. Decide whether the first
  successful login enrolls the key pair, or whether an administrator performs
  an explicit enrollment. The old plaintext field must be removable afterward.
- Enrollment authorization. Accepting a public key is a credential replacement.
  The enrollment action must be authenticated (an administrator session or the
  old credential), otherwise an attacker could install their own key for an
  account. The initial enrollment and later rotation need separate rules.
- Recovery and rotation. A forgotten password cannot be reset from a password
  hash because none is stored. The administrator must be able to issue a new
  enrollment. Password change and key rotation need a defined flow.
- Multiple browsers and devices. Decide whether each device enrolls its own key
  pair or whether the private key is deterministically reconstructed on every
  device from the password and salt.
- What the server can still learn. This is not a PAKE. A stolen public key and
  salt permit an offline dictionary attack. A PAKE such as OPAQUE or SRP would
  avoid that, but it is a larger change. XSS in the WebUI can still capture the
  password before it is used.
- Redis failure and clock skew. The challenge lifetime and the behavior when
  Redis is unavailable must be explicit; login already depends on Redis for the
  session.

Acceptance criteria:

- a password-derived key pair can enroll and authenticate without the password
  appearing in any request body, URL, log or server-side store;
- a replayed, expired or wrong-challenge signature is rejected;
- a wrong password fails without revealing whether the login exists;
- rate limiting and generic error responses survive the new flow;
- existing users have a documented migration path and a way to recover;
- the design has a written threat model before implementation.

Source: `proposal_update4.md` W3 / `proposal_update1.md` U22.

### W4: `web_fetch` SSRF guard

Status: deferred.

Reject loopback, private, link-local and metadata targets, and re-check after
every redirect. The item is in scope but needs a design for the allowed target
policy, DNS rebinding and the interaction with explicitly configured local
services.

Source: `proposal_update4.md` W4 / `proposal_update1.md` U23.

### W5: `exec_cmd` through the settings editor

Status: deferred.

The settings editor can currently write `exec_cmd`; a write is a
command-execution chain. Decide between a dedicated permission and moving the
value out of the editable document into the environment. This changes the
settings contract and needs its own proposal.

Source: `proposal_update4.md` W5 / `proposal_update1.md` U27.

### W7: CSRF token

Status: deferred.

Add a double-submit token to every state-changing WebUI request. The current
protection is `samesite=strict`. The design must cover the settings editor, the
WebSocket routes and API clients that do not use cookies.

Source: `proposal_update4.md` W7 / `proposal_update1.md` U20.

## Deferred validation and tooling

### W31: OpenCode Go release validation

Status: deferred from `proposal_update4.md` when Round 4 closed.

The provider transport, routing, session policy, health probe and configuration
validation are implemented. The remaining release gates are the focused
managed-session and redaction tests, the health-probe regression, the error-code
normalization audit, and the redacted synchronous and streaming fixtures with a
live `/v1/models` + Chat Completions + SSE smoke test against a real OpenCode Go
deployment.

Tracking: `proposal_opencode_go.md` remains the release-gate document.

Source: `proposal_update4.md` W31 / `proposal_update1.md` U38.

### W47: Browser acceptance of the generated editor

Status: deferred from `proposal_update4.md` when Round 4 closed.

The acceptance pass needs a person to open the generated settings editor through
nginx, exercise the tree, field validation, apply policies, secret handling and
the restart banner, and record the result. The procedure stays in `TEST.md` 7.1.

Source: `proposal_update4.md` W47 / `plan_aiset_integration.md`.

### W48: Example configuration sets and installation scripts

Status: deferred from `proposal_update4.md` when Round 4 closed (P3).

`install.sh` already accepts a template name resolved as
`<name>.json5.example`. The remaining work is to add several selectable example
sets and the installation scripts for a full configuration set (aidir + sndbx +
ollama). The notes and the target sequence stay in `TODO.md`.

Source: `proposal_update4.md` W48 / `TODO.md`.

## Deferred usage and billing work

Source: `proposal_billing.md`.

### B1: OpenCode Go provider allowance view

Status: deferred to a future version.

OpenCode Go exposes an undocumented read-only route, `GET {baseUrl}/v1/usage`,
that returns rolling, weekly, and monthly allowance percentages with reset
times. The first release does not poll it. A later version may poll it on a
bounded interval to show the provider view beside the locally computed limits.
The route is not a public contract, so the poll stays best-effort and must
never replace the local counters. The provider view also needs a staleness
limit, after which the UI must mark it unavailable.

### B2: Percentile latency and throughput

Status: deferred to a future version.

The first release stores weighted averages only. A later version may add p50 and
p95 latency and throughput. That requires bounded histograms, and the schema
must be extended with them when the work starts. The current aggregate schema
does not reserve space for them.

### B3: Richer price sets

Status: deferred to a future version.

The first release has one price per token kind and an optional context-size
tier. A later version may add time-of-day prices, for example peak and
off-peak, a separate cache-write price, and prices for image, audio, and
embedding units. Each of them needs its own unit definition before it can be
stored.

### B4: Raw per-call retention and re-aggregation

Status: deferred to a future version.

The first release aggregates on every call and keeps no raw per-call statistics
table, so a statistics bug cannot be repaired by re-aggregation. A later
version may add an optional raw table with a retention window, and it must be
opt-in because it multiplies the storage.

### B5: Batched usage writes

Status: deferred to a future version.

The first release writes one small transaction per completed LLM call. If the
write rate becomes a problem, the write path moves to a batching writer
without a schema change. The metering-health counter must show the difference.

### B6: Per-envid exposure

Status: deferred to a future version.

The first release collects the usage per envid in the usage store and stores
the calls without an envid in the empty bucket. The data is not exposed. A
later version may add the per-envid rows to the WebUI and to the usage
endpoints. That needs a decision on privacy, on the display of the empty
bucket, and on label cardinality.

### B7: Cost-aware and speed-aware routing

Status: deferred to a future version.

The first release excludes a smart candidate whose limit is exhausted and
nothing else. Cost, cost per successful answer, cached-input cost, speed, and
a normalized quality score do not influence the routing yet. A later version
may add a tie-break that prefers a cheaper or faster candidate when quality
and availability are otherwise equal. The signals exist in the usage store by
then.

### B8: `/v1/models` usage snapshot and switch

Status: deferred to a future version.

The first release builds the `/v1/models` usage block on every request. A
later version may keep a snapshot of the usage summary and add a query
parameter that turns the usage block off, so a client that does not need it
keeps the small old response.

### B9: More failure classes

Status: deferred to a future version.

The failure taxonomy of the first release covers the classes that the existing
error codes and the provider shapes give for free. A later version may split
the coarse classes further, for example a canceled call into a client
disconnect and a service restart, or the tool-loop stop into a length stop and
a loop limit. The taxonomy stays open, and an unknown shape falls into
`other_error`.

## Deferred architecture

- The middleware chain and the `before_tools` extension. They are recorded in
  `SPEC.md` §14.4 and need their own proposals.
