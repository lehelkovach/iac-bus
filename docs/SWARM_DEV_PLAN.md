# Swarm Development Plan

How the IAC Bus gets from "a message relay that a handful of agents can share"
to "coordination infrastructure a swarm of agents can rely on", running as a
hosted service on OCI.

Scope note: `docs/FULL_DEV_PLAN.md` remains the canonical program plan for the
product as a whole. This document is narrower and deeper: it covers only what
multi-agent swarming needs, and every claim in the baseline section below was
measured against the current code rather than inferred from the docs.

Related documents:

- `docs/SWARM_TESTING_PLAN.md` - how each phase below is proven
- `docs/OCI_HOSTING_PLAN.md` - how the service is run and released
- `tests/swarm/test_swarm_gaps.py` - the gaps below, as executable tests

## 1. What "swarming" means here

A **swarm** is a group of agents working the same objective at the same time,
where membership changes during the work and no agent has a complete picture.
That is different from the two-agent handoff the bus was built for, and it is
the difference that drives this plan.

| Term | Meaning in this plan |
| --- | --- |
| Member | One agent process with a stable handle, e.g. `agent:cursor.iac-bus.3@web` |
| Supervisor | A member that decomposes work and adjudicates blockers |
| Worker | A member that claims work from a pool and reports outcomes |
| Work pool | A queue that any capable member may claim from |
| Coordination surface | The bus state members use to agree: queues, channels, jobs, locks |

Four properties separate a swarm from a pair of agents:

1. **Anonymity does not scale.** With three agents you can infer who did what.
   With thirty you cannot, so identity has to be in the protocol.
2. **Chatter grows quadratically.** Status messages scale with member count,
   and they compete with real work for whatever shared resources exist.
3. **Members die mid-task.** At any useful size, something is always failing,
   so recovery is a normal path rather than an exception.
4. **Nobody can poll fast enough.** Latency has to come from the bus pushing
   or blocking, not from members polling harder.

## 2. Verified baseline

Everything in this section was measured against the current `server.py`.
Numbers come from `scripts/swarm_harness.py` on a 4 vCPU container with the bus
in-process; treat the shapes as authoritative and the absolute figures as
optimistic, since a network-deployed bus adds a round trip.

### 2.1 What already works under a real swarm

These hold with 12 concurrent workers over HTTP and are locked in by
`tests/swarm/test_swarm_coordination.py`:

- **Exactly-once claim.** 300 tasks drained by 12 workers produced 300 claims,
  zero duplicates, zero losses. The global lock around the message list is
  coarse but correct.
- **Lease exclusivity.** A leased task is invisible to every other member until
  its lease expires.
- **Takeover with a fresh fencing id.** An abandoned lease is re-offered with a
  new `lease_id`, and the previous holder's ack is rejected with 409.
- **Requeue preserves payload.** `nack` returns work intact.
- **Dependency and barrier semantics.** A fan-out/barrier/fan-in job executed by
  4-6 workers respected `depends_on` and `wait_for` every time, with observed
  parallelism equal to the worker count.

That is a solid core. The gaps below are about scale, durability, and identity,
not about the correctness of the leasing model.

### 2.2 The bus is a serialization point

Throughput is flat no matter how many members join, while latency grows
linearly with swarm size. Every request takes one global lock, and
`_bus_prune()` scans the entire message list on the way in.

| Members | Throughput | claim p50 | claim p95 | claim p99 |
| --- | --- | --- | --- | --- |
| 1 | 387 tasks/s | 1.1 ms | 1.3 ms | 1.5 ms |
| 4 | 338 tasks/s | 5.0 ms | 7.2 ms | 8.2 ms |
| 8 | 342 tasks/s | 9.7 ms | 12.5 ms | 14.2 ms |
| 16 | 337 tasks/s | 19.8 ms | 22.3 ms | 23.6 ms |
| 32 | 329 tasks/s | 39.7 ms | 43.0 ms | 44.4 ms |
| 64 | 286 tasks/s | 85.2 ms | 91.7 ms | 97.4 ms |

**Adding members buys no capacity.** Beyond roughly 16 members the swarm is
just queueing on the bus, and past 32 the wait exceeds the cost of the work for
short tasks. Plan capacity as a fixed ~330 operations/second budget shared by
every member, and treat member count as a latency decision.

### 2.3 Per-request cost grows with retained messages

`_bus_prune()` is O(n) in resident messages and runs on every post, poll, and
claim:

| Resident messages | post | claim | poll |
| --- | --- | --- | --- |
| 500 | 1.3 ms | 1.3 ms | 1.5 ms |
| 5,000 | 1.8 ms | 1.9 ms | 2.3 ms |
| 20,000 | 3.7 ms | 4.2 ms | 5.3 ms |
| 50,000 | 7.3 ms | 8.5 ms | 11.4 ms |

