# Swarm Testing Plan

How multi-agent swarming on the IAC Bus is verified, from a laptop through CI to
the OCI-hosted service.

`docs/TESTING_STRATEGY.md` defines the general L0-L4 gate model for the product.
This plan is the swarm-specific instance of it: what to test when many agents
share one bus, which tools do it, and what "passing" means numerically.

## 1. What swarm testing has to catch

Single-agent tests cannot see the failures that actually hurt a swarm, because
those failures need concurrency, time, or scale to appear:

| Failure | Why single-agent tests miss it | Covered by |
| --- | --- | --- |
| Two agents run the same task | Needs simultaneous claims | `test_every_task_is_delivered_to_exactly_one_worker` |
| A task is never run | Needs the buffer to overflow | harness `flood`, `test_queued_work_is_not_evicted_by_chatter` |
| A dead agent strands work | Needs a lease to expire | `test_abandoned_work_is_recovered_by_another_agent` |
| A revived agent corrupts finished work | Needs a stale fencing token | `test_the_original_agent_cannot_ack_after_losing_its_lease` |
| Dependent work starts too early | Needs real parallelism | `test_swarm_executes_a_dependency_graph_in_order` |
| A subscriber misses or replays messages | Needs retention pressure | harness `fanout` |
| A deploy shards state across processes | Needs multiple connections | conformance `all connections share one coordination state` |

The organising principle: **every coordination guarantee is a test, and every
missing guarantee is a test too.** The second half is what section 4 is about.

## 2. Tooling

Three tools, each with one job.

### `tests/swarm/` - correctness under concurrency

Pytest suites that start a real HTTP bus on an ephemeral port (`tests/swarm/conftest.py`)
and drive it with threads. Real sockets, real leases, real timing; Flask's
in-process test client is not used, because it does not reproduce concurrent
socket behaviour.

```bash
python3 -m pytest tests/swarm -q          # everything
python3 -m pytest -m swarm -q             # swarm tests wherever they live
```

The `bus_factory` fixture starts a bus with custom settings, which is how tests
reproduce resource pressure:

```python
bus = bus_factory(BUS_MAX_MESSAGES=50)    # a deliberately starved bus
```

### `scripts/swarm_harness.py` - behaviour at scale

Drives a bus with many concurrent agents and reports invariant violations.
Targets any URL, so the same command validates a laptop, CI, staging, and prod.

| Scenario | Shape | Catches |
| --- | --- | --- |
| `flood` | N producers, W workers draining a queue | lost work, duplicate delivery, unfair distribution |
| `fanout` | one publisher, S subscribers on cursors | missed broadcasts, replays, ordering |
| `dag` | fan-out, barrier, fan-in job run by W workers | dependency and barrier violations, duplicate execution |

```bash
# no deployment required; starts its own bus
python3 scripts/swarm_harness.py all --embedded

# against a deployed bus
python3 scripts/swarm_harness.py flood \
    --bus-url https://bus-staging.example --token "$BUS_API_TOKEN" \
    --workers 16 --tasks 500 --json flood.json
```

It exits non-zero when an invariant is violated, so it gates a deploy directly.
`--no-fail` reports without failing, for exploring a known-degraded environment.

The harness is itself tested (`tests/swarm/test_swarm_harness.py`) in both
directions: clean on a healthy bus, and *loud* on a starved one. A detector that
quietly stops detecting is worse than none.

### `scripts/bus_conformance.py` - is this deployment correct

Run after every deploy. It separates two questions:

- **Required checks** verify the protocol contract. A failure should block or
  roll back the release. Currently 15 checks, or 16 with `--slow`: auth,
  cursors, queue visibility, the claim/ack/nack lifecycle, fencing, validation,
  orchestration ordering, the single-coordination-domain check, and lease
  expiry (the `--slow` one).
- **Capability probes** report which optional swarm features the build has, and
  never fail the suite. This is how an operator tells which version is deployed,
  and how progress through `docs/SWARM_DEV_PLAN.md` becomes visible in an
  environment.

```bash
python3 scripts/bus_conformance.py --bus-url "$BUS_URL" --token "$BUS_API_TOKEN" --slow
```

Against the current build all required checks pass and all 7 capability probes
report absent, each naming the plan phase that will deliver it.

## 3. Test layers

Extending the L0-L4 model in `docs/TESTING_STRATEGY.md`:

