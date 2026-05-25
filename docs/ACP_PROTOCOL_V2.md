# ACP v2 Protocol Specification (Draft)

Status: **Planned / Draft for implementation**

This document defines the v2 Agent Coordination Protocol (ACP) contract for
IAC-Bus as a generic coordination layer.

## 1. Scope
ACP v2 is designed to improve multi-agent coordination for coding workflows
without coupling the bus to any one application stack.

ACP v2 covers:

- agent identity and endpoint handles
- message envelope and required message/channel conventions
- API contracts for post/read/long-poll and basic agent lifecycle
- durable SQL-backed message history requirements
- task/block/wait coordination semantics
- Slack bridge contract (optional component)

ACP v2 does not include:

- semantic memory implementation
- project-specific business logic
- OpenClaw/KSG/CPMS runtime internals
- full workflow scheduling engine

## 2. Identity Model

### 2.1 Canonical identity
- `agent_uuid` (UUIDv4) is immutable and authoritative.

### 2.2 Human-readable logical handle
- Format: `agent:<brand>.<repo-locale>.<ordinal-path>`
- Examples:
  - `agent:cursor.iac-bus.0`
  - `agent:cursor.repo-x.0-1`

### 2.3 Endpoint handle (medium-qualified)
- Format: `<logical-handle>@<medium>`
- Examples:
  - `agent:cursor.iac-bus.0@web`
  - `agent:cursor.repo-x.0-1@slack`

### 2.4 Ordinal path rules
- Master agent ordinal: `0`
- Child ordinals: `<parent>-<child-index>` (for example `0-1`, `0-4-8`)

### 2.5 Optional regex guidance
- `ordinal-path`: `^([0-9]+)(-[0-9]+)*$`
- `logical-handle`: `^agent:[a-z0-9_-]+\.[a-z0-9_.-]+\.[0-9]+(-[0-9]+)*$`

## 3. Message Contract

### 3.1 Required message types

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

### 3.2 Channel conventions

- `ops`
- `project.<project-name>`
- `repo.<repo-name>`
- `task.<task-id>`
- `agent.<agent-id>`
- `handoff`
- `blockers`

### 3.3 Envelope

Required top-level fields:
- `channel`
- `agent`
- `type`
- `message`

Optional top-level fields:
- `ref`
- `metadata`
- `correlation_id`
- `parent_message_id`

Example:

```json
{
  "channel": "task.self-healing-login-demo",
  "agent": "agent:cursor.repo-x.0-1@web",
  "type": "progress",
  "message": "Imported login procedure JSON and compiled DAG",
  "ref": "cursor/login-teaching-mode",
  "metadata": {
    "project": "ksg",
    "repo": "osl-agent-prototype",
    "branch": "cursor/login-teaching-mode",
    "pr": null,
    "tests": ["pytest tests/test_login_teaching.py"],
    "blockers": [],
    "needs": ["KSG selector repair endpoint deployed"],
    "artifacts": [],
    "next": ["Run live KSG smoke"]
  }
}
```

## 4. API Contract (v2)

All endpoints except `/health` require:

```http
Authorization: Bearer <BUS_API_TOKEN>
```

### 4.1 Agent lifecycle endpoints

#### POST `/agents/register`
Registers an agent identity and returns canonical IDs/handles.

Request:
```json
{
  "brand": "cursor",
  "repo_locale": "repo-x",
  "ordinal_path": "0-1",
  "parent_agent_uuid": "optional-uuid",
  "role": "worker",
  "medium": "web",
  "session_id": "optional-session"
}
```

Response `201`:
```json
{
  "agent_uuid": "uuid",
  "logical_handle": "agent:cursor.repo-x.0-1",
  "endpoint_handle": "agent:cursor.repo-x.0-1@web"
}
```

#### POST `/agents/heartbeat`
Updates endpoint liveness.

### 4.2 Message write endpoint

#### POST `/bus/messages`
Accepts ACP envelope. Server persists canonical message + route records.

Validation requirements:
- required fields present
- `type` in required type set
- channel convention checks enabled

### 4.3 Message read endpoint

