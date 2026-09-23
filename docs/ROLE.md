# IAC Bus: what it is for now (2026-09-23)

## History in one paragraph

On 2026-09-20 this repo was parked at M0: nothing consumed it, and the M1–M5
ladder rebuilt what Redis Streams, NATS or Temporal already do. On 2026-09-23
the founder asked for OSLO to spawn subagents and to test this bus with them.
Knowshowgo's plan already had the lane for it (`DEVELOPMENT-PLAN.md` §0, "iac-bus
coordination so one agent can direct others for coding", track **B**). So the
park is lifted, with a narrower job than the old ladder.

## The job

**The cross-host ICBus transport for OSLO agent swarms.** OSLO's components
speak ICBus (`subscribe / publish / request`); OSLO's `IacBusTransport` carries
each ICMessage as one message on a channel here (default `oslo.icbus`). A
parent agent and its forked subagents meet on that channel. Rungs and acceptance
tests: OSLO `docs/SWARM-LADDER.md` (SW0–SW9).

Not its job: the chat turn substrate (rejected in KSG `DEVELOPMENT-PLAN.md`
§3f), durable storage of record (KSG is), or a general team chat.

## What OSLO depends on (keep these true)

| Contract | Why | Test |
|---|---|---|
| `GET /bus/messages?channel=&since_id=&limit=` returns the **oldest** `limit` messages after the cursor, plus `has_more` | forward paging; the old newest-page behaviour dropped the head of any burst larger than one page (measured: 200 of 450 arrived) | `tests/test_bus.py::test_since_id_pages_forward_without_loss`; OSLO `tests/integration/iac_bus_live.integration.test.mjs` |
| An unknown `since_id` returns `cursor_lost: true` and the oldest retained page | a pruned cursor must be visible, not a silent full replay | `test_pruned_cursor_is_reported_not_silently_replayed` |
| Without `since_id`, the newest `limit` messages | OSLO reads the tail once on open | `test_no_cursor_still_returns_latest` |
| `message` may be any JSON value, stored as posted | ICMessage envelopes are objects | existing post tests |
| Bearer auth on every route but `/health` | the token is the only access control | `test_post_requires_auth` |

## Rungs that come back, only when a swarm rung needs them

| From the old ladder | Needed by | Why |
|---|---|---|
| Queue `claim/ack/nack` (exists) | SW4 | pull-based work with lease expiry |
| TLS behind Caddy + DNS `iac-bus.knowshowgo.com` | SW8 | workers on other hosts send a bearer token |
| Presence / heartbeat registry (old L2) | SW8 | know which remote workers are alive |
| Durable ledger (old L5, SQLite first) | SW8 / SW9 | today a bus restart loses in-flight messages; retention is 500 msgs / 1 h |
| Long-poll `wait_seconds` (old L1) | if SW2/SW8 latency hurts | ~100 ms poll today |

## Ideas kept from the parked design

1. **Barrier sync is the litmus.** Fan-out is trivial; reconverging is the test.
   OSLO's SW1 swarm test and `scripts/dogfood_litmus.sh` both assert it.
2. **The oversight envelope** (`request_decision` with `choices[]`,
   `requires_approval`, `escalation_chain`) is the shape SW6's integrator will
   use to ask the founder before opening a PR.

## Maintenance

```bash
./venv/bin/pytest -q
BUS_PORT=8101 BUS_API_TOKEN=devtoken bash ./scripts/bus_smoke.sh
```
