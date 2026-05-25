# IAC-Bus Full Development Plan (Assimilated from Merged PRs + ACP Plan)

## 1) Intent and Inputs
This plan consolidates:

- merged PR #1 intent: queue-style work leasing, protocol formalization,
  orchestration foundations, and deployment guidance,
- merged PR #2 intent: standard, comprehensive documentation structure,
- existing ACP planning docs:
  - `docs/ACP_DEV_PLAN.md`
  - `docs/ROADMAP.md`
  - `docs/TESTING_STRATEGY.md`
  - prompt scaffolding in `prompts/`.

Goal: provide one practical, execution-ready plan from current codebase state to
an MVP that mitigates coordination drift in multi-agent, multi-repo workflows.

## 1.1) OSL/OpenClaw Pivot Constraints (Generic Bus Mode)
For the OSL/OpenClaw + procedural-agent pivot, IAC-Bus must remain
application-agnostic.

IAC-Bus should not embed KSG-, OpenClaw-, CPMS-, or repo-specific business
logic. These systems are downstream consumers or adjacent layers.

Layer separation:

- IAC Bus: generic coordination/event stream
- KSG: semantic memory/procedures/evidence
- OpenClaw: runtime and tool execution
- CPMS: affordance matching and UI prototype interpretation
- Slack: human notification surface
- GitHub: source, branches, PR workflow

Non-goals for this pivot profile:
- full workflow engine
- complex scheduler semantics beyond lightweight queue + polling
- semantic memory in bus core
- KSG-specific storage contracts in bus core
- OpenClaw execution logic in bus core

## 2) Current Baseline (Code-Verified)
Implemented today (runtime and tests):

- core message publish/poll:
  - `POST /bus/messages`
  - `GET /bus/messages`
  - `GET /health`
- queue work leasing:
  - `POST /bus/queues/claim`
  - `POST /bus/queues/ack`
  - `POST /bus/queues/nack`
- queue visibility controls:
  - polling excludes queue items by default
  - `include_queue=true` to include queue items
- orchestration foundations:
  - `orchestration.job` and `orchestration.step.status` message handling
  - assignment message dispatch for ready steps
  - job state introspection endpoints
- protocol schemas and orchestration schemas in `schemas/`
- test coverage for auth, queue leasing, schema validation, and orchestration

## 2.1) Minimum Generic Coordination Profile (Required)
To support the pivot without coupling IAC-Bus to a specific application stack,
the following capability profile is required.

Required generic message types:

- `info`
- `progress`
- `request`
- `response`
- `blocker`
- `handoff`
- `done`
- `decision`
- `heartbeat`
- `error`

Required channel naming conventions:

- `ops`
- `project.<project-name>`
- `repo.<repo-name>`
- `task.<task-id>`
- `agent.<agent-id>`
- `handoff`
- `blockers`

Required message envelope (generic):

- required top-level: `channel`, `agent`, `type`, `message`
- optional top-level: `ref`, `metadata`

Compatibility note:
- current runtime uses `sender`; v0.1 protocol hardening should accept/emit
  `agent` as canonical while preserving compatibility with `sender` during
  transition.

Read API requirements:
- `GET /bus/messages` supports: `channel`, `since_id`, `limit`, `wait_seconds`
- long-poll behavior with `wait_seconds` for lightweight worker synchronization

CLI/script requirements:
- canonical examples for post/read/wait behaviors under repository-owned client
  helpers (not external project wrappers only)

Slack bridge requirements (optional component, not bus-core coupling):
- poll one or more bus channels
- post selected messages to Slack webhook
- filter noisy events by type
- support dry-run mode
- never post secrets
- default posted types: `blocker`, `handoff`, `done`, `decision`, `error`

## 3) Product Direction (Positioning)
IAC-Bus is:

- coordination infrastructure for autonomous systems,
- dependency-aware orchestration runtime,
- provenance and operational synchronization layer.

IAC-Bus is not:

- a general chat platform,
- a social network for agents,
- or a replacement for human messaging tools.

## 4) Core Delivery Principle
Prioritize practical orchestration pain:

1. prevent coordination drift,
2. propagate dependency changes reliably,
3. preserve causal/provenance history,
4. protect critical sections with safe locking semantics,
5. make behavior observable and testable.

## 5) Workstreams (Easiest -> Most Complex)

### W0: Documentation Truth and Canonicalization
Why first: avoids execution drift and rework.

Deliverables:
- designate `docs/FULL_DEV_PLAN.md` as canonical execution plan
- keep `docs/ROADMAP.md` as version roadmap
- keep `docs/TESTING_STRATEGY.md` as quality gate policy
- align README and DOCUMENTATION links to these canonical docs
- maintain explicit labels: `Implemented`, `Planned`, `Proposed`

Exit criteria:
- no core feature described as implemented unless code + tests confirm it
- no duplicate conflicting roadmap statements left unresolved

### W1: Protocol Hardening (v1 Envelope)
Deliverables:
- freeze event envelope fields for coordination use-cases:
  - `event_id`, `correlation_id`, `parent_event_id`
  - `actor_type`, `actor_id`, `source_medium`, `action_type`
  - `repo`, `branch`, `payload`
- include required generic coordination fields:
  - `channel`, `agent`, `type`, `message`
  - optional `ref`, `metadata`
- define message-type taxonomy and validation for:
  - `info`, `progress`, `request`, `response`, `blocker`, `handoff`, `done`,
    `decision`, `heartbeat`, `error`
- define and document channel naming conventions:
  - `ops`, `project.*`, `repo.*`, `task.*`, `agent.*`, `handoff`, `blockers`