| Layer | Question | Where |
| --- | --- | --- |
| L0 Schema | Is the payload legal? | `tests/test_iac_bus_schema.py` |
| L1 Unit | Is the logic right in isolation? | `tests/test_orchestration.py`, `tests/test_agent_contract.py` |
| L2 Integration | Does the endpoint behave? | `tests/test_bus.py`, `tests/test_orchestration_bus.py` |
| L3 Behaviour | Does a realistic scenario work? | `tests/swarm/test_swarm_coordination.py` |
| **L4 Concurrency** | Does it hold with many agents at once? | `tests/swarm/`, harness |
| **L5 Deployment** | Is *this* running instance correct? | `bus_conformance.py` |
| **L6 Capacity** | How much swarm can it carry? | harness sweeps, section 6 |

L5 is new and important: layers 0-4 test the code, L5 tests the deployment.
Most of the hazards in `docs/OCI_HOSTING_PLAN.md` are configuration mistakes
that no amount of code testing would catch.

## 4. The gap ledger

`tests/swarm/test_swarm_gaps.py` holds one test per known missing capability,
marked `xfail(strict=True)`. The mechanism:

- While the gap exists, the test xfails and the suite stays green.
- The moment someone implements the feature, the test XPASSes, which pytest
  reports as a **failure**, prompting whoever did the work to delete the marker
  and turn it into a permanent guarantee.

This keeps the plan and the code honest in both directions: a gap cannot be
quietly forgotten, and a fix cannot land without the test that proves it. Each
test names the phase in `docs/SWARM_DEV_PLAN.md` that closes it.

Current ledger:

| Test | Gap | Phase |
| --- | --- | --- |
| `test_agent_handle_survives_a_post` | agent identity is discarded | S1 |
| `test_handoff_metadata_survives_a_post` | `metadata` and `ref` are discarded | S1 |
| `test_read_can_block_until_a_message_arrives` | no server-side long poll | S2 |
| `test_an_unusable_cursor_is_reported_instead_of_replayed` | stale cursor replays silently | S2 |
| `test_queued_work_is_not_evicted_by_chatter` | retention deletes pending work | S3 |
| `test_high_priority_work_is_claimed_first` | priority ignored when claiming | S3 |
| `test_pending_work_survives_a_restart` | no durable state | S3 |
| `test_swarm_membership_is_queryable` | no presence registry | S4 |
| `test_queue_depth_is_observable` | no queue statistics | S5 |

**Working rule:** implementing a phase means removing the marker from its tests
in the same change. A phase whose gap tests still xfail is not done.

## 5. Acceptance thresholds

Derived from measurements on a 4 vCPU host with an in-process bus (see
`docs/SWARM_DEV_PLAN.md` section 2). Thresholds sit below observed performance
so they catch regressions without failing on noise. Re-baseline per environment
before enforcing in prod, since a network round trip changes the absolute
numbers.

**Correctness, never negotiable:**

| Metric | Threshold |
| --- | --- |
| Tasks lost (flood) | 0 |
| Tasks delivered more than once (flood) | 0 |
| Steps executed more than once (dag) | 0 |
| Dependency or barrier violations (dag) | 0 |
| Missed or duplicated broadcasts (fanout, within capacity) | 0 |
| Required conformance checks failing | 0 |

**Performance, environment-specific:**

| Metric | Threshold | Observed |
| --- | --- | --- |
| Throughput, 8 workers | >= 250 tasks/s | 342 |
| claim p95, 8 workers | <= 25 ms | 12.5 |
| claim p95, 16 workers | <= 40 ms | 22.3 |
| Idle workers during a flood | 0 | 0 |
| Observed parallelism (dag, width >= workers) | >= workers - 1 | = workers |

**Known scaling behaviour, asserted so a change is noticed:** throughput must
stay flat rather than collapse as members increase, and claim latency should
grow roughly linearly with member count. A sudden change in either shape means
the locking behaviour changed.

## 6. Capacity testing

Run the sweep when the concurrency model changes, and before committing to a
swarm size in a new environment.

```bash
for w in 1 4 8 16 32 64; do
  python3 scripts/swarm_harness.py flood --embedded \
      --workers "$w" --tasks 400 --json "sweep-$w.json"
done
```

Report throughput and claim p50/p95/p99 per member count. The expected profile
today is flat throughput with linearly growing latency; the point where p95
exceeds the acceptable handoff latency for the workload is the practical member
ceiling for that environment.

