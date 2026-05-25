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

## Hotfix (no OCI credentials)

Apply a hotfix directly on a VM by pulling a tarball from GitHub:
```bash
sudo ./scripts/hotfix-pull.sh
```

Apply to a remote VM over SSH (from your local machine):
```bash
./scripts/hotfix-remote.sh ubuntu@<IP> ~/.ssh/your_key
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
- [docs/FULL_DEV_PLAN.md](docs/FULL_DEV_PLAN.md) - canonical full development plan assimilating merged PR intent and ACP planning.
- [docs/ACP_DEV_PLAN.md](docs/ACP_DEV_PLAN.md) - consolidated MVP-first development plan with OSS architecture references.
- [docs/ROADMAP.md](docs/ROADMAP.md) - versioned delivery roadmap from easiest to most complex.
- [docs/TESTING_STRATEGY.md](docs/TESTING_STRATEGY.md) - TDD/BDD cascading test strategy and completion gates.
- [prompts/README.md](prompts/README.md) - agent prompt file conventions and usage.
