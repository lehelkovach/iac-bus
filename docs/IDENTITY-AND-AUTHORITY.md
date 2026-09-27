# Agent identity and authority: design and dev ladder

_Written 2026-09-27. Status: plan. Nothing below is implemented unless a rung is marked done._

This joins two designs that were written separately and never merged:

- **The ACP v2 registrar.** `docs/ACP_PROTOCOL_V2.md` §2 and §4.1, and
  `docs/sql/ACP_V2_SCHEMA.sql`. It gives each agent an immutable `agent_uuid`,
  a logical handle `agent:<brand>.<repo>.<ordinal>@<medium>`,
  `parent_agent_uuid`, endpoints, and an idempotent `POST /agents/register`.
  A SQLite implementation exists in `store.py` on
  `cursor/iac-bus-v0-1-coordination-ba28` (PR #8, closed unmerged).
- **The agent extranet protocol.** `docs/AGENT-EXTRANET-PROTOCOL.md`,
  `docs/CODING-AGENT-KICKSTART.md` and `schemas/agent-extranet.schema.json` on
  `agent/agent-extranet-protocol` (PR #11, closed unmerged). It covers
  principals, agent bindings, attenuating grants, the
  proposal → approval → receipt chain, and 14 security invariants.

## Scope: IPC for agents on one project

Today iac-bus coordinates several agents working on the **same project** so
they stay in sync and do not conflict. That is inter-process communication,
with agents as the processes:

| IPC concept | iac-bus equivalent | Status |
|---|---|---|
| process id / parent pid | `run_id` / `parent_run_id` | planned (ID1–ID2) |
| process liveness | run heartbeat and expiry | planned (ID2) |
| work queue / semaphore | queue `claim` / `ack` / `nack` with leases | built (L0) |
| mutex on a shared resource | path or resource **reservation** with a lease | planned (ID3) |
| generation counter against stale writers | **fencing token** checked at the write boundary | planned (ID3) |
| condition variable / join | `wait` (long-poll) and **barriers** | long-poll on L1 branch; barriers planned (ID4) |
| task graph / dependencies | orchestration jobs and ready-step evaluator | built (`orchestration.py`) |
| signals (cancel, pause, resume) | `CANCEL_TASK`, `PAUSE_TASK`, `RESUME_TASK` actions | message contract only (`agent_contract.py`) |
| audit log | append-only event ledger | planned (ID5) |

So the first priority is making those primitives correct: exactly one holder
of a lock, stale holders fenced out, and every message and lock attributable
to a real run. Identity is needed only as far as that requires.

The extranet protocol's authority layer covers grants, human approvals of
exact actions, signed receipts and federation. It matters once the bus
coordinates agents across **trust boundaries**: outside users, other
organisations, or real side effects like payments and merges. Here it is a
separate, later track (Track B).

The goal is one record set that serves both tracks. Every message, lock and
claim on the bus must answer **which run did it and which agent that is**.
Track B adds **on whose authority, traced back to which human**.

## Gaps in the two designs

1. **No run.** ACP models a persistent agent and a session endpoint. The
   extranet models agents and grants. Neither models one execution: a Claude
   Code session, a `claude -p` call, an OSLO swarm replica or a Cursor cloud
   agent. A run has a start, an end, a launcher and a lease, and it is the unit
   to attribute, revoke and audit.
2. **`parent_agent_uuid` does two jobs.** It records structure (where the
   agent sits in the tree, which the ordinal path encodes). It is also read as
   authority (who allowed this agent to act). Authority must come from the
   grant chain. The parent pointer stays for display and routing only.
3. **Registration is unauthenticated.** `POST /agents/register` accepts the
   shared `BUS_API_TOKEN`, so any holder can register or post as any handle.
   That breaks extranet invariants 1 and 2: `sender` is never an identity, and
   the bus derives the actor from a verified binding.
4. **"Author" is three different roles.** They need separate fields:
   - the **controller**, the human or org accountable for the agent;
   - the **publisher**, who built the code, prompt or skill it runs (with a
     `build_digest`);
   - the **delegator**, who launched this particular run and handed it
     authority.

## Record model

IDs are ULIDs, which sort by time, in the `urn:iac:` scheme the extranet spec
already uses. ACP handles are kept as display aliases.

| Record | Key fields | Notes |
|---|---|---|
| **principal** | `principal_id` (`urn:iac:principal:<ULID>`), `kind` (human, organization, service), `external_id`, `status` | The accountable root. The first `external_id` source is a KnowShowGo developer-portal account (`ksg:<ownerUserId>`), so the owner of a private KSG memory overlay and the root of a bus grant chain are the same person. |
| **agent** | `agent_id` (`urn:iac:agent:<ULID>`), `controller_principal_id`, `published_by`, `build_digest`, `handle` (ACP alias), `parent_agent_id` (structure only), `status` | A persistent definition. It changes only when its build changes. |
| **run** | `run_id` (`urn:iac:run:<ULID>`), `agent_id`, `initiator` (a `principal_id` or a `parent_run_id`), `grant_id`, `medium`, `session_ref`, `token_hash`, `lease_id`, `fencing_token`, `started_at`, `heartbeat_at`, `ended_at`, `status` | One execution. It ends. An ended run's token stops working immediately. |
| **grant** (Track B) | `grant_id`, `issuer` (a principal or a run), `subject_run_id`, `capabilities[]`, `resources[]`, `constraints` (`deny`, `max_uses`, `delegation_depth`, `requires_approval_for`), `expires_at`, `parent_grant_id`, `digest`, `revoked_at` | Authority. A child grant is always equal or narrower on every axis. Revoking a grant revokes everything below it. |

The **accountability chain** is `run → agent → grant → parent grant … → root
principal`. A new `GET /v2/runs/{run_id}/lineage` returns that chain, and every
receipt carries enough digests to rebuild it.

ACP v2's conflict matrix and handle rules still apply to the `handle` alias.
The `agents` and `agent_endpoints` tables from PR #8 migrate forward:
`agent_uuid` becomes `agent_id`, and endpoints become `run.medium` and
`run.session_ref`.

## How an agent gets its identity: the launcher mints the run

An agent never asserts who it is. Whoever starts it mints its run:

- **A human starts an agent.** For example, a human opens Claude Code or OSLO
  with their own token. The bus's MCP endpoint or `iac-bus run start` creates a
  run whose `initiator` is that principal, and returns a **run token**.
- **An agent starts an agent.** A supervisor run calls
  `POST /v2/runs {agent_id, parent_run_id, grant}` with a grant that must be a
  subset of its own. The bus checks the attenuation and returns `run_id` plus a
  short-lived run token. The runner or bridge injects these into the worker it
  launches as `IAC_RUN_ID` and `IAC_RUN_TOKEN`. `scripts/spawn-cursor-agents.py`
  is the first launcher to adopt this.
- **On every later request,** the bus derives the actor from the run token. A
  caller-supplied `sender` is kept only as a display alias. This matches the
  extranet compatibility table.

Run tokens are opaque, stored hashed, time-limited, bound to one run, and
renewed by heartbeat. They are session authentication, not delegated
authority: authority always comes from the grant.

## Signatures now or later

`CODING-AGENT-KICKSTART.md` orders canonical bytes and Ed25519 signatures
early (WP2). This plan defers signatures:

- **While the bus is the only verifier:** grants, approvals and receipts are
  server-side records with **RFC 8785 canonical-JSON digests**, stored in an
  append-only ledger. Every extranet invariant still holds, because nothing
  outside the bus has to check them.
- **When a second verifier exists** (an executor on another host, a federated
  bus, an A2A peer): add detached JWS/Ed25519 signatures over the same digests,
  and passkey-signed approvals (HumanKey-lite). Because digests exist from day
  one, no schema changes.

This reverses the kickstart's WP ordering, not its requirements. Track A needs
no signatures at all. The ADR for kickstart item 2 records the decision when
Track B starts.

## Threat model: now versus later

**Now.** There is one operator, the agents are your own, and the bus is on a
public IP (`:8101`) behind one shared token. Deliberate impersonation is not
the main risk. These are:

1. **Agents act on messages.** A message claiming to come from `supervisor`
   that does not is a prompt-injection path into an agent that will act on it.
   A server-derived sender closes it.
2. **Attribution.** Handles collide, stale sessions keep posting, and history
   cannot be trusted afterwards. Adding identity later cannot fix records
   written before it existed.
3. **A leaked shared token** lets anyone post as any agent. Per-run tokens
   limit the damage to one run for its lifetime.

These justify rungs ID1–ID2 now. Both are cheap and involve no cryptography.
Locks and claims are worthless if any caller can release another run's lock,
so real run identity is a correctness requirement for IPC, not just a
security feature.

**Later.** Outside users or organisations, paying tenants, cross-host
executors, and approvals of real side effects (payments, submissions,
merges). These justify Track B (B1–B3), with signatures (B3) only when a
second verifier exists.

## Dev ladder

Every rung starts with a failing test, then the implementation, then green on
`dev`. The "Existing rung" column maps onto the L-ladder in `docs/PROGRESS.md`
on `master`.

### Track A: coordination for one project (now)

| Rung | Existing rung | Build | Gate (the test that must fail first) |
|---|---|---|---|
| **ID0** | L5 (store) | Consolidate: `dev` plus the SQLite `store.py` from PR #8 (it already has `agents`, `agent_endpoints` and `resource_locks`), plus the long-poll and metrics work from PR #17. Write two ADRs: the ID format (ULID URNs) and the run model (this doc). | The legacy tests pass on the merged base. Queued messages, claims and locks survive a restart. |
| **ID1** | L2 | Registry: `principals`, `agents` and `runs` tables with migrations. ACP handle rules and the conflict matrix apply to `handle`, which is only an alias. | IDs are immutable. A handle cannot be re-pointed to another agent. |
| **ID2** | L2 | Run tokens (hashed, time-limited, renewed by heartbeat). The sender is derived from the token. `POST /v2/runs` lets an existing run launch a child run. `spawn-cursor-agents.py` mints runs for the agents it starts. | A spoofed `sender` is ignored. Tokens for an expired, ended or foreign run are rejected. A child run records its parent run and initiator. A dead run's claims and leases expire. |
| **ID3** | L4 | Path and resource reservations with leases and **fencing tokens**, owned by runs. A write to a protected resource carries the fencing token. | Only one run holds a reservation. After a lease expires and another run takes over, the old run's writes are rejected (kickstart WP6 exit). A run cannot release another run's reservation. |
| **ID4** | L3 | Barriers and task dependencies: `wait` long-poll, a barrier that releases when N runs arrive, and invalidating dependent tasks when an input changes. Cancel, pause and resume messages reach a run's queue. | The `scripts/dogfood_litmus.sh` pattern (on `master`: parallel workers, then a barrier sync) passes against the bus with run identities. A cancelled task is not claimable. |
| **ID5** | L5 | Event ledger and lineage: an append-only table with causal ids and content digests, and `GET /v2/runs/{id}/lineage` (run → parent runs → initiating principal). | The history rebuilds after a restart. A replayed or duplicate write is deterministic. |
| **ID6** | new | MCP face: `POST /mcp` on the bus (Streamable HTTP, the same pattern as KnowShowGo's), authenticated by run token. Tools: `post`, `inbox`, `claim`, `ack`, `nack`, `progress`, `wait`, `reserve`, `release`. A runner or bridge (`iac-bus run --queue … -- <command>`) mints child runs and wakes agents. | An MCP tool call is attributed to the calling run. A runner-launched `claude -p` worker claims, works and acks under its own run id. |

Track A delivers what the bus was designed for: several agents on one
repository, each with a real identity, that cannot take the same task, cannot
edit the same paths at once, and can wait on each other. It needs no
cryptography.

### Track B: authority across trust boundaries (later, only when needed)

This track is the extranet protocol and kickstart WP3, WP5, WP7 and WP8. Start
it when the bus coordinates agents that are not all yours, or approves real
side effects.

| Rung | Build | Gate |
|---|---|---|
| **B1** | Grants: issue, delegate only narrower (capabilities, resources, expiry, `max_uses`, depth), revoke with cascade. Enforce grants on claims and reservations. | The extranet negative matrix: widened, expired, revoked, depth-exceeded and wrong-audience grants are all rejected. |
| **B2** | Proposal → policy → approval → execution receipt. Approvals come from Slack, the web or MCP elicitation, outside the agent's context. KnowShowGo accepts `X-KSG-Session: <run_id>` and verifies it against the bus. | The kickstart's first end-to-end scenario: two coding agents open a draft PR, and replay, wrong repo, widened path, expired or revoked grant, altered body and stale fencing token are all rejected. |
| **B3** | Ed25519/JWS over the existing digests, passkey approvals (HumanKey-lite), then A2A and federation. | Signature test vectors pass. An approval completes without the agent ever holding a signing key. |

### Options

- **Minimal:** ID0–ID3. Real run identity plus locks and fencing. This is the
  core of "agents on one project do not conflict".
- **Recommended:** ID0–ID6. Adds barriers, an audit ledger, and use from any
  MCP client or runner.
- **Full spec:** Track A, then Track B. Only once agents from outside your
  trust boundary join.

### Out of scope, unchanged

- KeyChain (`key-chain-network`) is not a dependency (`AGENTS.md`).
- No custom chain or new DID method.
- No secrets in messages, logs or prompts.
- Git branches, CI and PR review remain authoritative for source changes.
  Reservations reduce collisions; they do not replace version control.

## Relation to the rest of the stack

- **KnowShowGo** (`POST /mcp`, `docs/MCP.md` in `knowshowgo`) is shared memory.
  Its `agent` argument is free text today. Passing the `run_id` there gives
  attribution in Track A; verified run ids arrive in B2. KSG references never
  grant authority (extranet invariant 11).
- **OSLO** (`osl-oc-agent`) consumes MCP servers through `mcp.call` and has
  its own stdio MCP server. Its plan parks iac-bus until after the first
  paying customer (Gate D), and routes OSLO's internal swarm messages over
  NATS (orchestration ladder rung G). The split: NATS carries OSLO-internal
  traffic, and iac-bus is the between-agents control plane for identity,
  authority, approvals and receipts.
