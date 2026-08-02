# IAC Agent Extranet Protocol

Status: pre-implementation specification, version `0.1`  
Primary repository: `lehelkovach/iac-bus`  
Tracked by: IAC Bus issue #10

## Product thesis

IAC Bus is an authenticated control and evidence plane for agents working across trust boundaries. It binds a human or organizational mandate to agent delegation, exact action approval, execution, and a verifiable receipt.

The core causal chain is:

```text
human mandate
  -> agent grant
  -> attenuated sub-agent grant
  -> exact action proposal
  -> policy decision / approval
  -> target invocation
  -> execution receipt and effect evidence
```

IAC Bus does not replace A2A, MCP, OAuth, SPIFFE, AGNTCY, OpenTelemetry, or KSG. It composes them into an accountable agent extranet.

## Scope

An agent extranet is a bounded collaboration domain whose members, trusted issuers, policies, and data rules are explicit. It is not a permissionless global network.

In scope for `0.1`:

- signed and versioned authority, proposal, decision, approval, and receipt objects;
- stable verified principal and agent identifiers;
- monotonic delegation: child authority can only be narrower;
- expiry, revocation, nonce, audience, resource, and action checks;
- delivery receipts distinct from execution receipts;
- causal and distributed tracing fields;
- KSG semantic/procedure references that never grant permission;
- transport-neutral envelopes usable over the existing HTTP bus, A2A, or MCP adapters;
- repository coordination using leases, reservations, and fencing tokens.

Out of scope for `0.1`:

- a custom blockchain, token, consensus system, or BlockDAG;
- a new global identity or agent-discovery network;
- universal proof of unique humanity;
- custody of third-party credentials inside messages;
- treating semantic similarity, LLM judgment, or conversational consent as authorization;
- replacing Git branches, worktrees, reviews, and merge controls.

## Component boundaries

| Component | Owns | Does not own |
| --- | --- | --- |
| IAC Bus | delivery, queues, leases, causal events, verified envelopes, execution evidence | root human identity, secrets, semantic truth |
| KeyChain-lite | grant issuance, presentation, attenuation, revocation, agent-key isolation | message routing, tool execution |
| HumanKey-lite | human-controlled approval, root mandate, recovery, conspicuous-use confirmation | agent transport, universal personhood |
| Patronus / ShowGo | user-facing action preview, policy UX, agent execution, receipts | foundational identity or consensus |
| KSG | semantic objects, procedures, lineage, evidence ingestion | runtime authorization |
| ComputeNet | future execution-market adapter, if demand is proven | MVP execution environment |
| GovernChain | future governed protocol-change process | a separate blockchain |

## Standards profile

Use existing standards at the edge and normalize them internally:

- A2A for agent tasks, messages, artifacts, streaming, Agent Cards, and `AUTH_REQUIRED` state;
- MCP for agent-to-tool invocation and its OAuth resource-server rules;
- OAuth 2.0/OIDC with Resource Indicators, Rich Authorization Requests, Token Exchange, and DPoP where applicable;
- SPIFFE/SPIRE for short-lived workload identity in managed infrastructure;
- W3C Verifiable Credentials or DIDs as optional credential/key adapters;
- AuthZEN-compatible policy decision requests and responses;
- RFC 8785 JSON canonicalization with detached JWS/Ed25519 signatures initially;
- W3C Trace Context and OpenTelemetry for observability;
- append-only event storage with signed Merkle checkpoints; Rekor or SCITT anchoring only later.

No adapter is mandatory for a local deployment. The internal contracts stay stable while authentication adapters vary.

## Security invariants

An implementation is non-conformant if it violates any of these rules:

1. A display handle or caller-supplied `sender` value is never a security identity.
2. The bus derives the actor from a verified key/session binding.
3. Every protected action has an unexpired grant for the exact audience, resource, and capability.
4. A child grant cannot widen any parent constraint.
5. Revocation and expiry are checked again immediately before execution.
6. The executor proves possession of its own key; the LLM never receives private keys.
7. An approval signs the canonical proposal digest, not prose describing the proposal.
8. The executor hashes the final target request and rejects approval/execution drift.
9. Target-service credentials are audience-bound and are never passed through an agent chain.
10. Delivery proves carriage only. Execution and observed effects require separate evidence.
11. KSG references can add meaning or lineage but cannot add authority.
12. Trace identifiers are correlation data, not credentials.
13. A stale lease holder cannot write after another worker takes over; fencing tokens are enforced at the write boundary.
14. Receipts attribute claims. They do not, by themselves, prove that a claim is correct.

## Identity and extranet membership

The internal principal binding normalizes local keys, OIDC identities, SPIFFE IDs, or DID/VC identities:

```json
{
  "principal_id": "urn:iac:principal:01J...",
  "kind": "human",
  "external_id": "https://issuer.example/subjects/123",
  "key_id": "did:key:z6Mk...#z6Mk...",
  "extranet_id": "urn:iac:extranet:project-7",
  "status": "active",
  "valid_until": "2026-08-02T18:00:00Z"
}
```

An agent binding also records its controller and build identity:

