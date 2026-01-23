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
- [ ] Expand tests for validation errors and lease edge cases.
- [ ] Integration tests for leader/subordinate workflows.
- [ ] Load tests for retention and queue throughput.

## Deployment Plan
- [ ] OCI VM baseline (Ubuntu + systemd).
- [ ] TLS termination (nginx/caddy) and firewall rules.
- [ ] Rolling upgrade and hotfix workflow.
