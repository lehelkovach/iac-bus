# Inter-Agent Communication Bus (IAC Bus)

Lightweight message bus for coordinating multiple agents over HTTP.

## Features
- Simple REST endpoints for posting and polling messages
- In-memory retention with size + time limits
- Queue-style work leasing (claim/ack/nack)
- Optional bearer-token auth
- Systemd service deployment

## Endpoints

### Post message
```bash
curl -X POST http://<BUS_IP>:8091/bus/messages \
  -H "Authorization: Bearer $BUS_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"channel":"ops","sender":"agent-a","message":"ready"}'
```

### Poll messages
```bash
curl "http://<BUS_IP>:8091/bus/messages?channel=ops" \
  -H "Authorization: Bearer $BUS_API_TOKEN"
```

Use `since_id` to avoid re-reading older messages:
```bash
curl "http://<BUS_IP>:8091/bus/messages?channel=ops&since_id=<LAST_ID>" \
  -H "Authorization: Bearer $BUS_API_TOKEN"
```

Queue messages are excluded from polling by default. To include them:
```bash
curl "http://<BUS_IP>:8091/bus/messages?include_queue=true" \
  -H "Authorization: Bearer $BUS_API_TOKEN"
```

### Claim from queue (work leasing)
```bash
curl -X POST http://<BUS_IP>:8091/bus/queues/claim \
  -H "Authorization: Bearer $BUS_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"queue":"work","worker":"agent-a","lease_seconds":60}'
```

### Ack queue message
```bash
curl -X POST http://<BUS_IP>:8091/bus/queues/ack \
  -H "Authorization: Bearer $BUS_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"queue":"work","worker":"agent-a","message_id":"<ID>","lease_id":"<LEASE_ID>"}'
```

### Nack queue message (requeue)
```bash
curl -X POST http://<BUS_IP>:8091/bus/queues/nack \
  -H "Authorization: Bearer $BUS_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"queue":"work","worker":"agent-a","message_id":"<ID>","lease_id":"<LEASE_ID>","requeue":true}'
```

### Health check
```bash
curl http://<BUS_IP>:8091/health
```

## Environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `BUS_HOST` | `0.0.0.0` | Bind address |
| `BUS_PORT` | `8091` | Listen port |
| `BUS_API_TOKEN` | empty | Bearer token |
| `BUS_MAX_MESSAGES` | `500` | Max retained messages |
| `BUS_RETENTION_SECONDS` | `3600` | Message retention window |
| `BUS_QUEUE_LEASE_SECONDS` | `60` | Default queue lease seconds |
| `BUS_LOG_LEVEL` | `INFO` | Log level |

## Local Run
```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
BUS_API_TOKEN=devtoken ./venv/bin/python server.py
```

## Deploy (systemd)
```bash
sudo ./deploy.sh
sudo systemctl status iac-bus.service
```

The deploy script installs to `/opt/iac-bus` and creates
`/etc/iac-bus/iac-bus.env` for configuration.

## OCI Deployment

See `OCI_DEPLOYMENT.md` for VM provisioning and setup steps.

## Dev VM CI/CD (Oracle Cloud)

For autonomous dev deployments with hot reload and debug logs, use:

- `scripts/deploy-dev-vm.sh` (manual/local deployment using injected secrets)
- `.github/workflows/dev-deploy.yml` (test + deploy on push)
- `systemd/iac-bus-dev.service` + `scripts/run-dev-hot-reload.sh` (live reload)

Expected secret/environment inputs:

- `KSG_DEV_VM_HOST`
- `KSG_DEV_VM_USER`
- `KSG_DEV_VM_PORT` (optional, default `22`)
- `KSG_DEV_VM_APP_DIR` (optional, default `/opt/iac-bus-dev`)
- `KSG_DEV_VM_KEY` (private key content or file path)
- `BUS_API_TOKEN` (optional, for protected dev API)

Manual deploy:
```bash
chmod +x scripts/deploy-dev-vm.sh
./scripts/deploy-dev-vm.sh
```

Live debugging logs on dev VM:
```bash
ssh -i <key> <user>@<host> "sudo journalctl -u iac-bus-dev.service -f"
```

