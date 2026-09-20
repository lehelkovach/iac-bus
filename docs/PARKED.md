# IAC-Bus: parked at M0 (2026-09-20)

## Decision

IAC-Bus is frozen at its current `master` (M0 / L0): in-memory HTTP pub/sub,
claim/ack/nack work leasing, and the orchestration job-graph evaluator with
barriers. Tests and smoke script must stay green. No further ladder work.

## Why

- **No consumer.** None of the active sibling repos (`knowshowgo-client`,
  `truth-app`, `yolo-online-learner`) reference the bus. The client repo's
  agent guide explicitly moved to "work lives in PRs, no run-once queues".
- **Roadmap duplicates commodity infrastructure.** M1–M5 (identity, directed
  routing, durable storage, SSE/WebSocket, ACLs, HA) are Redis Streams, NATS,
  or Temporal at lower quality. The master/worker/sibling pattern in the README
  is already covered by agent-host features (subagents, PR subscriptions,
  scheduled triggers, fan-out sessions) with durability and a UI.
- **Open blockers are all infra, not product.** B-001..B-003 are OCI secrets,
  DNS, and VM SSH. Clearing them yields a deployed service nobody calls.
- **Docs outweigh code ~3:1.** Further planning docs would not change the above.

## Ideas worth keeping (design, not code)

1. **Barrier sync as the coordination litmus.** Fan-out is trivial; the real
   test is that parallel workers *reconverge*. `scripts/dogfood_litmus.sh`
   encodes this: a barrier step may run only after every dependency reports
   `completed`, and a job is healthy only when `/ready` returns `[]`.
   Reusable anywhere as "a check that waits on other checks".
2. **Oversight / adjudication envelope.** A subordinate posts
   `request_decision` with `choices[]`, `requires_approval`, and an
   `escalation_chain`; the supervisor answers `provide_decision` with
   `choice_id` + `approved`. See README "Oversight / adjudication flow" and
   `schemas/agent-contract.schema.json`. Maps cleanly onto a PR review or an
   issue comment; the shape is the value, not the transport.

## Un-park trigger

Resume only when **all** of these hold:

- three or more agents from different vendors must hand work to each other,
- a shared PR / issue workflow demonstrably cannot carry the handoff, and
- the coordination log must outlive any single vendor's session.

Even then, evaluate NATS or Redis Streams behind a thin adapter before
extending `server.py`.

## Maintenance scope while parked

```bash
./venv/bin/pytest -q
BUS_PORT=8101 BUS_API_TOKEN=devtoken bash ./scripts/bus_smoke.sh
```

Dependency bumps and security fixes only. No new endpoints, no new docs.