This matters because raising `BUS_MAX_MESSAGES` is the only available mitigation
for the task loss described next. The stopgap is real but bounded: it is
comfortable to about 20,000 resident messages and painful beyond that.

### 2.4 Gaps, in severity order

Each has a matching strict-xfail test in `tests/swarm/test_swarm_gaps.py`, so
"is it fixed yet" is answered by running the suite rather than reading a doc.

**G1. Queued work is silently deleted to make room for chatter.** (phase S3)
`_bus_prune()` trims the shared buffer to `BUS_MAX_MESSAGES` with no regard for
message status, so pending and leased queue entries are evicted alongside old
status updates. Measured: with a 50-message buffer, 250 of 300 posted tasks
vanished before any worker saw them, with no error and no dead-letter record.
The default buffer is 500, and a busy swarm produces chatter far faster than
that. This is the gap most likely to cause a silent production incident.

**G2. All coordination state is in process memory.** (phase S3)
A restart, crash, or hot-reload discards every pending task, lease, and
orchestration job. The dev VM runs `watchmedo auto-restart`, so on that host any
file change wipes the swarm's work queue. Nothing in the API tells a member this
happened.

**G3. Agent identity is dropped on the floor.** (phase S1)
Every example in the README's "Agent Collaboration Guide" posts an `agent`
handle. `_normalize_message_payload` only reads `sender`, so those messages are
stored with the literal sender `"agent"`. `metadata` and `ref` are discarded
entirely, which means the documented blocker and handoff patterns lose exactly
the payload that makes them useful. Members are anonymous and `agent.<id>`
routing cannot be built on the current envelope.

**G4. Reads cannot block.** (phase S2)
`wait_seconds` is accepted and ignored. Members must busy-poll, which costs
`members / interval` requests per second against the fixed throughput budget in
2.2, and adds up to half a poll interval to every handoff.

**G5. An unusable cursor replays the whole window.** (phase S2)
The `since_id` lookup is a linear scan of the filtered view; on failure the
handler falls through and returns everything. A cursor that aged out, or that
came from a different view, silently converts an incremental read into a full
replay, and the member reprocesses work it already did.

**G6. Priority is stored and then ignored.** (phase S3)
`priority` is validated, persisted, and copied into orchestration assignments,
but `_claim_queue_message` returns the oldest pending entry regardless. Urgent
work queues behind the backlog, and the gap is invisible until latency matters.

**G7. Membership is unknowable.** (phase S4)
There is no registry, presence, or heartbeat. A supervisor cannot size a
fan-out to available workers, notice a dead member, or address one directly.

**G8. Backlog is unobservable.** (phase S5)
`/health` reports one total message count mixing chatter with work. There is no
per-queue depth, lease count, or backlog age, so the swarm cannot be scaled on
evidence and an operator cannot see a stuck queue.

**G9. One shared static token.** (phase S6)
Every member presents the same bearer token, so there is no per-agent
authentication, no revocation of a single misbehaving member, and no ACL.

## 3. Design constraints

- **Stay application-agnostic.** Per `docs/FULL_DEV_PLAN.md` section 1.1, no
  KSG, OpenClaw, or repo-specific logic in bus core.
- **Do not break existing members.** `sender` keeps working while `agent`
  becomes canonical; new response fields are additive.
- **Prefer boring, provable mechanisms.** Leases with fencing tokens and an
  append-only ledger, not consensus protocols.
- **Every phase ships with its own failure-mode test.** A feature without a
  timeout, conflict, or restart test is not done.

Non-goals for this plan: a general workflow engine, semantic memory in the bus,
agent-to-agent RPC, or scheduling policy beyond priority and fairness.

## 4. Phases

Phases are ordered by (risk removed) / (effort), and each names the OCI hosting
phase it unblocks. Exit criteria are written as test outcomes.

### S0 - Instrumented baseline (this change)

**Problem.** Nothing could tell us whether the bus was safe for a swarm, so
every claim about it was an assumption.

**Deliverables.**
- `scripts/swarm_harness.py`, three scenarios with invariant checking
- `scripts/bus_conformance.py`, contract checks plus capability probes
- `tests/swarm/`, real-HTTP concurrency tests and the gap ledger
- `bus_client.py`, one correct client for tooling and tests

**Exit criteria.** Met. 8 coordination tests pass, 9 gap tests xfail, 16
conformance checks pass against a live bus, and the harness is proven to detect
both task loss and dropped broadcasts.

### S1 - Agent-addressable envelope

**Problem.** G3. The documented protocol and the implemented protocol disagree,
and the implemented one loses identity.

**Scope.**
- Accept `agent` as the canonical sender handle; keep `sender` as an accepted
  alias and echo both, so no existing member breaks.
