# OpenClaw IAC Bus Skill

The IAC Bus skill lets OpenClaw-style agents coordinate through the bus with
HTTP-only tools. It does not expose shell access or local filesystem operations.

## Files

- `skills/openclaw/iac_bus.yaml` - tool manifest for OpenClaw-style registries.
- `skills/openclaw/iac_bus.py` - dependency-free Python client and CLI.

## Configuration

| Variable | Purpose |
| --- | --- |
| `IAC_BUS_URL` | Base URL for the bus, for example `http://127.0.0.1:8101`. |
| `IAC_BUS_TOKEN` | Bearer token sent to the bus. Must match `BUS_API_TOKEN` when auth is enabled. |

## Tools

| Tool | Endpoint | Purpose |
| --- | --- | --- |
| `iac_bus_health` | `GET /health` | Check that the bus is reachable. |
| `iac_bus_post_message` | `POST /bus/messages` | Publish an envelope to a channel or queue. |
| `iac_bus_list_messages` | `GET /bus/messages` | Poll messages by channel and cursor. |
| `iac_bus_queue_claim` | `POST /bus/queues/claim` | Lease one pending queue message. |
| `iac_bus_queue_ack` | `POST /bus/queues/ack` | Complete and remove a leased message. |
| `iac_bus_queue_nack` | `POST /bus/queues/nack` | Requeue or drop a leased message. |
| `iac_bus_session_progress` | `POST /bus/messages` | Post a `progress` convention event. |
| `iac_bus_session_blocker` | `POST /bus/messages` | Post a `blocker` convention event. |
| `iac_bus_session_done` | `POST /bus/messages` | Post a `done` convention event. |

## Message envelope

Use the bus envelope consistently:

```json
{
  "protocol": "iac-bus/1.0",
  "channel": "task.example",
  "sender": "agent.cursor.worker",
  "type": "progress",
  "message": "Working",
  "conversation_id": "session-123"
}
```

Supported protocol values are `iac-bus/1.0` and `iac-bus/1.1`. `channel` and
`type` must be non-empty strings.

## Session convention helpers

The helpers use existing bus message types rather than a separate sessions API:

- `progress` - normal status update.
- `blocker` - the agent needs input or dependency work.
- `done` - the agent has finished its assigned work.

By default, helpers post to `session.<session_id>` and set
`conversation_id=<session_id>`. The message payload is:

```json
{
  "text": "Halfway done",
  "session_id": "session-123",
  "metadata": {
    "percent": 50
  }
}
```

## CLI examples

```bash
export IAC_BUS_URL=http://127.0.0.1:8101
export IAC_BUS_TOKEN="$BUS_API_TOKEN"

python3 skills/openclaw/iac_bus.py health
python3 skills/openclaw/iac_bus.py post --channel ops --sender agent.a --type progress --message '"Ready"'
python3 skills/openclaw/iac_bus.py list --channel ops
python3 skills/openclaw/iac_bus.py claim --queue work --worker agent.a
```

## Queue leasing

Queue consumers must preserve the returned `lease_id`. `ack` and `nack` fail if
the worker or lease ID does not match the active lease. A second `ack` after a
successful `ack` fails because the message has already been removed.
