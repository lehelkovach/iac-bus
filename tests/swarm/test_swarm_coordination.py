"""
Coordination guarantees the bus provides today, verified with a real swarm of
concurrent workers over HTTP.

These are the properties a swarm may rely on right now. If one of them breaks,
agents will silently do duplicate or lost work, so they are release blockers
rather than nice-to-haves. See `docs/SWARM_TESTING_PLAN.md` (layer L4).
"""

from __future__ import annotations

import threading
import time
from collections import Counter

import pytest

pytestmark = pytest.mark.swarm


def _drain_queue(bus, queue, workers, lease_seconds=30.0, timeout=60.0):
    """Run `workers` concurrent claimers until the queue is empty.

    Returns (claims_per_message_id, completed_ids, errors).
    """
    claims: Counter = Counter()
    completed: set = set()
    errors: list = []
    lock = threading.Lock()
    deadline = time.time() + timeout

    def worker(name: str) -> None:
        client = bus.client()
        empty_polls = 0
        while empty_polls < 3 and time.time() < deadline:
            try:
                message = client.claim(queue, name, lease_seconds=lease_seconds)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(f"{name}: claim failed: {exc}")
                return
            if message is None:
                empty_polls += 1
                time.sleep(0.02)
                continue
            empty_polls = 0
            with lock:
                claims[message["id"]] += 1
            try:
                client.ack(queue, message["id"], name, message["lease_id"])
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(f"{name}: ack failed: {exc}")
                continue
            with lock:
                completed.add(message["id"])

    threads = [
        threading.Thread(target=worker, args=(f"worker-{i}",), daemon=True)
        for i in range(workers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout)
    return claims, completed, errors


def test_every_task_is_delivered_to_exactly_one_worker(bus):
    """The core swarm guarantee: no task is dropped and none is done twice."""
    producer = bus.client()
    queue = "swarm.exactly-once"
    task_count = 300
    posted = {
        producer.post(message={"task": i}, channel="work", sender="lead", queue=queue)["id"]
        for i in range(task_count)
    }

    claims, completed, errors = _drain_queue(bus, queue, workers=12)

    assert errors == []
    assert completed == posted, f"{len(posted - completed)} task(s) were never completed"
    duplicated = {mid: count for mid, count in claims.items() if count > 1}
    assert duplicated == {}, f"tasks handed to more than one worker: {duplicated}"


def test_work_is_spread_across_the_swarm(bus):
    """Every worker gets work; a queue that only ever feeds one agent is useless."""
    producer = bus.client()
    queue = "swarm.distribution"
    for i in range(200):
        producer.post(message={"task": i}, channel="work", sender="lead", queue=queue)

    workers = 8
    assignments: Counter = Counter()
    lock = threading.Lock()

    def worker(name: str) -> None:
        client = bus.client()
        empty_polls = 0
        while empty_polls < 3:
            message = client.claim(queue, name, lease_seconds=30)
            if message is None:
                empty_polls += 1
                time.sleep(0.02)
                continue
            empty_polls = 0
            # Simulated work, so no single worker can monopolise the queue.
            time.sleep(0.005)
            client.ack(queue, message["id"], name, message["lease_id"])
            with lock:
                assignments[name] += 1

    threads = [
        threading.Thread(target=worker, args=(f"worker-{i}",), daemon=True)
        for i in range(workers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert sum(assignments.values()) == 200
    assert len(assignments) == workers, f"idle workers: {workers - len(assignments)}"


def test_a_leased_task_is_not_offered_to_anyone_else(bus):
    """While one agent holds a lease, the task is invisible to the rest."""
    client = bus.client()
    queue = "swarm.lease-exclusive"
    client.post(message={"task": "only-one"}, channel="work", sender="lead", queue=queue)

    holder = client.claim(queue, "holder", lease_seconds=30)
    assert holder is not None

    results = []
    lock = threading.Lock()

    def contender(name: str) -> None:
        message = bus.client().claim(queue, name, lease_seconds=30)
        with lock:
            results.append(message)

    threads = [
        threading.Thread(target=contender, args=(f"contender-{i}",), daemon=True)
        for i in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert all(result is None for result in results), "a leased task was handed out twice"


def test_abandoned_work_is_recovered_by_another_agent(bus):
    """An agent that dies mid-task must not strand the work."""
    client = bus.client()
    queue = "swarm.takeover"
    posted = client.post(message={"task": "recover-me"}, channel="work", sender="lead", queue=queue)

    abandoned = client.claim(queue, "agent-that-dies", lease_seconds=1)
    assert abandoned is not None
    assert client.claim(queue, "too-early") is None, "lease was not honoured"

    time.sleep(1.5)

    recovered = client.claim(queue, "agent-that-recovers", lease_seconds=30)
    assert recovered is not None, "abandoned task was never re-offered"
    assert recovered["id"] == posted["id"]
    assert recovered["lease_id"] != abandoned["lease_id"], (
        "lease id was reused after takeover, so the bus cannot fence off the dead agent"
    )


def test_the_original_agent_cannot_ack_after_losing_its_lease(bus):
    """Fencing: a resurrected agent must not complete work that moved on."""
    client = bus.client()
    queue = "swarm.fencing"
    client.post(message={"task": "fenced"}, channel="work", sender="lead", queue=queue)

    stale = client.claim(queue, "agent-a", lease_seconds=1)
    time.sleep(1.5)
    fresh = client.claim(queue, "agent-b", lease_seconds=30)
    assert fresh is not None

    from bus_client import BusError

    with pytest.raises(BusError) as excinfo:
        client.ack(queue, stale["id"], "agent-a", stale["lease_id"])
    assert excinfo.value.status_code == 409


def test_nacked_work_returns_to_the_queue_intact(bus):
    """A worker that hits a blocker can hand the task back without losing it."""
    client = bus.client()
    queue = "swarm.nack"
    payload = {"task": "retry-me", "attempt": 1}
    posted = client.post(message=payload, channel="work", sender="lead", queue=queue)

    claimed = client.claim(queue, "agent-a", lease_seconds=30)
    client.nack(queue, claimed["id"], "agent-a", claimed["lease_id"], requeue=True)

    retried = client.claim(queue, "agent-b", lease_seconds=30)
    assert retried is not None, "nacked task was not requeued"
    assert retried["id"] == posted["id"]
    assert retried["message"] == payload, "payload was mutated on requeue"


def test_swarm_executes_a_dependency_graph_in_order(bus):
    """Fan-out, barrier, then fan-in: the shape most swarm jobs take."""
    orchestrator = bus.client()
    job_id = "swarm-dag"
    queue = "swarm.dag"
    width = 5
    impl_ids = [f"impl-{i}" for i in range(width)]

    orchestrator.submit_job(
        {
            "job_id": job_id,
            "name": "swarm-dag",
            "steps": [
                {"id": "plan", "queue": queue},
                *[
                    {"id": sid, "queue": queue, "depends_on": [{"step_id": "plan"}]}
                    for sid in impl_ids
                ],
                {
                    "id": "integrate",
                    "queue": queue,
                    "depends_on": [{"step_id": sid} for sid in impl_ids],
                    "wait_for": ["impl-barrier"],
                },
            ],
            "barriers": [
                {"id": "impl-barrier", "requires": impl_ids, "mode": "all_completed"}
            ],
        }
    )

    events = []
    lock = threading.Lock()
    stop = threading.Event()
    deadline = time.time() + 60

    def worker(name: str) -> None:
        client = bus.client()
        while not stop.is_set() and time.time() < deadline:
            assignment = client.claim(queue, name, lease_seconds=30)
            if assignment is None:
                time.sleep(0.02)
                continue
            step_id = assignment["message"]["step"]["id"]
            with lock:
                events.append(("start", step_id, time.time()))
            client.report_step(job_id, step_id, "running", sender=name)
            time.sleep(0.02)
            client.report_step(job_id, step_id, "completed", sender=name)
            with lock:
                events.append(("end", step_id, time.time()))
            client.ack(queue, assignment["id"], name, assignment["lease_id"])

    threads = [
        threading.Thread(target=worker, args=(f"dag-worker-{i}",), daemon=True)
        for i in range(4)
    ]
    for thread in threads:
        thread.start()

    while time.time() < deadline:
        state = orchestrator.get_job(job_id)["state"]
        if all(step.get("status") == "completed" for step in state.values()):
            break
        time.sleep(0.05)
    stop.set()
    for thread in threads:
        thread.join(timeout=10)

    state = orchestrator.get_job(job_id)["state"]
    assert all(step.get("status") == "completed" for step in state.values()), state

    starts = {step: at for kind, step, at in events if kind == "start"}
    ends = {step: at for kind, step, at in events if kind == "end"}
    executions = Counter(step for kind, step, _ in events if kind == "start")

    assert executions["integrate"] == 1, "the fan-in step ran more than once"
    for sid in impl_ids:
        assert starts[sid] >= ends["plan"], f"{sid} started before plan finished"
    assert starts["integrate"] >= max(ends[sid] for sid in impl_ids), (
        "integrate started before the barrier opened"
    )


def test_broadcast_readers_each_see_every_message_once(bus):
    """Channel fan-out: N observers following a cursor must not miss or repeat."""
    publisher = bus.client()
    channel = "swarm.broadcast"
    anchor = publisher.post(message="anchor", channel=channel, sender="lead")

    observed: dict = {}
    lock = threading.Lock()
    stop = threading.Event()

    def subscriber(name: str) -> None:
        client = bus.client()
        cursor = anchor["id"]
        seen = []
        while not stop.is_set():
            messages = client.poll(channel=channel, since_id=cursor, limit=100)
            if messages:
                seen.extend(m["id"] for m in messages)
                cursor = messages[-1]["id"]
            else:
                time.sleep(0.02)
        with lock:
            observed[name] = seen

    threads = [
        threading.Thread(target=subscriber, args=(f"subscriber-{i}",), daemon=True)
        for i in range(5)
    ]
    for thread in threads:
        thread.start()

    published = [
        publisher.post(message={"seq": i}, channel=channel, sender="lead", type="progress")["id"]
        for i in range(60)
    ]

    time.sleep(1.0)
    stop.set()
    for thread in threads:
        thread.join(timeout=10)

    for name, seen in observed.items():
        assert seen == published, (
            f"{name} did not observe the broadcast stream exactly once in order "
            f"(saw {len(seen)} of {len(published)})"
        )
