# IAC-Bus / ACP Versioned Roadmap

This roadmap is ordered by implementation complexity and operational risk.

## v0.1 - Coordination MVP (First Useful Version)
Primary outcome: demonstrate coordination drift mitigation.

### Scope
- durable event ledger (SQLite baseline)
- agent registry with handles and role metadata
- task model with dependency edges and blockers
- contract/spec change event type
- dependency impact propagation
- provenance query endpoint
- lease-based lock API for repo/path critical sections

### Exit Criteria
- end-to-end invalidation demo passes automated tests
- provenance can answer "what caused this blocker?"
- lock semantics validated under concurrent worker simulation

## v0.2 - Reliability and Throughput
Primary outcome: dependable operations under higher load.

### Scope
- Postgres storage backend and migrations
- queue retry policies + dead-letter handling
- idempotency keys for write endpoints
- consumer checkpointing for replay/resume
- structured metrics and tracing

### Exit Criteria
- soak tests pass with no coordination state corruption
- retry and duplicate-delivery behaviors deterministic

## v0.3 - Governance and Policy Enforcement
Primary outcome: safe delegation in multi-agent organizations.

### Scope
- role-based permissions and scoped capabilities
- approval checkpoints for sensitive actions
- escalation rules and authority boundaries
- policy engine integration pattern (OPA-style decision point)

### Exit Criteria
- unauthorized control paths blocked by tests
- approval-gated transitions enforced by policy suite

## v0.4 - Real-Time Coordination and Integrations
Primary outcome: low-latency sync across IDE/Web/Slack bridges.

### Scope
- SSE transport
- optional WebSocket transport
- Slack bridge adapter (inbound commands + outbound status mirrors)
- medium-aware routing and filtering

### Exit Criteria
- end-to-end medium sync tests pass
- ordering and dedupe guarantees documented and tested

## v0.5 - Operational Maturity
Primary outcome: production readiness for sustained multi-repo usage.

### Scope
- backup/restore and disaster recovery runbooks
- operational dashboards and alerting
- rolling upgrade procedures
- optional HA deployment profile

### Exit Criteria
- restore drills pass from backup snapshots
- on-call diagnostics sufficient to resolve injected failures quickly

## Research Track (Not on critical path)
Keep separate from delivery milestones:
- semantic attention routing
- trust/reputation layers
- federated identity and negotiation protocols
- autonomous arbitration systems