```json
{
  "agent_id": "urn:iac:agent:01J...",
  "controller_principal_id": "urn:iac:principal:01J...",
  "key_id": "did:key:z6Mk...#agent-key",
  "build_digest": "sha256:...",
  "endpoint": "https://agents.example/showgo",
  "extranet_id": "urn:iac:extranet:project-7"
}
```

Human-readable handles remain aliases. Pairwise identifiers should be used between extranets when linkability is undesirable.

## Authority grant

A grant is explicit, signed, revocable, audience-bound, and attenuable:

```json
{
  "schema": "iac.authority-grant/0.1",
  "grant_id": "urn:iac:grant:01J...",
  "issuer": {"principal_id": "urn:iac:principal:human-123", "kind": "human"},
  "subject_agent_id": "urn:iac:agent:supervisor",
  "capabilities": ["repo.pull_request.open_draft"],
  "resources": ["github:acme/repo/path/src/**"],
  "constraints": {
    "deny": ["merge", "force_push", "delete_branch"],
    "max_cost_usd": 5,
    "max_uses": 20,
    "delegation_depth": 1,
    "requires_approval_for": ["repo.pull_request.open_draft"]
  },
  "issued_at": "2026-08-02T14:00:00Z",
  "expires_at": "2026-08-02T18:00:00Z",
  "nonce": "base64url-random",
  "status_ref": {"url": "https://keychain.example/status/grant", "checked_at": "2026-08-02T14:00:00Z"},
  "digest": "sha256:...",
  "signature": {"algorithm": "Ed25519", "key_id": "key:human:1", "value": "..."}
}
```

Delegated grants add `parent_grant_digest`. The verifier walks the complete chain and checks that capabilities, resources, audiences, time, use count, cost, and delegation depth are equal or narrower at every link.

## Exact action proposal and approval

An action proposal pre-commits the material action before side effects occur:

```json
{
  "schema": "iac.action-proposal/0.1",
  "proposal_id": "urn:iac:proposal:01J...",
  "principal_id": "urn:iac:principal:human-123",
  "actor_agent_id": "urn:iac:agent:worker-7",
  "capability": "repo.pull_request.open_draft",
  "resource": "github:acme/repo",
  "input_digest": "sha256:canonical-parameters",
  "risk": "moderate",
  "requested_at": "2026-08-02T14:53:59Z",
  "expires_at": "2026-08-02T15:05:00Z",
  "authority": {"grant_digest": "sha256:...", "verifier": "keychain.example", "checked_at": "2026-08-02T14:53:59Z"},
  "semantic_refs": [{"object_id": "ksg:procedure:open-draft-pr", "version": "7", "relation": "procedure"}],
  "digest": "sha256:..."
}
```

Policy evaluation uses `subject + resource + action + context -> decision`. If approval is required, the separate approval surface displays the normalized resource, action, material parameters, predicted effects, cost, and expiry. It signs only the proposal digest:

```json
{
  "schema": "iac.approval-record/0.1",
  "approval_id": "urn:iac:approval:01J...",
  "proposal_digest": "sha256:...",
  "approver": {"principal_id": "urn:iac:principal:human-123", "kind": "human"},
  "decision": "approved",
  "conditions": {"max_uses": 1},
  "decided_at": "2026-08-02T14:54:00Z",
  "expires_at": "2026-08-02T15:05:00Z",
  "digest": "sha256:...",
  "signature": {"algorithm": "Ed25519", "key_id": "passkey:human-123", "value": "..."}
}
```

## Canonical envelope

All protected messages use the `ExtranetEnvelope` in `schemas/agent-extranet.schema.json`.

```json
{
  "protocol": "iac-extranet/0.1",
  "message_id": "018f...",
  "message_type": "action.propose",
  "from_agent": {
    "agent_id": "urn:iac:agent:worker-7",
    "controller_principal_id": "urn:iac:principal:human-123",
    "key_id": "did:key:z6Mk...#worker"
  },
  "to_agent_id": "urn:iac:agent:executor",
  "conversation_id": "context-77",
  "causation_id": "018e...",
  "sent_at": "2026-08-02T14:53:59Z",
  "expires_at": "2026-08-02T15:05:00Z",
  "idempotency_key": "context-77:proposal-1",
  "trace": {"traceparent": "00-...-...-01"},
  "authority": {"grant_digest": "sha256:...", "verifier": "keychain.example", "checked_at": "2026-08-02T14:53:59Z"},
  "payload": {},
  "payload_digest": "sha256:...",
  "signature": {"algorithm": "Ed25519", "key_id": "did:key:z6Mk...#worker", "value": "..."}
}
```

Compatibility mapping for current IAC fields:

| Existing field | Protocol meaning |
| --- | --- |
| `sender` | derived display alias only |
| `agent_id` | verified actor/agent principal |
| `conversation_id` | A2A `contextId` / correlation identifier |
| `reply_to` | causal predecessor |
| `channel` | routing metadata only |
| bearer token | deployment authentication only; not delegated action authority |

## Receipts and evidence

