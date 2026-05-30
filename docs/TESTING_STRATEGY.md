# IAC-Bus / ACP Testing Strategy (TDD + BDD + Cascading Gates)

## Testing Philosophy
Use test-first development for protocol and orchestration behavior.

- **TDD** for engine logic, state transitions, and edge cases.
- **BDD** for user-visible coordination outcomes and handoff scenarios.
- **Cascading quality gates** so release readiness is objective.

## Test Layers

### L0 - Schema and contract validation
Purpose: block invalid payloads and contract drift early.

- JSON schema validation tests (events, agents, tasks, locks).
- Backward compatibility tests for schema versions.

### L1 - Unit tests (deterministic logic)
Purpose: verify core rules in isolation.

- task state machine transitions
- dependency impact resolver
- lock lease lifecycle and fencing checks
- policy decision adapter behavior

### L2 - Service integration tests
Purpose: validate endpoint behavior with real storage adapters.

- API + SQLite integration
- API + Postgres integration (CI profile)
- idempotency and dedupe behavior

### L3 - BDD scenario tests
Purpose: prove operational value in human-readable scenarios.

Example scenario families:
- contract change invalidates downstream tasks
- stale assumption detected and task blocked
- orchestrator assigns task and worker escalates blocker
- lock contention prevents conflicting writes
- time-boxed "lipmess" umbrella task: uncoordinated swarm stalls while
  coordinated bus-assisted swarm completes

### L4 - Concurrency and resilience tests
Purpose: stress coordination reliability.

- concurrent lock acquisition races
- lease expiration and takeover behavior
- duplicate event delivery and retry semantics
- restart and replay recovery tests

## Pivot Minimum Test Matrix (OSL/OpenClaw-Compatible)
In addition to existing layered tests, the following minimum integration checks
must be present for the generic coordination profile:

1. post a message with valid bearer token
2. reject message post without valid token
3. read by channel filter
4. read with `since_id`
5. long-poll read with `wait_seconds`
6. invalid `limit` and malformed input handling
7. Slack bridge formatting in dry-run mode
8. handoff metadata preservation end-to-end

## BDD Conventions
Use Gherkin feature files with explicit Given/When/Then semantics.

Recommended layout:

- `tests/bdd/features/*.feature`
- `tests/bdd/steps/*.py`

Suggested tooling:
- `pytest`
- `pytest-bdd`

## Cascading Gate Model ("if this passes, it is done")
Each gate must pass before advancing.

1. **Gate A (Schema Gate):** L0 must be fully green.
2. **Gate B (Logic Gate):** L1 must be fully green.
3. **Gate C (Integration Gate):** L2 must be fully green.
4. **Gate D (Behavior Gate):** critical L3 scenarios must be fully green.
5. **Gate E (Resilience Gate):** mandatory L4 chaos/recovery cases must pass.

Release candidate criteria:
- all gates green,
- no critical severity defects open,
- provenance and lock safety acceptance checks passing.

## Minimal CI Matrix
- Python versions supported by deployment target
- SQLite integration job
- Postgres integration job
- optional race/stress job on schedule

## Definition of Done for Core Features
A feature is "done" only when:

1. schema and API contract are documented,
2. L1 and L2 tests exist and pass,
3. at least one L3 BDD scenario demonstrates user-facing value,
4. failure-mode test exists (retry, timeout, conflict, or restart path),
5. observability signal (log/metric) exists for incident diagnosis.

## Suggested Initial Feature Test Backlog
1. Event ingestion + replay by correlation id
2. Task dependency invalidation on contract change
3. Lock acquire/renew/release with lease expiry takeover
4. Unauthorized action rejection by role policy
5. Restart recovery preserves causal provenance chain
6. Message taxonomy validation for required generic `type` values
7. Channel convention conformance for `project.*`, `repo.*`, `task.*`, `agent.*`
8. Slack dry-run emission for blocker/handoff/done/decision/error
9. Coordination entropy benchmark regression:
   - enforce lower entropy score with bus mode
   - enforce higher completion ratio within fixed tick budget with bus mode
