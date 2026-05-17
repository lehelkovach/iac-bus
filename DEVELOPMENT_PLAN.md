# Development Plan: Cursor Agent Coordination

## Relevance Assessment

This repository remains relevant for coordinating Cursor agents. The current
service is intentionally small, runs without external infrastructure, and now
supports enough message metadata for orchestrator/worker communication patterns:
directed messages, message kinds, thread correlation, replies, and structured
metadata.

The bus should stay focused on coordination primitives. Slack or another chat
surface should be treated as an integration layer that mirrors important events
and accepts operator commands, not as the system of record for agent state.

## First Features to Prioritize

1. **Directed routing and workflow correlation**
   - Required fields: `target`, `kind`, `thread_id`, `reply_to`, `metadata`.
   - Status: initial envelope implemented in `/bus/messages`.
   - Why first: agents need to know which messages are theirs and which work
     item a reply belongs to before higher-level orchestration can be reliable.

2. **Acknowledgements, retries, and leases**
   - Add conventions or endpoints for `task`, `ack`, `failed`, and `lease`.
   - Include retry counts, expiration timestamps, and final status in metadata.
   - Why next: orchestration needs evidence that a worker accepted and completed
     work, especially when Cursor agents can stop or lose context.

3. **Identity and presence**
   - Track agent identity, role, capabilities, last heartbeat, and current
     thread/task.
   - Why next: an orchestrator needs to choose appropriate workers and detect
     stale agents.

4. **Role and group addressing**
   - Support targets like `role:reviewer`, `group:frontend`, or `all`.
   - Why next: direct IDs work for small tests, but real orchestration needs
     dynamic assignment.

5. **Human-visible bridge**
   - Mirror high-value events to Slack or another chat system: spawned agents,
     plans, failed tasks, handoff summaries, and blocked threads.
   - Accept explicit operator commands such as pause, resume, terminate, and
     summarize.
   - Why later: the bus should first define stable event semantics that the
     bridge can translate.

6. **Persistence and streaming**
   - Add SQLite or Postgres for retained state.
   - Add server-sent events or WebSocket streaming for low-latency inboxes.
   - Why later: both are useful once the core coordination contract settles.

## Message Kind Conventions

Use short, stable names so scripts and agents can filter quickly:

- `note`: default informational message.
- `plan`: orchestrator intent, feature order, or execution plan.
- `task`: actionable work for an agent.
- `ack`: acknowledgement or completion notice; set `reply_to`.
- `failed`: failed work; set `reply_to` and put error details in `metadata`.
- `handoff`: summary for another agent or human operator.
- `lifecycle.spawned`: Cursor agent was created.
- `lifecycle.terminated`: Cursor agent was terminated.
- `presence.heartbeat`: agent heartbeat.

## Mock-Agent Validation

Use `scripts/mock-agent-roundtrip.py` against a local or deployed bus:

1. The orchestrator posts a `plan` message with a new `thread_id`.
2. The orchestrator posts one `task` per worker with `target=<worker-id>`.
3. Each worker polls by `channel`, `target`, `kind=task`, and `thread_id`.
4. Each worker replies with `kind=ack`, `target=orchestrator`, and
   `reply_to=<task-id>`.
5. The orchestrator polls for `ack` messages on the same thread and verifies
   all tasks completed.

This is the first live-test loop to keep working while additional Cursor or
Slack integrations are added.

## Documentation Integration Checklist

- Keep `README.md` focused on quick start, configuration, scripts, and roadmap.
- Keep `DOCUMENTATION.md` as the API reference and runtime behavior guide.
- Keep this file as the product/development plan for agent communication
  priorities.
- Add Slack bridge docs only after there is a script or service that can be run
  locally with mocked Slack events.
