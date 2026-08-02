"""
Executable gap ledger for multi-agent swarming.

Every test here describes behaviour a swarm needs but the bus does not have
yet. They are marked `xfail(strict=True)`, so:

- while the gap exists the suite stays green and the gap stays documented;
- the moment someone implements the feature the test XPASSes, which is a
  failure, and whoever did the work removes the marker.

Each test names the phase in `docs/SWARM_DEV_PLAN.md` that is meant to close
it. Keep this file and that plan in sync.
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.swarm

S1 = "closed by SWARM_DEV_PLAN phase S1 (agent-addressable envelope)"
S2 = "closed by SWARM_DEV_PLAN phase S2 (blocking reads and stable cursors)"
S3 = "closed by SWARM_DEV_PLAN phase S3 (durable, prioritised work state)"
S4 = "closed by SWARM_DEV_PLAN phase S4 (membership and presence)"
S5 = "closed by SWARM_DEV_PLAN phase S5 (swarm observability)"


@pytest.mark.xfail(strict=True, reason=S1)
def test_agent_handle_survives_a_post(bus):
    """The README collaboration playbook posts `agent`, and the bus drops it.

    Every example in the "Agent Collaboration Guide" sends an `agent` handle.
    The server only reads `sender`, so each of those messages is stored with
    the literal sender "agent": in a swarm, every participant is anonymous and
    `agent.<agent-id>` routing cannot work.
    """
    client = bus.client()
    posted = client.post(
        message="Implement login validation",
        channel="task.login-flow",
        type="request",
        agent="agent:cursor.iac-bus.0@web",
    )
    assert posted.get("agent") == "agent:cursor.iac-bus.0@web"


@pytest.mark.xfail(strict=True, reason=S1)
def test_handoff_metadata_survives_a_post(bus):
    """`metadata` and `ref` are documented and silently discarded.

    The handoff pattern depends on `metadata.needs` and artifact lists to tell
    a sibling worker what is blocked. Today that context never reaches the
    reader.
    """
    client = bus.client()
    posted = client.post(
        message="Waiting for API contract update",
        channel="blockers",
        sender="agent-1",
        type="blocker",
        metadata={"needs": ["OpenAPI v3 response schema"], "task": "task.login-flow"},
        ref="task-login-flow",
    )
    assert posted.get("metadata", {}).get("needs") == ["OpenAPI v3 response schema"]
    assert posted.get("ref") == "task-login-flow"


@pytest.mark.xfail(strict=True, reason=S2)
def test_read_can_block_until_a_message_arrives(bus):
    """`wait_seconds` is accepted and ignored, so agents must busy-poll.

    A swarm of N agents polling every 500ms is N*2 requests per second of pure
    overhead, and it adds up to half a poll interval of latency to every
    handoff.
    """
    client = bus.client()
    started = time.perf_counter()
    client.poll(channel="swarm.quiet-channel", limit=10)
    # Not a real assertion of blocking yet: the call returns immediately today.
    elapsed = time.perf_counter() - started
    assert elapsed >= 1.0, (
        "GET /bus/messages returned immediately; server-side long poll is not implemented"
    )


@pytest.mark.xfail(strict=True, reason=S3)
def test_queued_work_is_not_evicted_by_chatter(bus_factory):
    """Retention drops pending work to make room for conversation.

    `_bus_prune` trims the single shared buffer to `BUS_MAX_MESSAGES` without
    regard for status, so status chatter from a busy swarm silently deletes
    tasks nobody has run yet. This is the highest-severity gap: work simply
    disappears, with no error and no dead-letter record.
    """
    bus = bus_factory(BUS_MAX_MESSAGES=50)
    client = bus.client()
    queue = "swarm.pressure"

    posted = [
        client.post(message={"task": i}, channel="work", sender="lead", queue=queue)["id"]
        for i in range(40)
    ]
    for i in range(200):
        client.post(message={"status": i}, channel="ops", sender="chatty-agent", type="progress")

    claimed = []
    while True:
        message = client.claim(queue, "worker", lease_seconds=30)
        if message is None:
            break
        claimed.append(message["id"])
        client.ack(queue, message["id"], "worker", message["lease_id"])

    assert sorted(claimed) == sorted(posted), (
        f"{len(posted) - len(claimed)} queued tasks were evicted before any worker saw them"
    )


@pytest.mark.xfail(strict=True, reason=S3)
def test_high_priority_work_is_claimed_first(bus):
    """`priority` is stored, validated, and then ignored by the scheduler.

    `_claim_queue_message` returns the oldest pending task regardless of
    priority, so an urgent step queued behind a backlog waits for the backlog.
    Orchestration already writes `step.priority` into assignments, which makes
    the gap invisible until latency matters.
    """
    client = bus.client()
    queue = "swarm.priority"
    client.post(message="low", channel="work", sender="lead", queue=queue, priority=0)
    client.post(message="high", channel="work", sender="lead", queue=queue, priority=99)

    first = client.claim(queue, "worker", lease_seconds=30)
    assert first["message"] == "high"


@pytest.mark.xfail(strict=True, reason=S2)
def test_an_unusable_cursor_is_reported_instead_of_replayed(bus):
    """A `since_id` the bus cannot place is ignored, and the whole window replays.

    The anchor lookup is a linear scan over the *filtered* view; on
    StopIteration the handler falls through and returns everything it has. So a
    cursor that has aged out, or that came from a view the agent is not asking
    for right now, silently turns an incremental read into a full replay, and
    the agent reprocesses every message it already handled.

    The swarm needs the stale cursor called out explicitly so an agent can
    resynchronise deliberately rather than duplicate work.
    """
    import requests

    client = bus.client()
    channel = "swarm.cursor"

    # A perfectly ordinary cursor for an agent that also consumes queue work:
    # the message exists, but it is not part of the broadcast view.
    cursor_message = client.post(
        message={"task": "assigned"}, channel=channel, sender="lead", queue="swarm.cursor.work"
    )
    already_seen = [
        client.post(message={"seq": i}, channel=channel, sender="lead")["id"] for i in range(5)
    ]

    resp = requests.get(
        f"{bus.url}/bus/messages",
        params={"channel": channel, "since_id": cursor_message["id"], "limit": 100},
        timeout=10,
    )
    body = resp.json()
    replayed = [m["id"] for m in body.get("messages", [])]

    assert resp.status_code == 409 or body.get("cursor_stale") is True, (
        f"the bus accepted an unusable cursor and replayed {len(replayed)} of "
        f"{len(already_seen)} already-seen messages instead of reporting it"
    )


@pytest.mark.xfail(strict=True, reason=S3)
def test_pending_work_survives_a_restart(bus_factory):
    """All coordination state lives in process memory.

    A restart, a crash, or a hot-reload on the dev VM discards every pending
    task, every lease, and every orchestration job. On OCI this means a
    `systemctl restart` silently drops the swarm's entire work queue.
    """
    bus = bus_factory()
    client = bus.client()
    queue = "swarm.durability"
    posted = client.post(message={"task": "survive"}, channel="work", sender="lead", queue=queue)

    restarted = bus_factory()
    recovered = restarted.client().claim(queue, "worker-after-restart", lease_seconds=30)
    assert recovered is not None and recovered["id"] == posted["id"]


@pytest.mark.xfail(strict=True, reason=S4)
def test_swarm_membership_is_queryable(bus):
    """There is no way to ask who is in the swarm.

    Without a registry, an orchestrator cannot size a fan-out to the available
    workers, detect a dead agent, or route to `agent.<id>`.
    """
    import requests

    resp = requests.get(f"{bus.url}/bus/agents", timeout=10)
    assert resp.status_code == 200
    assert isinstance(resp.json().get("agents"), list)


@pytest.mark.xfail(strict=True, reason=S5)
def test_queue_depth_is_observable(bus):
    """Backlog is invisible, so the swarm cannot be scaled on evidence.

    `/health` reports a single total message count that mixes chatter with
    work. Deciding whether to add workers needs per-queue pending, leased, and
    oldest-age figures.
    """
    import requests

    client = bus.client()
    client.post(message={"task": 1}, channel="work", sender="lead", queue="swarm.stats")

    resp = requests.get(f"{bus.url}/bus/queues/stats", timeout=10)
    assert resp.status_code == 200
    stats = resp.json()["queues"]["swarm.stats"]
    assert stats["pending"] == 1
