# IAC Bus: Coding Agent Kickstart

Mission: deliver one trustworthy authority-to-receipt vertical slice without expanding IAC Bus into a universal agent platform.

## Read first

1. `README.md`
2. `AGENTS.md` if present on the working branch
3. `docs/AGENT-EXTRANET-PROTOCOL.md`
4. `schemas/agent-extranet.schema.json`
5. issue #10 and the current PR stack

Repository reality as of 2026-08-02:

- `master` is stale and has six open draft PRs;
- PR #9 is the live OCI baseline and reports 32 tests;
- PRs #6/#7 overlap PR #9 on server, README, smoke, and test-scaffold files;
- PR #5 carries the coordination scaffold;
- this protocol PR is intentionally stacked on PR #9 and initially owns only the four reserved files listed below.

Do not begin broad runtime work until the maintainer chooses/reconciles the baseline.

## Reserved scope for this branch

This branch owns only:

- `docs/AGENT-EXTRANET-PROTOCOL.md`
- `docs/CODING-AGENT-KICKSTART.md`
- `schemas/agent-extranet.schema.json`
- `tests/test_agent_extranet_schema.py`

Do not edit `server.py`, the existing agent-contract schema, OpenClaw/OCI files, smoke scripts, or shared test fixtures on this branch.

## Non-negotiable constraints

- Preserve existing endpoints and message formats during the first implementation.
- Add versioned `/v2` behavior or isolated middleware; do not silently reinterpret legacy fields.
- No custom blockchain, cryptocurrency, BlockDAG, global directory, or new DID method.
- No private keys, refresh tokens, or target credentials in messages, logs, or LLM prompts.
- A shared bearer token cannot establish sender identity or delegated authority.
- Semantic/KSG references cannot authorize actions.
- Delivery and execution receipts stay distinct.
- All authority decisions use exact canonical action/resource identifiers.
- All implementation PRs include negative security tests.

## Architecture decision records to create before runtime code

Create short ADRs for:

1. baseline reconciliation and migration from PR #9;
2. RFC 8785 canonicalization library and signed-byte representation;
3. Ed25519/JWS library and key-storage boundary;
4. SQLite schema/migrations and append-only-event guarantees;
5. canonical resource URI grammar and matcher;
6. delegation attenuation algorithm;
7. revocation/status caching and fail-closed behavior;
8. lease fencing semantics;
9. compatibility strategy for legacy `sender`, `conversation_id`, and bearer auth.

## Work packages

Each package should be a separate, reviewable PR unless a maintainer explicitly combines adjacent packages.

### WP0 — consolidate the active baseline

Owner files: integration/reconciliation only.

- compare PR #9 with #7, #5, and #3;
- preserve the deployed OCI fixes from #9;
- carry forward CI/auth corrections and the coordination scaffold;
- close or label superseded PRs;
- verify deployment source equals repository source;
- run the complete existing suite before adding v2 behavior.

Exit: one agreed development branch with no unexplained production/source divergence.

### WP1 — contract and conformance fixtures

Owner files: `schemas/agent-extranet.schema.json`, conformance fixtures, schema tests.

- review the `0.1` schema against the protocol;
- add valid fixtures for grant, proposal, decision, approval, envelope, delivery receipt, and execution receipt;
- add invalid fixtures for missing authority, empty scope, unknown versions, wrong digest shape, and unrecognized decisions;
- publish a tiny validator entry point for SDK use;
- state clearly which checks require runtime logic.

Exit: schema suite passes in CI and fixtures can be consumed by another language.

### WP2 — canonical bytes and signatures

Owner files: new isolated `iac_security/` or equivalent plus tests.

- RFC 8785 canonical JSON;
- SHA-256 digest helpers;
- Ed25519 signing/verifying with `key_id` resolution;
- signed version binding so downgrade/field-stripping fails;
- key-provider interface; test keys only in fixtures;
- verify digest before policy evaluation.

Negative tests: byte changes, wrong key, unknown key, algorithm confusion, unsupported version, malformed signature.

Exit: deterministic vectors pass across two independent invocations and preferably a second implementation.

### WP3 — authority verifier

Owner files: new isolated authority package and tests.

- grant issue, resolve, delegate, revoke;
- exact capability and canonical-resource matching;
- complete parent-chain validation;
- monotonic attenuation of scope, time, count, cost, risk, and depth;
- audience/key confirmation;
- expiry, nonce, max-use, and revocation checks;
- fail closed if status cannot be verified for a protected action.

