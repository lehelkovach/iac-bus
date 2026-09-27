# Continuity (iac-bus)

**What iac-bus is today:** IPC for agents on one project. It keeps parallel
agents in sync and out of each other's way with queues, claims, leases,
reservations, barriers and a shared message log.

**Plan of record:** [`docs/IDENTITY-AND-AUTHORITY.md`](../docs/IDENTITY-AND-AUTHORITY.md)
(2026-09-27). It merges the ACP v2 registrar (PR #8) and the agent extranet
protocol (PR #11), both closed unmerged, into one record model:
principal → agent → run, plus grants in Track B.

**Next:** ID0, consolidating onto `dev`:
- the SQLite `store.py` from `cursor/iac-bus-v0-1-coordination-ba28`;
- the long-poll and metrics work from `cursor/ladder-ops-metrics-e357`;
- the extranet schemas and fixtures from `agent/agent-extranet-protocol`.

Then ID1–ID3 (registry, run tokens, reservations with fencing). Track B
(grants, approvals, signatures) waits until agents from outside your trust
boundary join.

**Also on branches, unmerged:**
- `skills/openclaw/iac_bus.py`, an HTTP client and CLI (extranet branch);
- `scripts/swarm_harness.py` (swarm branch).

**Waking agents:** neither MCP nor the bus can start an agent's turn. The
waking piece is a runner or bridge that mints a run and launches or resumes
the agent (ID6).