Debug-level logging is enabled by default in dev deployment (`BUS_LOG_LEVEL=DEBUG`)
and the service auto-restarts on file changes via `watchmedo`.

### Provision a brand-new OCI VM using `OCI_*` secrets

Use the workflow `.github/workflows/oci-provision-dev-vm.yml` (manual
workflow_dispatch) or run locally:

```bash
python3 -m pip install oci
python3 scripts/provision-oci-dev-vm.py
```

Required `OCI_*` secrets:
- `OCI_TENANCY_OCID`
- `OCI_USER_OCID`
- `OCI_FINGERPRINT`
- `OCI_REGION`
- `OCI_COMPARTMENT_OCID`
- `OCI_SUBNET_OCID`
- `OCI_IMAGE_OCID`
- `OCI_SSH_PUBLIC_KEY`
- one of:
  - `OCI_PRIVATE_KEY`
  - `OCI_PRIVATE_KEY_B64`

Optional:
- `OCI_AVAILABILITY_DOMAIN`
- `OCI_SHAPE`
- `OCI_VM_DISPLAY_NAME`
- `OCI_BOOT_VOLUME_SIZE_GBS`
- `OCI_RUN_DEPLOY_AFTER_CREATE=true` (will trigger `scripts/deploy-dev-vm.sh`)

## Protocol Schema

JSON schema definitions for the message protocol and queue endpoints live in
`schemas/iac-bus.schema.json`.

Protocol v1.1 adds orchestration message types:
- `orchestration.job`
- `orchestration.step.assign`
- `orchestration.step.status`

## Universal Agent Contract

Define shared agent behavior, actions, states, and control inputs with a
portable envelope plus adapter interfaces:

- Schema: `schemas/agent-contract.schema.json`
- Python interface + marshalling helpers: `agent_contract.py`

### Oversight / adjudication flow

Subordinate requests a decision from the supervisor (with options):
```json
{
  "kind": "control",
  "action": "request_decision",
  "from": "agent-2",
  "to": "supervisor-1",
  "conversation_id": "conv-99",
  "state": "waiting_input",
  "payload": {
    "prompt": "Proceed with deploy?",
    "choices": [
      {"id": "continue", "label": "Continue"},
      {"id": "pause", "label": "Pause and review"},
      {"id": "abort", "label": "Abort"}
    ],
    "requires_approval": true,
    "escalation_chain": ["supervisor-1", "user"]
  }
}
```

Supervisor responds (allowing the agent to continue):
```json
{
  "kind": "response",
  "action": "provide_decision",
  "from": "supervisor-1",
  "to": "agent-2",
  "conversation_id": "conv-99",
  "payload": {
    "choice_id": "continue",
    "approved": true,
    "notes": "Proceed"
  }
}
```

## Orchestration Jobs (dependencies + sync)

Define job/step graphs with dependencies, time gates, and barrier sync points:

- Schema: `schemas/orchestration.schema.json`
- Python helper (ready-step evaluation): `orchestration.py`

Example:
```json
{
  "job_id": "job-1",
  "name": "build-service",
  "steps": [
    {"id": "design"},
    {"id": "impl", "depends_on": [{"step_id": "design"}]},
    {"id": "tests", "depends_on": [{"step_id": "impl"}]},
    {"id": "review", "wait_for": ["sync-1"]}
  ],
  "barriers": [
    {"id": "sync-1", "requires": ["design", "impl", "tests"], "mode": "all_completed"}
  ]
}
```

## Agent Collaboration Guide (Master / Slave / Sibling)

Use this section as the quick operational playbook for any new agent joining an
active multi-agent task.

Terminology note:
- "Master" == supervisor/orchestrator agent.
- "Slave" == subordinate worker agent (execution-focused child).
- "Sibling" == another worker under the same master.

### Role responsibilities

| Role | Primary responsibilities | Should publish |
| --- | --- | --- |
| Master | assign tasks, track blockers, resolve dependencies, approve handoffs | `request`, `decision`, `handoff`, `done` |
| Slave/Subordinate | execute scoped tasks, report progress, raise blockers, hand off finished work | `progress`, `blocker`, `handoff`, `done`, `error` |
| Sibling worker | same as subordinate, plus explicit dependency notifications to peer workers via bus | `response`, `progress`, `done`, `blocker` |

