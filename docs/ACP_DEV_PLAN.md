# IAC-Bus / ACP Development Plan (MVP First)

## Purpose
Build IAC-Bus into a practical coordination runtime for autonomous agents working
across interdependent codebases (for example, a primary product repo plus
related repos),

- coordination reliability,
- dependency-aware orchestration,
- provenance and explainability,
- synchronization and lock safety,
- and low human-overhead operations.

This plan is intentionally ordered from easiest to ship from the current Flask
codebase to most complex.

## Product Positioning
IAC-Bus is:

- coordination infrastructure for autonomous systems,
- dependency-aware orchestration runtime,
- operational coordination and provenance layer for agents.

IAC-Bus is not:

- a social chat platform,
- generic team chat replacement,
- or "Slack for agents."

## Generic Pivot Profile (OSL/OpenClaw-Compatible)
IAC-Bus must remain generic and application-agnostic for the OSL/OpenClaw pivot.

Required minimum coordination capabilities:

- status/progress updates,
- blocker signaling,
- cross-repo requests and responses,
- handoff and completion notices,
- optional Slack mirroring for human visibility,
- lightweight polling/long-poll synchronization.

Required generic message types:
- `info`, `progress`, `request`, `response`, `blocker`, `handoff`, `done`,
  `decision`, `heartbeat`, `error`

Required channel conventions:
- `ops`
- `project.<project-name>`
- `repo.<repo-name>`
- `task.<task-id>`
- `agent.<agent-id>`
- `handoff`
- `blockers`

Required envelope fields:
- required: `channel`, `agent`, `type`, `message`
- optional: `ref`, `metadata`

API requirements to include in MVP plan:
- `POST /bus/messages`
- `GET /bus/messages` with `channel`, `since_id`, `limit`, `wait_seconds`

Bus-core non-goals:
- KSG-specific storage or semantics
- OpenClaw runtime execution logic
- CPMS affordance matching logic
- repo-specific business rules

## Current Baseline (as of master)
- Flask HTTP bus with post/poll endpoints
- in-memory message retention limits
- optional bearer token auth
- scripts for deployment/hotfix and Cursor agent lifecycle

## External Models and Codebases to Borrow From
Use established patterns instead of inventing from scratch.

| Area | Borrow Pattern | Open Source Reference |
| --- | --- | --- |
| Event envelope | Standard event metadata shape and transport neutrality | CloudEvents spec: https://github.com/cloudevents/spec |
| Durable execution and replay | Append-only history and deterministic replay semantics | Temporal: https://github.com/temporalio/temporal |
| Dependency DAG orchestration | DAG-based dependencies, retries, fail-fast control | Argo Workflows: https://github.com/argoproj/argo-workflows |
| Queue work leasing | Explicit ack/nak/in-progress semantics | NATS JetStream docs: https://docs.nats.io/nats-concepts/jetstream |
| Pending/recovery model | Pending list inspection and claim/reassign workflows | Redis Streams docs: https://redis.io/docs/latest/develop/data-types/streams/ |
| Distributed locks | Lease + compare-and-swap + fencing token | etcd docs: https://etcd.io/docs/ |
| Policy enforcement | Decouple policy decisions from service logic | OPA: https://github.com/open-policy-agent/opa |

## MVP Scope (Version 0.1)
MVP proves coordination drift mitigation end-to-end.

### MVP Demo Scenario
1. Backend/contract agent publishes contract/spec change.
2. System resolves impacted tasks and dependencies.
3. Affected worker agents are notified.
4. Blocked/stale tasks are auto-marked for replan.
5. Provenance trail answers "why did this happen?"

### MVP Required Features
1. **Durable event ledger**
   - Append-only event storage (SQLite first, Postgres target)
   - replay by sequence and correlation id
2. **Agent identity registry**
   - canonical `agent_uuid`
   - readable handles (for example `agent:cursor.iac-bus.0@web`)
   - role, purpose, parent/root relationships
3. **Task and dependency model**
   - owner, watchers, status, blockers, dependencies
   - contract/spec references
