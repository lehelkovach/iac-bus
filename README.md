# Inter-Agent Communication Bus (IAC Bus)

Lightweight message bus for coordinating multiple agents over HTTP.

## Features
- Simple REST endpoints for posting and polling messages
- In-memory retention with size + time limits
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