#### GET `/bus/messages`
Query parameters:
- `channel` (optional)
- `since_id` (optional)
- `limit` (optional, bounded)
- `wait_seconds` (optional, long-poll timeout)

Long-poll contract:
- if no matching messages and `wait_seconds > 0`, request blocks up to timeout
- returns immediately when a matching message arrives
- returns empty array on timeout

Example:
```http
GET /bus/messages?channel=task.self-healing-login-demo&since_id=42&wait_seconds=20
```

## 5. Master/Sub-Agent Coordination Semantics

### 5.1 Task state model

```mermaid
stateDiagram-v2
    [*] --> assigned
    assigned --> accepted
    accepted --> running
    running --> blocked
    blocked --> running
    running --> handoff
    handoff --> running
    running --> done
    running --> failed
    failed --> [*]
    done --> [*]
```

### 5.2 Blocking/wait IPC-like flow

```mermaid
sequenceDiagram
    participant M as Master Agent
    participant A as Worker A
    participant B as Worker B
    participant Bus as IAC-Bus

    M->>Bus: request(assign task T1 to A)
    A->>Bus: progress(T1 started)
    A->>Bus: blocker(waiting on T2 from B)
    M->>Bus: request(assign task T2 to B)
    B->>Bus: done(T2 completed)
    A->>Bus: progress(T1 resumed)
    A->>Bus: done(T1 completed)
    M->>Bus: handoff(work package complete)
```

### 5.3 Authority model (recommended)
- Master/orchestrator agents can assign/cancel/approve/escalate.
- Child agents can execute scoped work, report progress, blockers, and handoffs.
- Policy engine (future/optional) can enforce these constraints.

## 6. Durable Agent History Log Requirement
All messages routed through IAC-Bus must be durably stored with:

- canonical `agent_uuid`
- endpoint medium context (`@web`, `@slack`, etc.)
- message envelope
- routing metadata (where message was delivered, filtered, failed, or posted)

History log requirements:
- append-only for message content
- immutable message IDs
- query by channel, agent, repo, time range, and correlation

## 7. SQL Backend Contract
Reference SQL schema:

- `docs/sql/ACP_V2_SCHEMA.sql`

Minimum required entities:
- agents
- agent_endpoints
- bus_messages
- bus_routes
- channel_offsets
- task_assignments
- resource_locks

## 8. Slack Bridge Contract (Optional Component)
The Slack bridge is separate from bus-core routing logic.

Requirements:
- poll configured channels
- post selected message types to webhook
- support dry-run mode
- filter noisy event types
- avoid posting secrets

Default forwarded types:
- `blocker`, `handoff`, `done`, `decision`, `error`

Suggested env vars:
```bash
SLACK_WEBHOOK_URL=...
BUS_POLL_SECONDS=20
BUS_SLACK_CHANNELS=project.ksg,blockers,handoff
BUS_SLACK_TYPES=blocker,handoff,done,decision,error
BUS_SLACK_DRY_RUN=true
```

## 9. Environment Variables
Core:
```bash
BUS_URL=https://bus.example.test
BUS_API_TOKEN=...
BUS_CHANNEL=task.self-healing-login-demo
AGENT_ID=agent:cursor.repo-x.0-1@web
```

Persistence:
```bash
BUS_DB_DSN=postgresql://user:pass@host:5432/iac_bus
BUS_DB_POOL_SIZE=20
```

## 10. Canonical Client Commands (Examples)
`iac-bus` client examples to be provided/maintained by this repo:

```bash
iac-bus post --channel task.self-healing-login-demo --type progress --message "Started"
iac-bus read --channel task.self-healing-login-demo --since-id 0
iac-bus wait --channel task.self-healing-login-demo --since-id 42 --wait-seconds 20
```

## 11. Minimum Required Tests for ACP v2 Rollout
1. valid-token message post success
2. unauthorized post rejection
3. channel filter read behavior
4. `since_id` read behavior
5. long-poll behavior with `wait_seconds`
6. invalid `limit` / malformed input handling
7. Slack dry-run formatting behavior
8. handoff metadata preservation