A delivery receipt reports accepted, delivered, expired, or rejected carriage. It says nothing about action effects.

An execution receipt binds:

- proposal, grant, policy decision, and approval digests;
- verified actor and exact capability/resource;
- start/end time and status;
- canonical result digest;
- claimed effects separately from independently observed effects;
- immutable artifact references and digests;
- trace and KSG procedure/evidence references;
- executor signature.

Useful independent evidence includes a target API response digest, commit SHA, artifact hash, test attestation, or separately observed state. KSG may ingest a receipt as evidence about a procedure; it does not automatically become semantic truth.

## Agent work lifecycle

1. Discover an endpoint through configuration, a signed A2A Agent Card, or an external directory.
2. Admit a principal/agent into one extranet under trusted-issuer policy.
3. Authenticate the session and bind it to a verified principal key.
4. Issue a bounded root grant from a human or organization.
5. Delegate a narrower child grant when another agent needs work.
6. Offer/claim work and acquire scoped path or resource reservations.
7. Create the exact action proposal.
8. Verify signatures, grant chain, audience, resource, capability, time, revocation, nonce, and policy.
9. Obtain digest-bound approval outside the agent context when required.
10. Acquire/renew a lease with a monotonically increasing fencing token.
11. Re-hash and execute the final request only if it still matches the approval.
12. Sign an execution receipt, attach evidence, and ack/nack the work item.
13. Append causal events to durable storage and release the lease.
14. Optionally submit the receipt to KSG as procedural evidence.

Git branches, worktrees, protected paths, CI, and pull requests remain authoritative for source changes. IAC reservations reduce collisions but do not replace version control.

## A2A and MCP adapters

### A2A

Publish this profile as an A2A extension. Map A2A `contextId` to `conversation_id`, the A2A Task ID to IAC task metadata, `AUTH_REQUIRED` to the need for an approval/credential, and A2A Artifacts to receipt artifact references. Carry IAC grants, proposal digests, approvals, and receipts in extension-scoped metadata.

### MCP

Wrap each protected MCP invocation in propose -> evaluate/approve -> invoke -> receipt. Follow MCP OAuth rules, request a target-specific token, and never pass a client token through to another service. Tool annotations remain descriptive metadata, not policy.

## Persistence and transparency

The first durable ledger should be ordinary SQL:

- immutable events table with canonical event bytes and content digest;
- causal predecessor and correlation indexes;
- grants, revocations, nonces/idempotency keys, leases/fencing tokens, and receipts;
- per-tenant access controls and encrypted sensitive fields;
- signed periodic Merkle roots and inclusion proofs.

Later, checkpoint roots may be submitted to Rekor or a SCITT-compatible transparency service. Private event data is never published. A custom chain requires proven independent operators, adversarial federation, and a measured need that SQL plus checkpoints cannot meet.

## Required runtime verification order

For every protected action:

1. parse and schema-validate;
2. enforce supported protocol/schema version;
3. canonicalize payload and compare digest;
4. verify envelope signature and key binding;
5. reject expired/replayed event and enforce idempotency;
6. resolve grant and verify the full signature/delegation chain;
7. check revocation epoch/status immediately;
8. match actor, audience, capability, and canonical resource;
9. evaluate constraints and external policy;
10. verify digest-bound approval when required;
11. validate lease/fencing token;
12. canonicalize the final invocation and ensure it matches the proposal;
13. execute and produce a signed receipt.

Schema validation alone is never authorization.

## MVP acceptance test

The first vertical slice is complete when two coding agents can safely coordinate one repository task:

- a human issues a short-lived grant to a supervisor;
- the supervisor delegates a narrower grant to a worker;
- the worker reserves explicit paths and claims the task;
- a destructive or externally visible action enters approval-required state;
- a separate approval signs the exact proposal digest;
- the executor rejects expired, revoked, replayed, widened, wrong-target, or digest-mismatched requests;
- successful execution returns a signed receipt with commit/test/PR evidence;
- a second worker with a stale fencing token cannot write;
- all causal events survive a process restart.

## Phased delivery

### Phase 1: trustworthy local bus

Canonical objects, Ed25519 signatures, SQLite event ledger, nonces/idempotency, revocation, expiry, proposals, approvals, receipts, and fencing tokens.

### Phase 2: real human authority

HumanKey-lite passkey/OIDC mandate, KeyChain-lite grant wallet, AuthZEN adapter, resource-bound tokens, and an approval surface outside the LLM.

### Phase 3: Internet interoperability

A2A extension, signed Agent Card, MCP gateway, SPIFFE adapter, optional AGNTCY discovery, and OpenTelemetry.

### Phase 4: verifiable federation

Read-only mirrors, signed Merkle checkpoints, optional Rekor/SCITT anchoring, and only then evaluation of offline Biscuit/UCAN attenuation.

## Naming and positioning

One-sentence positioning:

> IAC Bus lets people and organizations delegate bounded authority to cooperating AI agents and get a signed, causal receipt for what actually happened.

This is not “another memory service,” “another agent chat protocol,” or “a blockchain for agents.” It is the accountability profile between human intent and agent side effects.