- Preserve `metadata` and `ref` through post, storage, and poll.
- Validate `type` against the taxonomy in `docs/FULL_DEV_PLAN.md` section 2.1,
  rejecting unknown types with 400 and a list of valid values.
- Make `recipient` and `group` filterable on read, so direct addressing works:
  `GET /bus/messages?recipient=agent:...`.
- Update `schemas/iac-bus.schema.json` and bump the protocol to `iac-bus/1.2`.

**Exit criteria.**
- `test_agent_handle_survives_a_post` and `test_handoff_metadata_survives_a_post`
  pass with their xfail markers removed.
- Conformance reports `agent identity preserved` and `metadata/ref passthrough`.
- A compatibility test proves a `sender`-only member still works unchanged.

**Risk.** Low. Additive, and the schema tests catch drift.

### S2 - Blocking reads and honest cursors

**Problem.** G4 and G5. Members burn the shared throughput budget on polling,
and a stale cursor causes silent duplicate work.

**Scope.**
- Implement `wait_seconds` on `GET /bus/messages`: block until a matching
  message arrives or the timeout elapses, capped server-side (30s suggested).
  Use a condition variable signalled by the writer, not a sleep loop.
- Give every message a monotonic sequence number alongside its id, so a cursor
  is an integer comparison instead of a scan. Keep `since_id` working by
  resolving it to a sequence.
- When a cursor cannot be resolved, say so: return `409` with
  `{"error": "stale_cursor", "resume_from": <earliest retained seq>}` rather
  than replaying. Members then resynchronise deliberately.
- Document the reconnect pattern in the README playbook.

**Exit criteria.**
- `test_read_can_block_until_a_message_arrives` and
  `test_an_unusable_cursor_is_reported_instead_of_replayed` pass unmarked.
- Harness fanout with 16 subscribers shows a reduction in poll requests per
  observed message of at least 10x versus the S0 baseline.
- Conformance reports `server-side long poll`.

**Risk.** Medium. Long-poll holds threads open; a request that blocks for 30s
occupies a worker thread, which interacts with the deployment model in
`docs/OCI_HOSTING_PLAN.md`. Size the thread pool for
`concurrent_members * 1.5` and cap `wait_seconds`.

### S3 - Durable, prioritised work state

**Problem.** G1, G2, and G6. Work is lost to chatter, lost to restarts, and
scheduled in the wrong order. This phase carries most of the risk reduction in
the plan.

**Scope.**
- **Separate work from chatter.** Queue entries move to their own store and are
  never evicted by retention. Only `published` broadcast messages age out.
- **Bound the work store explicitly.** When it is full, reject the post with
  `429` and a `Retry-After`, so a producer learns about backpressure instead of
  discovering loss later.
- **Order claims by `(priority desc, enqueued_at asc)`,** and index by queue so
  a claim stops scanning every message.
- **Persist through a storage adapter,** SQLite first, behind the same
  interface a Postgres adapter will implement. Persist queue entries, leases,
  and orchestration job state. Broadcast retention may stay in memory.
- **Recover leases on startup:** anything leased with an expired lease returns
  to pending; anything leased with a live lease keeps its remaining time.
- **Dead-letter queue** after a configurable nack count, with the reason
  retained, so poison tasks stop cycling.

**Exit criteria.**
- `test_queued_work_is_not_evicted_by_chatter`,
  `test_high_priority_work_is_claimed_first`, and
  `test_pending_work_survives_a_restart` pass unmarked.
- Harness flood with a deliberately small broadcast buffer reports zero
  `task_loss` while chatter is being evicted normally.
- A restart injected mid-flood loses no tasks (new L4 scenario).
- Claim latency at 20,000 resident messages is within 1.5x of the 500-message
  figure, proving the scan was removed.

**Risk.** High, and this is where the design should be reviewed before coding.
Persisting on the request path will cost latency against an already-serialized
service; batch commits and keep the hot path in memory with the store as the
durable log.

### S4 - Membership and presence

**Problem.** G7. Supervisors cannot see or address the swarm.

**Scope.**
- `POST /bus/agents/register` returning a stable `agent_uuid` for a handle,
  with role, capabilities, and an optional parent handle.
- `POST /bus/agents/heartbeat` with a TTL; `GET /bus/agents` listing members
  with `last_seen` and derived `alive` status.
- Route by handle: posting with `recipient` delivers to that member's view.
- Groups: a member may join named groups, and a supervisor may address a group.
- On TTL expiry, emit a `member_lost` event on `ops` so supervisors can react,
  and release that member's leases immediately rather than waiting them out.

**Exit criteria.**
- `test_swarm_membership_is_queryable` passes unmarked.
- A new BDD scenario: a supervisor sizes a fan-out to the number of live
  workers, one worker is killed, and its in-flight step is reassigned within
  one heartbeat interval.