- add protocol version and compatibility notes
- ensure schemas validate all public request/response surfaces

Borrowed model:
- CloudEvents-style normalized metadata envelope

Exit criteria:
- schema gate fully green
- backwards-compat tests for protocol evolution rules

### W2: Persistence Foundation (Durable Coordination State)
Deliverables:
- storage abstraction layer behind bus operations
- SQLite adapter (first)
- migrations and schema versioning
- Postgres adapter path (initial support)
- persistence for:
  - events
  - queue lease state
  - orchestration job state

Borrowed model:
- Temporal-style append-only history and replay thinking

Exit criteria:
- restart/recovery tests pass with no coordination state loss

### W3: Agent Identity and Presence
Deliverables:
- agent registry:
  - canonical `agent_uuid`
  - readable handles (for example `agent:cursor.repo.0@web`)
  - role/purpose/capabilities
  - parent/root relationship fields
- heartbeat/last_seen presence tracking

Exit criteria:
- presence TTL tests and registry API tests pass

### W4: Task/Dependency/Contract Layer
Deliverables:
- task model with ownership and status lifecycle
- dependency graph storage and DAG validation
- contract/spec entities linked to tasks
- blocker model and state transitions

Borrowed model:
- Argo-style DAG dependency handling

Exit criteria:
- dependency graph validation and state transition tests pass

### W5: Dependency Propagation + Provenance Queries
Deliverables:
- impact resolver:
  - contract change -> affected tasks -> affected agents
- automatic invalidation/blocker events
- provenance query endpoints:
  - why an action happened
  - what triggered a blocker
  - what chain caused invalidation

Exit criteria:
- end-to-end drift mitigation scenario passes in BDD tests

### W6: Lock Manager for Repo Critical Sections
Deliverables:
- lock APIs:
  - acquire / renew / release
- lease TTL behavior
- fencing token support
- path-scoped lock resources for repo/file critical sections

Borrowed model:
- etcd lease + CAS + fencing-token safety patterns

Exit criteria:
- concurrent contention tests demonstrate stale-writer prevention

### W7: Governance and Policy
Deliverables:
- scoped permissions and capability boundaries
- approval checkpoints for high-risk actions
- escalation policy hooks
- optional OPA-compatible policy decision point

Exit criteria:
- unauthorized action paths rejected in policy tests

### W8: Streaming and External Integrations
Deliverables:
- SSE (first) and optional WebSocket
- medium-aware routing/filtering
- bridge adapter pattern for external channels
- optional Slack webhook bridge:
  - channel polling
  - type filtering
  - dry-run mode
  - secret redaction/no-secret-post policy

Exit criteria:
- ordering, dedupe, and delivery behavior validated

### W9: Operational Maturity
Deliverables:
- observability (metrics, structured logs, trace hooks)
- backup/restore runbooks
- deployment hardening and upgrade playbooks

Exit criteria:
- restore drills and operational fault-injection checks pass

## 6) MVP Definition (First Useful Version)
MVP objective: prove coordination drift mitigation in a realistic multi-agent
workflow.

Required MVP capabilities:
- durable event ledger
- agent identity registry
- task/dependency core
- propagation engine
- provenance query support
- lease-based locking for critical paths
- generic coordination profile support:
  - required message types
  - required channel conventions
  - `wait_seconds` long-poll reads
  - canonical bus client examples (post/read/wait)

Required MVP scenario:
1. upstream contract/task change emitted
2. impacted downstream tasks detected
3. affected workers notified
4. stale work blocked/replanned
5. provenance explains entire chain

## 7) Testing and Quality Gates (Cascading)
Follow `docs/TESTING_STRATEGY.md` as release law:

- L0 Schema
- L1 Unit logic
- L2 Integration
- L3 BDD behavior
- L4 Concurrency/resilience

Feature completion rule:
- a feature is complete only when required gate layers pass and failure-mode
  tests exist (timeout/conflict/retry/restart path).

## 8) Next Execution Backlog (Immediate)
Ordered for earliest value with low rework:

1. Add canonical API surface table in `DOCUMENTATION.md` with
   implemented/planned labels.
2. Introduce event ledger abstraction and SQLite implementation.
3. Add `POST /events` and `GET /events` with correlation filtering.
4. Persist queue state in storage adapter.
5. Add agent registry endpoints (`register`, `heartbeat`, `get`).
6. Add task model + dependency edges endpoints.
7. Implement dependency impact resolver (first deterministic rule set).
8. Add provenance query endpoint for cause-chain retrieval.
9. Add lock API primitives with lease/fencing semantics.
10. Add long-poll support via `wait_seconds` on reads.
11. Add canonical CLI/script examples for post/read/wait.
12. Add optional Slack bridge with dry-run and type filters.
13. Add BDD scenarios for invalidation + lock contention + recovery + handoff.

## 9) Standards and OSS Patterns to Continue Reusing
- CloudEvents: event metadata consistency
- Temporal: event history/replay and durable state thinking
- Argo Workflows: DAG orchestration semantics
- NATS/Redis Streams: ack/nack/pending and recovery semantics
- etcd: robust lease lock patterns
- OPA: externalized policy decisions

## 10) Planning Artifacts Map
- Canonical execution plan: `docs/FULL_DEV_PLAN.md` (this file)
- ACP strategy baseline: `docs/ACP_DEV_PLAN.md`
- Version roadmap: `docs/ROADMAP.md`
- Test gates and done criteria: `docs/TESTING_STRATEGY.md`
- Prompt scaffolding: `prompts/`