### Required message shape (ACP v2 style)

At minimum, agents should send:
- `channel`
- `agent`
- `type`
- `message`

Optional:
- `ref`
- `metadata`

Reference: `docs/ACP_PROTOCOL_V2.md`

### Channel conventions

- `ops`
- `project.<project-name>`
- `repo.<repo-name>`
- `task.<task-id>`
- `agent.<agent-id>`
- `handoff`
- `blockers`

### New-agent startup checklist

1. Register identity (or resolve existing identity) and determine role:
   - master / slave / sibling.
2. Set a stable `agent` handle with medium suffix:
   - example: `agent:cursor.iac-bus.0@web`
3. Subscribe/poll required channels for assigned scope:
   - at minimum task channel + `blockers` + `handoff`.
4. Send initial heartbeat/progress message:
   - announce active state and current task focus.
5. Use `since_id` (and when available `wait_seconds`) to avoid missed messages.

### Master delegation pattern

1. Master posts task assignment to `task.<task-id>` (`type=request`).
2. Slave accepts and posts `progress`.
3. If blocked, slave posts `blocker` to `blockers` (with dependency details).
4. Master either:
   - assigns dependency work to a sibling, or
   - posts `decision` with unblock instructions.
5. Slave resumes, posts `done`, then posts `handoff` with tests/artifacts/next.

### Sibling dependency handoff pattern (IPC-like)

When worker A needs worker B:

1. A posts `blocker` with `metadata.needs`.
2. Master assigns B via `request`.
3. B posts `done` on dependency task channel.
4. A observes completion (poll channel with `since_id`), resumes, posts `progress`.

This pattern provides OS-like "wait until dependency completes" behavior without
tight coupling between workers.

### Minimal practical examples

Master assignment:
```bash
curl -X POST "http://<BUS_IP>:8091/bus/messages" \
  -H "Authorization: Bearer $BUS_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"channel":"task.login-flow","agent":"agent:cursor.iac-bus.0@web","type":"request","message":"Implement login validation","metadata":{"owner":"agent:cursor.iac-bus.0-1@web"}}'
```

Slave reports blocker:
```bash
curl -X POST "http://<BUS_IP>:8091/bus/messages" \
  -H "Authorization: Bearer $BUS_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"channel":"blockers","agent":"agent:cursor.iac-bus.0-1@web","type":"blocker","message":"Waiting for API contract update","metadata":{"needs":["OpenAPI v3 response schema"],"task":"task.login-flow"}}'
```

Sibling completion notice:
```bash
curl -X POST "http://<BUS_IP>:8091/bus/messages" \
  -H "Authorization: Bearer $BUS_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"channel":"task.api-contract","agent":"agent:cursor.iac-bus.0-2@web","type":"done","message":"API contract update completed","metadata":{"artifacts":["openapi.yaml"]}}'
```

Wait/read loop (poll with cursor):
```bash
curl "http://<BUS_IP>:8091/bus/messages?channel=task.api-contract&since_id=<LAST_ID>&limit=100" \
  -H "Authorization: Bearer $BUS_API_TOKEN"
```

## Multi-Agent Coordination Entropy Benchmark ("LIPMESS test")

Use the synthetic benchmark to compare a time-boxed umbrella workflow:

- `without_bus`: ad-hoc swarm behavior (stale state, duplicate effort, rework)
- `with_bus`: orchestrated task leasing/assignment behavior

Run:
```bash
python3 scripts/run-multiagent-entropy-benchmark.py --pretty
```

The benchmark reports:

- completion ratio within a fixed tick budget,
- entropy score (collision + rework + blocking pressure),
- productivity score,
- per-trial details for reproducibility.

Regression tests for this benchmark live in:

- `tests/test_multiagent_entropy_benchmark.py`

## Hotfix (no OCI credentials)

Apply a hotfix directly on a VM by pulling a tarball from GitHub:
```bash
sudo ./scripts/hotfix-pull.sh
```

