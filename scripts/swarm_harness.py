#!/usr/bin/env python3
"""
Swarm harness: drive a real IAC Bus with many concurrent agents and check that
coordination invariants hold.

The harness is the executable half of `docs/SWARM_TESTING_PLAN.md`. It runs
against any bus URL, so the same command validates a laptop, a CI job, and the
OCI-hosted service.

Scenarios
---------
flood   N producers fill a work queue, W workers claim/ack it. Checks that
        every task is delivered exactly once and none are lost.
fanout  One publisher broadcasts on a channel while S subscribers follow it
        with `since_id` cursors. Checks each subscriber sees every message
        once, in order, under retention pressure.
dag     An orchestration job with a fan-out/barrier/fan-in shape is executed by
        W workers. Checks the job completes and dependency order is respected.

Examples
--------
    # Self-contained run (starts its own bus in-process, no deployment needed)
    python3 scripts/swarm_harness.py all --embedded

    # Against the OCI-hosted bus
    python3 scripts/swarm_harness.py flood \
        --bus-url http://<VM_IP>:8091 --token "$BUS_API_TOKEN" \
        --workers 16 --tasks 500

Exit code is non-zero when an invariant is violated, so the harness can gate a
deployment. Pass `--no-fail` to report without failing.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import string
import sys
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from bus_client import BusClient, BusError  # noqa: E402


# ----------------------------------------------------------------------
# result plumbing
# ----------------------------------------------------------------------
@dataclass
class Violation:
    code: str
    detail: str
    severity: str = "critical"

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "detail": self.detail, "severity": self.severity}


@dataclass
class ScenarioResult:
    scenario: str
    metrics: Dict[str, Any] = field(default_factory=dict)
    violations: List[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(v.severity == "critical" for v in self.violations)

    def add(self, code: str, detail: str, severity: str = "critical") -> None:
        self.violations.append(Violation(code, detail, severity))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenario": self.scenario,
            "ok": self.ok,
            "metrics": self.metrics,
            "violations": [v.to_dict() for v in self.violations],
        }


class Latency:
    """Collects timings from many threads and summarises them."""

    def __init__(self) -> None:
        self._samples: List[float] = []
        self._lock = threading.Lock()

    def record(self, seconds: float) -> None:
        with self._lock:
            self._samples.append(seconds * 1000.0)

    def summary(self) -> Dict[str, float]:
        with self._lock:
            samples = sorted(self._samples)
        if not samples:
            return {}
        return {
            "count": len(samples),
            "mean_ms": round(statistics.fmean(samples), 2),
            "p50_ms": round(_percentile(samples, 0.50), 2),
            "p95_ms": round(_percentile(samples, 0.95), 2),
            "p99_ms": round(_percentile(samples, 0.99), 2),
            "max_ms": round(samples[-1], 2),
        }


def _percentile(sorted_samples: List[float], fraction: float) -> float:
    if not sorted_samples:
        return 0.0
    index = min(len(sorted_samples) - 1, int(round(fraction * (len(sorted_samples) - 1))))
    return sorted_samples[index]


def _timed(latency: Latency, fn: Callable[[], Any]) -> Any:
    start = time.perf_counter()
    try:
        return fn()
    finally:
        latency.record(time.perf_counter() - start)


def _run_id() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


# ----------------------------------------------------------------------
# embedded bus (so the harness is runnable without a deployment)
# ----------------------------------------------------------------------
def start_embedded_bus(env: Optional[Dict[str, str]] = None):
    """Start the bus in this process on an ephemeral port.

    Returns (base_url, shutdown_callable).
    """
    import importlib

    from werkzeug.serving import make_server

    for key, value in (env or {}).items():
        os.environ[key] = str(value)

    if "server" in sys.modules:
        bus_server = importlib.reload(sys.modules["server"])
    else:
        import server as bus_server  # noqa: F401

    http = make_server("127.0.0.1", 0, bus_server.app, threaded=True)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{http.server_port}"

    def shutdown() -> None:
        http.shutdown()
        thread.join(timeout=5)

    return base_url, shutdown


# ----------------------------------------------------------------------
# scenario: flood
# ----------------------------------------------------------------------
def scenario_flood(args, make_client: Callable[[], BusClient]) -> ScenarioResult:
    """W workers drain a queue of N tasks. Nothing may be lost or duplicated."""
    result = ScenarioResult("flood")
    queue = f"swarm.flood.{_run_id()}"
    producer = make_client()

    post_latency = Latency()
    claim_latency = Latency()
    ack_latency = Latency()

    posted: Dict[str, int] = {}
    for index in range(args.tasks):
        message = _timed(
            post_latency,
            lambda i=index: producer.post(
                message={"task": i, "payload": "x" * args.payload_bytes},
                channel="swarm.work",
                sender="swarm-producer",
                type="request",
                queue=queue,
            ),
        )
        posted[message["id"]] = index

    claims: Counter = Counter()
    claims_lock = threading.Lock()
    acked: set = set()
    worker_totals: Counter = Counter()
    errors: List[str] = []
    deadline = time.time() + args.timeout

    def worker(name: str) -> None:
        client = make_client()
        empty_polls = 0
        while time.time() < deadline and empty_polls < args.drain_empty_polls:
            try:
                message = _timed(
                    claim_latency,
                    lambda: client.claim(queue, name, lease_seconds=args.lease_seconds),
                )
            except BusError as exc:
                errors.append(f"claim failed for {name}: {exc}")
                break
            if message is None:
                empty_polls += 1
                time.sleep(args.idle_sleep)
                continue
            empty_polls = 0
            message_id = message["id"]
            with claims_lock:
                claims[message_id] += 1
                already_done = message_id in acked
            if already_done:
                result.add(
                    "duplicate_delivery",
                    f"task {message_id} re-delivered to {name} after being acked",
                )
            if args.work_ms:
                time.sleep(args.work_ms / 1000.0)
            try:
                _timed(
                    ack_latency,
                    lambda: client.ack(queue, message_id, name, message["lease_id"]),
                )
            except BusError as exc:
                errors.append(f"ack failed for {message_id}: {exc}")
                continue
            with claims_lock:
                acked.add(message_id)
                worker_totals[name] += 1

    started = time.perf_counter()
    threads = [
        threading.Thread(target=worker, args=(f"swarm-worker-{i}",), daemon=True)
        for i in range(args.workers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=max(1.0, deadline - time.time() + 5))
    elapsed = time.perf_counter() - started

    lost = sorted(set(posted) - acked)
    duplicated = {mid: count for mid, count in claims.items() if count > 1}

    if lost:
        result.add(
            "task_loss",
            f"{len(lost)}/{len(posted)} tasks were posted but never delivered to a worker "
            f"(first lost id {lost[0]}); with a full buffer the bus evicts pending queue "
            f"tasks silently",
        )
    if duplicated:
        result.add(
            "duplicate_delivery",
            f"{len(duplicated)} task(s) were claimed more than once: "
            f"{list(duplicated.items())[:5]}",
        )
    for error in errors[:5]:
        result.add("request_error", error)

    distribution = sorted(worker_totals.values())
    result.metrics = {
        "queue": queue,
        "tasks_posted": len(posted),
        "tasks_completed": len(acked),
        "tasks_lost": len(lost),
        "tasks_duplicated": len(duplicated),
        "workers": args.workers,
        "elapsed_seconds": round(elapsed, 3),
        "throughput_tasks_per_second": round(len(acked) / elapsed, 1) if elapsed else 0,
        "per_worker_min": distribution[0] if distribution else 0,
        "per_worker_max": distribution[-1] if distribution else 0,
        "post_latency": post_latency.summary(),
        "claim_latency": claim_latency.summary(),
        "ack_latency": ack_latency.summary(),
    }
    return result


# ----------------------------------------------------------------------
# scenario: fanout
# ----------------------------------------------------------------------
def scenario_fanout(args, make_client: Callable[[], BusClient]) -> ScenarioResult:
    """Subscribers follow a channel with `since_id`. No gaps, no repeats."""
    result = ScenarioResult("fanout")
    channel = f"swarm.fanout.{_run_id()}"
    publisher = make_client()

    anchor = publisher.post(
        message="swarm-fanout-anchor",
        channel=channel,
        sender="swarm-publisher",
        type="info",
    )

    observed: Dict[str, List[str]] = defaultdict(list)
    observed_lock = threading.Lock()
    stop = threading.Event()
    poll_latency = Latency()

    def subscriber(name: str) -> None:
        client = make_client()
        cursor = anchor["id"]
        while not stop.is_set():
            try:
                messages = _timed(
                    poll_latency,
                    lambda: client.poll(channel=channel, since_id=cursor, limit=args.poll_limit),
                )
            except BusError:
                time.sleep(args.idle_sleep)
                continue
            if messages:
                with observed_lock:
                    observed[name].extend(m["id"] for m in messages)
                cursor = messages[-1]["id"]
            else:
                time.sleep(args.idle_sleep)

    threads = [
        threading.Thread(target=subscriber, args=(f"swarm-subscriber-{i}",), daemon=True)
        for i in range(args.subscribers)
    ]
    for thread in threads:
        thread.start()

    published: List[str] = []
    for index in range(args.messages):
        message = publisher.post(
            message={"seq": index},
            channel=channel,
            sender="swarm-publisher",
            type="progress",
        )
        published.append(message["id"])
        # Background traffic from the rest of the swarm, which competes for the
        # shared retention buffer.
        for _ in range(args.pressure):
            publisher.post(
                message={"noise": index},
                channel="swarm.pressure",
                sender="swarm-noise",
                type="info",
            )
        if args.publish_interval_ms:
            time.sleep(args.publish_interval_ms / 1000.0)

    time.sleep(args.settle_seconds)
    stop.set()
    for thread in threads:
        thread.join(timeout=5)

    published_set = set(published)
    for name, seen in observed.items():
        relevant = [mid for mid in seen if mid in published_set]
        missed = published_set - set(relevant)
        repeats = {mid: count for mid, count in Counter(relevant).items() if count > 1}
        if missed:
            result.add(
                "missed_messages",
                f"{name} missed {len(missed)}/{len(published)} broadcast messages",
            )
        if repeats:
            result.add(
                "duplicate_observation",
                f"{name} received {len(repeats)} message(s) more than once; a cursor that "
                f"falls out of the retention window makes the bus replay the whole view",
            )
        ordering = [published.index(mid) for mid in relevant]
        if ordering != sorted(ordering):
            result.add("out_of_order", f"{name} observed messages out of publication order")

    result.metrics = {
        "channel": channel,
        "subscribers": args.subscribers,
        "messages_published": len(published),
        "pressure_messages_per_publish": args.pressure,
        "observed_per_subscriber": {k: len(v) for k, v in observed.items()},
        "poll_latency": poll_latency.summary(),
    }
    return result


# ----------------------------------------------------------------------
# scenario: dag
# ----------------------------------------------------------------------
def build_fanout_job(job_id: str, width: int, queue: str) -> Dict[str, Any]:
    """plan -> {impl-0..impl-N} -> barrier -> integrate -> verify."""
    steps: List[Dict[str, Any]] = [{"id": "plan", "queue": queue, "priority": 10}]
    impl_ids = []
    for index in range(width):
        step_id = f"impl-{index}"
        impl_ids.append(step_id)
        steps.append(
            {
                "id": step_id,
                "queue": queue,
                "depends_on": [{"step_id": "plan"}],
                "parallel_group": "impl",
            }
        )
    steps.append(
        {
            "id": "integrate",
            "queue": queue,
            "depends_on": [{"step_id": sid} for sid in impl_ids],
            "wait_for": ["impl-barrier"],
        }
    )
    steps.append(
        {"id": "verify", "queue": queue, "depends_on": [{"step_id": "integrate"}]}
    )
    return {
        "job_id": job_id,
        "name": "swarm-harness-fanout",
        "steps": steps,
        "barriers": [{"id": "impl-barrier", "requires": impl_ids, "mode": "all_completed"}],
    }


def scenario_dag(args, make_client: Callable[[], BusClient]) -> ScenarioResult:
    """A swarm cooperatively executes a fan-out/barrier/fan-in job."""
    result = ScenarioResult("dag")
    run = _run_id()
    job_id = f"swarm-job-{run}"
    queue = f"swarm.dag.{run}"
    orchestrator = make_client()

    job = build_fanout_job(job_id, args.width, queue)
    orchestrator.submit_job(job, sender="swarm-orchestrator")

    timeline: List[Dict[str, Any]] = []
    timeline_lock = threading.Lock()
    step_claims: Counter = Counter()
    stop = threading.Event()
    deadline = time.time() + args.timeout
    errors: List[str] = []

    def worker(name: str) -> None:
        client = make_client()
        while not stop.is_set() and time.time() < deadline:
            try:
                assignment = client.claim(queue, name, lease_seconds=args.lease_seconds)
            except BusError as exc:
                errors.append(f"claim failed for {name}: {exc}")
                return
            if assignment is None:
                time.sleep(args.idle_sleep)
                continue
            body = assignment["message"]
            step_id = body["step"]["id"]
            with timeline_lock:
                step_claims[step_id] += 1
                timeline.append({"step": step_id, "event": "start", "at": time.time(), "worker": name})
            try:
                client.report_step(body["job_id"], step_id, "running", sender=name)
                if args.work_ms:
                    time.sleep(args.work_ms / 1000.0)
                client.report_step(body["job_id"], step_id, "completed", sender=name)
                # The bus releases dependents the moment it accepts this report,
                # so "end" must be stamped here rather than after the ack.
                with timeline_lock:
                    timeline.append(
                        {"step": step_id, "event": "end", "at": time.time(), "worker": name}
                    )
                client.ack(queue, assignment["id"], name, assignment["lease_id"])
            except BusError as exc:
                errors.append(f"step {step_id} failed for {name}: {exc}")
                continue

    started = time.perf_counter()
    threads = [
        threading.Thread(target=worker, args=(f"swarm-dag-worker-{i}",), daemon=True)
        for i in range(args.workers)
    ]
    for thread in threads:
        thread.start()

    final_state: Dict[str, Any] = {}
    while time.time() < deadline:
        state = orchestrator.get_job(job_id)["state"]
        if all(s.get("status") in {"completed", "failed", "skipped", "timed_out"} for s in state.values()):
            final_state = state
            break
        time.sleep(0.2)
    else:
        final_state = orchestrator.get_job(job_id)["state"]

    stop.set()
    for thread in threads:
        thread.join(timeout=5)
    elapsed = time.perf_counter() - started

    incomplete = {sid: s.get("status") for sid, s in final_state.items() if s.get("status") != "completed"}
    if incomplete:
        result.add("job_incomplete", f"steps did not complete: {incomplete}")

    reruns = {sid: count for sid, count in step_claims.items() if count > 1}
    if reruns:
        result.add("step_duplicate_execution", f"steps executed more than once: {reruns}")

    ends = {entry["step"]: entry["at"] for entry in timeline if entry["event"] == "end"}
    starts = {entry["step"]: entry["at"] for entry in timeline if entry["event"] == "start"}
    impl_ends = [ends[f"impl-{i}"] for i in range(args.width) if f"impl-{i}" in ends]
    if "integrate" in starts and impl_ends:
        if starts["integrate"] < max(impl_ends):
            result.add(
                "barrier_violation",
                "integrate started before every impl step completed",
            )
    if "verify" in starts and "integrate" in ends:
        if starts["verify"] < ends["integrate"]:
            result.add("dependency_violation", "verify started before integrate completed")
    if "plan" in ends and impl_ends:
        if min(starts[f"impl-{i}"] for i in range(args.width) if f"impl-{i}" in starts) < ends["plan"]:
            result.add("dependency_violation", "an impl step started before plan completed")

    for error in errors[:5]:
        result.add("request_error", error)

    concurrency = _max_overlap(
        [(starts[f"impl-{i}"], ends[f"impl-{i}"]) for i in range(args.width) if f"impl-{i}" in ends]
    )
    result.metrics = {
        "job_id": job_id,
        "queue": queue,
        "steps": len(final_state),
        "fanout_width": args.width,
        "workers": args.workers,
        "completed_steps": sum(1 for s in final_state.values() if s.get("status") == "completed"),
        "observed_parallelism": concurrency,
        "elapsed_seconds": round(elapsed, 3),
    }
    return result


def _max_overlap(intervals: List[tuple]) -> int:
    """Largest number of intervals active at once (observed parallelism)."""
    events: List[tuple] = []
    for start, end in intervals:
        events.append((start, 1))
        events.append((end, -1))
    events.sort()
    current = best = 0
    for _, delta in events:
        current += delta
        best = max(best, current)
    return best


SCENARIOS: Dict[str, Callable] = {
    "flood": scenario_flood,
    "fanout": scenario_fanout,
    "dag": scenario_dag,
}


# ----------------------------------------------------------------------
# entrypoint
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Drive the IAC Bus with a swarm of agents and verify coordination invariants.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "scenario",
        choices=sorted(SCENARIOS) + ["all"],
        help="which swarm scenario to run",
    )
    parser.add_argument("--bus-url", default=os.environ.get("BUS_URL", "http://127.0.0.1:8091"))
    parser.add_argument("--token", default=os.environ.get("BUS_API_TOKEN", ""))
    parser.add_argument(
        "--embedded",
        action="store_true",
        help="start a throwaway bus in this process instead of using --bus-url",
    )
    parser.add_argument(
        "--embedded-max-messages",
        type=int,
        default=5000,
        help="BUS_MAX_MESSAGES for the embedded bus",
    )
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--tasks", type=int, default=200, help="flood: tasks to enqueue")
    parser.add_argument("--payload-bytes", type=int, default=64, help="flood: task payload size")
    parser.add_argument("--subscribers", type=int, default=4, help="fanout: concurrent readers")
    parser.add_argument("--messages", type=int, default=50, help="fanout: messages to broadcast")
    parser.add_argument(
        "--pressure",
        type=int,
        default=0,
        help="fanout: background messages published per broadcast (retention pressure)",
    )
    parser.add_argument("--publish-interval-ms", type=float, default=0.0)
    parser.add_argument("--poll-limit", type=int, default=50)
    parser.add_argument("--settle-seconds", type=float, default=1.5)
    parser.add_argument("--width", type=int, default=6, help="dag: parallel step count")
    parser.add_argument("--work-ms", type=float, default=0.0, help="simulated work per task")
    parser.add_argument("--lease-seconds", type=float, default=30.0)
    parser.add_argument("--idle-sleep", type=float, default=0.05)
    parser.add_argument("--drain-empty-polls", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=120.0, help="per-scenario budget")
    parser.add_argument("--json", dest="json_path", default="", help="write the report to a file")
    parser.add_argument(
        "--no-fail",
        action="store_true",
        help="always exit 0, even when invariants are violated",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    shutdown = None
    if args.embedded:
        args.bus_url, shutdown = start_embedded_bus(
            {
                "BUS_API_TOKEN": args.token,
                "BUS_MAX_MESSAGES": str(args.embedded_max_messages),
                "BUS_RETENTION_SECONDS": "3600",
                "BUS_LOG_LEVEL": "WARNING",
            }
        )

    def make_client() -> BusClient:
        return BusClient(args.bus_url, token=args.token)

    try:
        probe = make_client()
        health = probe.health()
    except Exception as exc:  # noqa: BLE001 - surface any connection problem plainly
        print(f"Cannot reach bus at {args.bus_url}: {exc}", file=sys.stderr)
        if shutdown:
            shutdown()
        return 2

    names = sorted(SCENARIOS) if args.scenario == "all" else [args.scenario]
    results = []
    try:
        for name in names:
            print(f"[swarm-harness] running {name} against {args.bus_url}", file=sys.stderr)
            results.append(SCENARIOS[name](args, make_client))
    finally:
        if shutdown:
            shutdown()

    report = {
        "bus_url": args.bus_url,
        "health": health,
        "generated_at": time.time(),
        "ok": all(result.ok for result in results),
        "scenarios": [result.to_dict() for result in results],
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            handle.write(rendered + "\n")

    if report["ok"] or args.no_fail:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