4. **Propagation engine**
   - contract/task changes trigger invalidation and notifications
5. **Provenance API**
   - causal links: event -> dependent events -> outcomes
6. **Locking API for repo-critical resources**
   - lease-based path locks
   - fencing token to prevent stale writers
7. **Generic coordination profile**
   - required message type taxonomy
   - channel naming convention support
   - long-poll reads via `wait_seconds`
   - canonical client examples for post/read/wait
8. **Optional Slack bridge**
   - channel polling with type filters
   - dry-run mode
   - secret-safe posting behavior

## Data Model (MVP)
Use minimal but explicit entities.

- `agents`
  - `agent_uuid`, `agent_handle`, `brand`, `repo`, `role`, `purpose`,
    `parent_agent_uuid`, `status`, `last_seen_at`
- `tasks`
  - `task_uuid`, `title`, `owner_agent_uuid`, `status`, `priority`,
    `repo`, `branch`, `blocked_reason`
- `task_dependencies`
  - `task_uuid`, `depends_on_task_uuid`, `dependency_type`
- `contracts`
  - `contract_uuid`, `name`, `version`, `state`, `changed_by_agent_uuid`
- `events`
  - `event_uuid`, `event_type`, `actor_type`, `actor_id`, `source_medium`,
    `correlation_id`, `parent_event_uuid`, `repo`, `branch`, `payload`,
    `created_at`
- `locks`
  - `lock_id`, `resource_key`, `mode`, `holder_agent_uuid`, `task_uuid`,
    `lease_expires_at`, `fencing_token`

## API Plan (MVP)
Build on top of existing `/bus/messages`.

- `POST /agents/register`
- `POST /agents/heartbeat`
- `POST /tasks`
- `POST /tasks/{id}/status`
- `POST /dependencies/resolve-impact`
- `POST /events`
- `GET /events?since=...&correlation_id=...`
- `POST /locks/acquire`
- `POST /locks/renew`
- `POST /locks/release`

## Implementation Sequence (Easiest -> Hardest)

### Stage 1: Protocol and schema finalization
- Define event envelope and task state machine in JSON schema.
- Adopt CloudEvents-aligned fields where practical.
- Freeze MVP schema version (`v0.1`).

### Stage 2: Persistence foundation
- Add storage abstraction to current server.
- Implement SQLite adapter first for speed.
- Add migration path and Postgres adapter.

### Stage 3: Agent and task core
- Implement registry and task CRUD/status transitions.
- Add dependency edges and basic DAG validation.

### Stage 4: Invalidation and notification propagation
- On contract/task changes, compute impacted tasks.
- Emit invalidation/blocker events.

### Stage 5: Provenance query surface
- Build causal chain retrieval endpoints.
- Provide explainability response format.

### Stage 6: Lock manager for repo/file critical sections
- Add lease, renew, release, and expiration handling.
- Add fencing tokens and stale-holder protection.

### Stage 7: Governance and policy hooks
- Add pluggable policy checks (OPA-compatible pattern).
- Enforce role-based execution boundaries.

## Multi-Repo Usage Pattern (Primary Repo + Related Repos)
Use one ACP instance as control plane:

- namespaced by `repo` and `project`,
- shared global agent identity,
- per-repo task/dependency graph views,
- shared provenance timeline across repos.

Recommended identity convention:
- canonical: `agent_uuid` (immutable)
- readable: `agent:<brand>.<repo>.<index>@<medium>`

## Non-Goals for MVP
- federated identity network
- blockchain-level trust systems
- semantic routing ML stack
- full HA clustering

These can be explored after MVP drift-mitigation value is proven.

## MVP Completion Criteria
MVP is complete when all are true:

1. A full dependency invalidation scenario passes in automated tests.
2. Provenance endpoint can explain cause chain for a blocked task.
3. Lock contention tests prevent concurrent conflicting edits.
4. Recovery tests prove restart does not lose critical coordination events.
5. Role/policy tests prevent unauthorized task control actions.