- Conformance reports `agent presence registry`.

**Risk.** Low to medium. The main question is whether presence is authoritative
or advisory; start advisory, since leases already provide the safety property.

### S5 - Swarm observability

**Problem.** G8. Nobody can see the swarm, so nobody can size or debug it.

**Scope.**
- `GET /bus/queues/stats`: per queue, `pending`, `leased`, `dead_lettered`,
  oldest pending age, claim rate.
- `GET /metrics` in Prometheus format: request counts and latency histograms by
  endpoint, queue depths, member count, lock wait time.
- Structured JSON logs with `agent`, `conversation_id`, and queue on every
  coordination event.
- Admin controls: pause and drain a queue, expire a lease, requeue a
  dead-lettered task.

**Exit criteria.**
- `test_queue_depth_is_observable` passes unmarked.
- OCI dashboard shows queue depth and member count (see hosting plan phase H3).
- An operator can answer "why is this swarm stalled" from metrics alone, proven
  by a game-day exercise against a deliberately stuck queue.

**Risk.** Low.

### S6 - Scale-out and safety

**Problem.** The remaining ceilings: the single-process limit from 2.2, and G9.

**Scope.**
- **Remove the single-process constraint.** With S3's store in place, move the
  authority for queue state into it so more than one bus process can serve the
  same swarm. This is what makes the OCI load-balanced topology (hosting phase
  H4) possible; until then, extra processes would each hold their own state.
- **Per-agent credentials** issued at registration, revocable individually,
  with channel and queue ACLs by role.
- **Rate limits and backpressure** per member and per queue, so one runaway
  member cannot consume the shared budget.
- **Lock manager** for repo critical sections: acquire, renew, release, with
  lease TTL and fencing tokens, per `docs/FULL_DEV_PLAN.md` W6.

**Exit criteria.**
- Two bus processes serving one queue deliver every task exactly once under the
  harness flood scenario.
- A member with a revoked credential is rejected while the rest continue.
- Concurrent lock contention tests demonstrate stale-writer prevention.

**Risk.** High. Multi-process correctness depends entirely on S3's store having
the right transactional semantics; do not start S6 before S3 is proven.

## 5. Sequencing

```
S0 ──> S1 ──> S2 ──┐
                   ├──> S4 ──> S5 ──> S6
       S3 ──────────┘
```

S1 and S3 are independent and can proceed in parallel. S2 depends on S1 only
for the envelope it returns. S4 depends on S2 for efficient presence updates and
on S3 for durable registration. S6 depends on S3.

Recommended order if work is serialized: **S1, S3, S2, S4, S5, S6.** S3 moves
ahead of S2 because silent task loss is a correctness problem while polling
overhead is only a performance one.

Hosting dependencies, detailed in `docs/OCI_HOSTING_PLAN.md`:

| Swarm phase | Unblocks hosting phase |
| --- | --- |
| S0 | H1 - release gating on conformance and harness |
| S3 | H2 - a service that survives restart and patching |
| S5 | H3 - monitoring, alerting, and evidence-based scaling |
| S6 | H4 - load-balanced, multi-instance, zero-downtime deploys |

## 6. Capacity guidance for today

Until S3 and S6 land, size swarms against the measured baseline:

- **Budget ~330 bus operations/second in total,** shared by all members.
- **Keep swarms at or below 16 active members** per bus. Beyond that, latency
  grows without any throughput gain.
- **Compute the poll cost first.** `members / poll_interval` requests per second
  come out of the budget before any work happens. Sixteen members polling every
  500 ms spend 32 ops/s, about 10% of capacity, doing nothing.
- **Raise `BUS_MAX_MESSAGES` to cover peak in-flight work plus chatter within
  the retention window,** and keep resident messages under ~20,000 so per
  request cost stays near 4 ms. Lower `BUS_RETENTION_SECONDS` in preference to
  raising the cap.
- **Treat the bus as ephemeral.** Anything that must survive a restart belongs
  in the repository or an external store until S3 lands.

## 7. Decisions still open

1. **Storage engine for S3.** SQLite is simplest and matches the single-VM
   deployment, but S6 multi-process wants Postgres or Oracle Autonomous DB.
   Choosing Postgres immediately avoids one migration; choosing SQLite ships
   durability sooner.
2. **Is presence authoritative?** If a supervisor may assume a member is dead on
   TTL expiry, presence needs to be as reliable as leases. Advisory presence
   plus authoritative leases is the safer default.
3. **Push transport.** S2 long-poll is deliberately the cheap option. SSE or
   WebSocket would cut latency further, at the cost of connection state on a
   service that currently has none.
4. **Where fairness lives.** Strict priority ordering can starve low-priority
   work. Aging or weighted fair queueing should be decided during S3 rather
   than retrofitted.