Negative tests: scope widening, later expiry, wrong subject, wrong audience, revoked parent, stale status, replay, path-normalization tricks.

Exit: every invariant is covered by a failing-then-passing test.

### WP4 — durable causal ledger

Owner files: new persistence package, migrations, tests.

- SQLite WAL mode initially;
- immutable canonical event bytes and content digest;
- unique event, nonce, and idempotency constraints;
- causation and correlation indexes;
- grant/revocation/use accounting;
- proposals, approvals, executions, receipts;
- process-restart and concurrent-writer tests;
- signed Merkle checkpoint interface; no external anchoring yet.

Exit: an accepted event and its provenance survive restart, and duplicate/replayed writes are deterministic.

### WP5 — policy, approval, and invocation

Owner files: new `/v2` routes/service layer and tests.

Recommended endpoints:

```text
POST /v2/grants
POST /v2/grants/{id}/delegate
POST /v2/grants/{id}/revoke
POST /v2/actions/propose
POST /v2/approvals
POST /v2/invocations
POST /v2/receipts
GET  /v2/provenance/{event_id}
```

- normalize policy input as subject/resource/action/context;
- create policy-adapter interface with a deterministic built-in implementation;
- make approval conditions and TTL explicit;
- bind approval to proposal digest;
- re-hash the final invocation before side effects;
- record claimed effects separately from verified evidence.

Exit: the action lifecycle works locally and bait-and-switch tests fail closed.

### WP6 — leases and safe parallel coding

Owner files: isolated lease/reservation module and integration tests.

- scoped resource/path reservations;
- lease acquisition, renewal, release, and expiration;
- monotonically increasing fencing token per protected resource;
- executor rejects stale tokens at the mutation boundary;
- Git branch/worktree/PR identifiers in receipts;
- guidance that IAC reservations are advisory until an actual write boundary checks the token.

Exit: worker B can take over an expired lease and worker A can no longer write.

### WP7 — HumanKey/KeyChain-lite adapters

Begin only after WPs 1–5 stabilize.

- passkey or OIDC human root mandate;
- isolated agent key provider;
- grant wallet and revocation/status endpoint;
- conspicuous-use event hook;
- approval UI/API outside the LLM context;
- no dependency from HumanKey core onto IAC transport.

Exit: a real human-controlled authenticator approves an exact proposal without exposing signing material to the agent.

### WP8 — A2A/MCP interoperability

Begin only after local conformance passes.

- signed A2A Agent Card and IAC extension declaration;
- map A2A task/context/artifact state to IAC objects;
- MCP gateway that obtains a separate audience-bound target token;
- never pass through the incoming token;
- OpenTelemetry trace propagation and span links;
- optional SPIFFE and AGNTCY adapters, not new core dependencies.

Exit: one A2A agent and one MCP tool complete a grant-to-receipt flow.

## First end-to-end scenario

Use one deliberately narrow example: two coding agents opening a draft PR.

1. Human grants supervisor `read`, `edit`, `test`, and `open_draft_pr` for one repository/branch; merge and force-push denied.
2. Supervisor delegates only `edit` and `test` on two paths to worker.
3. Worker claims the task and obtains a fencing token.
4. Worker produces a patch and test evidence.
5. Supervisor proposes `open_draft_pr` with canonical title/body/head/base digest.
6. Policy requires human approval.
7. Human approves that digest through a separate surface.
8. Executor verifies the final request digest, opens a draft PR, and signs a receipt containing commit SHA, tests, and PR URL/ID.
9. A replay, wrong repo, widened path, expired grant, revoked grant, altered PR body, and stale fencing token are all rejected.

## Definition of done

- Existing legacy tests still pass.
- New conformance and negative-security tests pass.
- No secrets appear in captured logs or fixtures.
- Restart and concurrency tests pass.
- Protocol, OpenAPI/endpoints, and runtime behavior agree.
- A fresh coding agent can run the demo from documented commands.
- The receipt reconstructs the complete causal chain without trusting mutable logs.
- PR includes migration/rollback notes and names all deferred work.

## Agent reporting template

Every coding agent reports:

```text
Goal:
Branch / base SHA:
Reserved files/resources:
Grant / authority assumptions:
Changes made:
Tests run and exact results:
Security cases covered:
Known gaps:
Artifacts / commit / PR:
Receipt or evidence references:
```

Do not report “done” without exact tests and immutable artifact identifiers.