Apply to a remote VM over SSH (from your local machine):
```bash
./scripts/hotfix-remote.sh ubuntu@<IP> ~/.ssh/your_key
```

Deploy and configure the Oracle dev VM with hot-reload service:
```bash
./scripts/deploy-dev-vm.sh
```

## Spawn Cursor Agents + Bus Announce

This script spawns Cursor Cloud Agents and announces each agent on the bus.

```bash
export CURSOR_API_KEY="key_xxx..."
python3 scripts/spawn-cursor-agents.py \
  --cursor-endpoint "https://api.cursor.com/v0/agents" \
  --count 2 \
  --payload '{"name":"agent-a"}' \
  --bus-url "http://127.0.0.1:8091" \
  --bus-token "$BUS_API_TOKEN"
```

Notes:
- Cursor uses **Basic Auth** with the key as the username and a blank password.
- `--payload` is passed directly to the Cursor API and can include custom fields.

## Handoff: Terminate Cursor Agents

Terminate agents explicitly by ID:
```bash
export CURSOR_API_KEY="key_xxx..."
python3 scripts/handoff-terminate-agents.py \
  --cursor-endpoint "https://api.cursor.com/v0/agents" \
  --agent-ids "agent-id-1,agent-id-2"
```

Or extract IDs from a handoff notes file:
```bash
python3 scripts/handoff-terminate-agents.py \
  --cursor-endpoint "https://api.cursor.com/v0/agents" \
  --agent-ids-file /path/to/redroid-cloud-phone_notes.md
```

Or terminate agents announced on the bus:
```bash
python3 scripts/handoff-terminate-agents.py \
  --cursor-endpoint "https://api.cursor.com/v0/agents" \
  --bus-url "http://127.0.0.1:8091" \
  --bus-token "$BUS_API_TOKEN"
```

## Topology Roadmap (Supervisor/Subordinate)

The current bus is a simple channel-based relay. To support supervisor and
subordinate agent topologies, the following functionality should be added:

- **Identity & presence**: register agents, heartbeat TTL, and last-seen status.
- **Roles & hierarchy**: supervisor/subordinate roles with parent-child links.
- **Directed routing**: direct messages to agent IDs (not only channels).
- **Group addressing**: supervisor can broadcast to all descendants.
- **Work leasing**: supervisor assigns tasks with lease + ack/complete flow.
- **Conversation threads**: correlate messages by `conversation_id`.
- **Access control**: role-based permissions and channel ACLs.
- **Durability**: optional persistent storage (SQLite/Postgres/Redis).
- **Streaming**: SSE or WebSocket long-poll for low-latency updates.
- **Rate limits**: per-agent and per-channel throttling.

## Planning and Scaffolding

Implementation planning and execution scaffolding are documented in:

- [docs/ACP_PROTOCOL_V2.md](docs/ACP_PROTOCOL_V2.md) - strict ACP v2 draft protocol contract with identity, message/channel conventions, API semantics, and state diagrams.
- [docs/sql/ACP_V2_SCHEMA.sql](docs/sql/ACP_V2_SCHEMA.sql) - draft Postgres schema for durable agent/message history and coordination state.
- [prompts/REPO_AGENT_TAKEOVER.prompt.md](prompts/REPO_AGENT_TAKEOVER.prompt.md) - first-read takeover prompt for any new agent continuing work.
- [docs/AGENT_TASKS.md](docs/AGENT_TASKS.md) - canonical active task queue for takeover agents.
- [docs/FULL_DEV_PLAN.md](docs/FULL_DEV_PLAN.md) - canonical full development plan assimilating merged PR intent and ACP planning.
- [docs/ACP_DEV_PLAN.md](docs/ACP_DEV_PLAN.md) - consolidated MVP-first development plan with OSS architecture references.
- [docs/ROADMAP.md](docs/ROADMAP.md) - versioned delivery roadmap from easiest to most complex.
- [docs/TESTING_STRATEGY.md](docs/TESTING_STRATEGY.md) - TDD/BDD cascading test strategy and completion gates.
- [prompts/README.md](prompts/README.md) - agent prompt file conventions and usage.