A second sweep measures the cost of retention, which matters because raising
`BUS_MAX_MESSAGES` is the current mitigation for task loss. Fill the bus to a
target resident count, then measure post, claim, and poll latency. Current
profile: about 1.3 ms at 500 resident messages rising to 7-11 ms at 50,000.

## 7. Failure injection

Each entry is a scenario the swarm must survive. Those marked *expected to fail
today* are the acceptance tests for their swarm phase, and should be added to
the automated suite as that phase lands.

| Injection | Expected behaviour | Status |
| --- | --- | --- |
| Kill a worker holding a lease | Another member takes over after expiry, with a new fencing id | Passing |
| Stale worker acks after takeover | Rejected with 409 | Passing |
| Worker nacks repeatedly | Task requeues; after N attempts it dead-letters | Dead-letter pending, S3 |
| Producer floods past the buffer | Producer receives backpressure, no silent loss | Expected to fail today, S3 |
| Restart the bus mid-flood | In-flight tasks survive | Expected to fail today, S3 |
| Subscriber disconnects and resumes | Resumes from its cursor, or is told the cursor is stale | Expected to fail today, S2 |
| Deploy with more than one worker process | Conformance fails the release | Passing |
| Slow consumer holds a long lease | Other members keep working; the task is not stolen early | Passing |
| Token rotated mid-run | Members fail closed with 401, not with corrupted state | Passing |
| Clock moves forward on the host | Leases expire early rather than never | Untested; add with S3 |

## 8. CI

`.github/workflows/swarm-tests.yml` runs on every push and pull request:

1. **Unit, schema, and integration** - the existing suite.
2. **Swarm concurrency** - `pytest tests/swarm`, including the gap ledger. An
   XPASS fails the build, which is the intended signal that a gap has been
   closed and its marker needs removing.
3. **Harness, embedded** - `swarm_harness.py all --embedded`, a self-contained
   end-to-end run needing no deployment.
4. **Conformance, embedded** - the suite against a locally started bus,
   confirming the deployment checks themselves still work.

Against a deployed environment, the same workflow runs conformance and a smoke
harness when the bus URL and token are available, and skips those steps cleanly
when they are not. The release process in `docs/OCI_HOSTING_PLAN.md` section 6
uses the identical commands, so a green pipeline and a verified deployment mean
the same thing.

Guidance on runtime: the swarm suite takes about 25 seconds and the embedded
harness a few seconds more, both fast enough to gate every pull request. The
capacity sweeps in section 6 are slower and belong on a schedule or a manual
trigger, not on every push.

## 9. Test requirements per swarm phase

What each phase of `docs/SWARM_DEV_PLAN.md` must add before it is considered
done. This is the definition of done from `docs/TESTING_STRATEGY.md`, made
specific.

| Phase | Required tests |
| --- | --- |
| S1 | Gap markers removed; a compatibility test proving `sender`-only members still work; schema validation for the type taxonomy; conformance reports both S1 capabilities |
| S2 | Gap markers removed; a test that a blocked read returns early when a message arrives, and on time when none does; stale-cursor resynchronisation; a harness measurement showing the drop in polls per observed message |
| S3 | Gap markers removed; restart-during-flood with zero loss; backpressure returns 429 rather than dropping; priority ordering under concurrency; dead-letter after N nacks; claim latency at 20,000 resident messages within 1.5x of the 500-message figure |
| S4 | Gap markers removed; a member killed mid-task has its work reassigned within one heartbeat interval; presence TTL expiry; directed routing to a handle |
| S5 | Gap markers removed; queue statistics correct under concurrent claim and ack; a game-day exercise diagnosing a stalled queue from metrics alone |
| S6 | Two processes serving one queue deliver every task exactly once; a revoked credential is rejected while other members continue; lock contention prevents stale writes; rate limits engage without dropping work |

## 10. Running everything

```bash
pip install -r requirements.txt -r requirements-dev.txt

# full local verification, roughly 30 seconds
python3 -m pytest -q
python3 scripts/swarm_harness.py all --embedded

# verify a deployment
python3 scripts/bus_conformance.py --bus-url "$BUS_URL" --token "$BUS_API_TOKEN" --slow
python3 scripts/swarm_harness.py all --bus-url "$BUS_URL" --token "$BUS_API_TOKEN"
```

Current expected result: 38 passed, 9 xfailed, harness clean, 16 conformance
checks passing with `--slow`, 7 capabilities absent.
