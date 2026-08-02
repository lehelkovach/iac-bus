# IAC Bus Roadmap

## Vision
Provide a lightweight inter-agent coordination bus with a clear protocol,
supporting supervisor/subordinate workflows, reliable task queues, and
low-latency delivery over polling now and push transport later.

## Milestones

### M0 - Core HTTP bus (now)
- [x] Publish/poll over HTTP with in-memory retention.
- [x] Queue leasing endpoints (claim/ack/nack).
- [x] Protocol schema definitions for message + queue endpoints.
- [x] Universal agent contract schema + adapter interface.
- [x] Orchestration job schema + ready-step evaluator.
- [x] Basic unit tests for auth, polling, and queue leasing.

### M1 - Identity, routing, and coordination
- [ ] Agent registration + heartbeat TTL (presence).
- [ ] Roles and hierarchy (supervisor/subordinate).
- [ ] Directed routing (recipient, group) and ACL rules.
- [ ] Conversation threads and correlation IDs.

### M2 - Reliability and storage
- [ ] Durable storage (SQLite/Redis/Postgres).
- [ ] Dead-letter queues + retry/backoff policies.
- [ ] Priority scheduling and ordering controls.

### M3 - Push and streaming
- [ ] SSE or long-poll for low latency.
- [ ] WebSocket transport and client SDK helpers.
- [ ] Backpressure and rate limits.

### M4 - Ops and observability
- [ ] Metrics, tracing, and structured logs.
- [ ] Queue/admin API for visibility and control.
- [ ] HA and clustering.

## Testing Plan
- [x] Concurrency tests for exactly-once claim, lease takeover, and fencing.
- [x] Swarm harness with flood/fanout/DAG scenarios and invariant checks.
- [x] Deployment conformance suite with capability probes.
- [ ] Expand tests for validation errors and lease edge cases.
- [ ] Integration tests for leader/subordinate workflows.
- [ ] Restart and backpressure injection (blocked on durable state).

See `docs/SWARM_TESTING_PLAN.md` for the full swarm test strategy.

## Deployment Plan
- [ ] OCI VM baseline (Ubuntu + systemd).
- [ ] TLS termination (nginx/caddy) and firewall rules.
- [ ] Rolling upgrade and hotfix workflow.

See `docs/OCI_HOSTING_PLAN.md` for the phased hosting plan and operating rules.

## Swarming

Multi-agent swarming has its own phased plan in `docs/SWARM_DEV_PLAN.md`, with a
measured baseline and nine tracked capability gaps. Progress is visible by
running `python3 -m pytest tests/swarm -q`: each gap is a strict-xfail test that
turns into a failure once the capability lands.
