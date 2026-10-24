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

## Deferred architecture

- The middleware chain and the `before_tools` extension. They are recorded in
  `SPEC.md` §14.4 and need their own proposals.
