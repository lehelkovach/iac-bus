#!/usr/bin/env python3
"""
Conformance and smoke suite for a deployed IAC Bus.

Run this against any bus URL immediately after a deploy. It answers two
questions:

1. Does the deployment honour the protocol contract? (required checks; a
   failure here should block or roll back the release)
2. Which optional coordination capabilities does this build have? (capability
   probes; reported, never failed, so an older bus does not fail the gate)

Usage
-----
    python3 scripts/bus_conformance.py --bus-url http://<VM_IP>:8091 \
        --token "$BUS_API_TOKEN"

    # include the slow lease-expiry check
    python3 scripts/bus_conformance.py --bus-url ... --token ... --slow

All test traffic uses run-scoped channel and queue names so it is safe to point
at a live bus, though the messages do consume the shared retention buffer.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import string
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import requests

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from bus_client import BusClient  # noqa: E402


@dataclass
class CheckReport:
    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class Suite:
    checks: List[CheckReport] = field(default_factory=list)
    capabilities: Dict[str, Any] = field(default_factory=dict)

    def check(self, name: str, fn: Callable[[], Optional[str]]) -> None:
        """Run a required check. `fn` returns None on success or a failure reason."""
        try:
            reason = fn()
        except Exception as exc:  # noqa: BLE001 - any exception is a failed check
            reason = f"{type(exc).__name__}: {exc}"
        self.checks.append(CheckReport(name, reason is None, reason or ""))
        status = "PASS" if reason is None else "FAIL"
        print(f"  [{status}] {name}" + (f" - {reason}" if reason else ""), file=sys.stderr)

    def capability(self, name: str, fn: Callable[[], bool], note: str = "") -> None:
        """Probe an optional capability. Never fails the suite."""
        try:
            present = bool(fn())
        except Exception:  # noqa: BLE001
            present = False
        self.capabilities[name] = {"present": present, "note": note}
        print(f"  [{'yes' if present else 'no '}] capability: {name}", file=sys.stderr)

    @property
    def ok(self) -> bool:
        return all(check.passed for check in self.checks)


def _run_id() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


def run_suite(args) -> Suite:
    suite = Suite()
    run = _run_id()
    client = BusClient(args.bus_url, token=args.token, timeout=args.timeout)
    channel = f"conformance.{run}"
    queue = f"conformance.q.{run}"
    worker = f"conformance-worker-{run}"

    print("Required checks:", file=sys.stderr)

    def check_health() -> Optional[str]:
        body = client.health()
        if body.get("status") != "ok":
            return f"unexpected health body: {body}"
        return None

    suite.check("health endpoint returns ok", check_health)

    def check_health_unauthenticated() -> Optional[str]:
        resp = requests.get(f"{args.bus_url.rstrip('/')}/health", timeout=args.timeout)
        if resp.status_code != 200:
            return f"health should be reachable without a token, got {resp.status_code}"
        return None

    suite.check("health is exempt from auth", check_health_unauthenticated)

    if args.token:

        def check_rejects_missing_token() -> Optional[str]:
            resp = requests.post(
                f"{args.bus_url.rstrip('/')}/bus/messages",
                json={"message": "unauthorized probe"},
                timeout=args.timeout,
            )
            if resp.status_code != 401:
                return f"expected 401 without a token, got {resp.status_code}"
            return None

        def check_rejects_wrong_token() -> Optional[str]:
            resp = requests.post(
                f"{args.bus_url.rstrip('/')}/bus/messages",
                json={"message": "unauthorized probe"},
                headers={"Authorization": "Bearer definitely-not-the-token"},
                timeout=args.timeout,
            )
            if resp.status_code != 401:
                return f"expected 401 with a bad token, got {resp.status_code}"
            return None

        suite.check("rejects unauthenticated posts", check_rejects_missing_token)
        suite.check("rejects posts with a wrong token", check_rejects_wrong_token)
    else:
        print(
            "  [SKIP] auth checks - no --token given; a public bus is a deployment risk",
            file=sys.stderr,
        )

    def check_post_poll_roundtrip() -> Optional[str]:
        posted = client.post(message="roundtrip", channel=channel, sender="conformance", type="info")
        messages = client.poll(channel=channel)
        if not any(m["id"] == posted["id"] for m in messages):
            return "posted message was not returned by poll"
        return None

    suite.check("post then poll returns the message", check_post_poll_roundtrip)

    def check_channel_isolation() -> Optional[str]:
        other = f"{channel}.other"
        client.post(message="isolated", channel=other, sender="conformance")
        messages = client.poll(channel=channel)
        if any(m["channel"] != channel for m in messages):
            return "poll leaked messages from another channel"
        return None

    suite.check("channel filter isolates traffic", check_channel_isolation)

    def check_since_id_cursor() -> Optional[str]:
        first = client.post(message="cursor-a", channel=channel, sender="conformance")
        second = client.post(message="cursor-b", channel=channel, sender="conformance")
        messages = client.poll(channel=channel, since_id=first["id"])
        ids = [m["id"] for m in messages]
        if first["id"] in ids:
            return "since_id returned the anchor message itself"
        if second["id"] not in ids:
            return "since_id dropped a message published after the anchor"
        return None

    suite.check("since_id advances the read cursor", check_since_id_cursor)

    def check_queue_hidden_from_poll() -> Optional[str]:
        queued = client.post(message="queued", channel=channel, sender="conformance", queue=queue)
        visible = client.poll(channel=channel)
        if any(m["id"] == queued["id"] for m in visible):
            return "queue message leaked into the default poll view"
        included = client.poll(channel=channel, include_queue=True)
        if not any(m["id"] == queued["id"] for m in included):
            return "include_queue=true did not return the queue message"
        return None

    suite.check("queue traffic is hidden from broadcast polls", check_queue_hidden_from_poll)

    def check_claim_ack_lifecycle() -> Optional[str]:
        task = client.post(message={"task": "lifecycle"}, channel=channel, sender="conformance", queue=queue)
        claimed = None
        for _ in range(20):
            candidate = client.claim(queue, worker, lease_seconds=args.lease_seconds)
            if candidate is None:
                return "claim returned nothing for a queue with pending work"
            if candidate["id"] == task["id"]:
                claimed = candidate
                break
            client.ack(queue, candidate["id"], worker, candidate["lease_id"])
        if claimed is None:
            return "could not claim the task that was just posted"
        if claimed["status"] != "leased" or not claimed["lease_id"]:
            return f"claimed task is not properly leased: {claimed.get('status')}"
        client.ack(queue, claimed["id"], worker, claimed["lease_id"])
        if client.claim(queue, worker) is not None:
            return "queue still has work after every task was acked"
        return None

    suite.check("claim/ack removes work from the queue", check_claim_ack_lifecycle)

    def check_empty_queue_returns_204() -> Optional[str]:
        resp = requests.post(
            f"{args.bus_url.rstrip('/')}/bus/queues/claim",
            json={"queue": f"{queue}.empty", "worker": worker},
            headers=_auth_headers(args.token),
            timeout=args.timeout,
        )
        if resp.status_code != 204:
            return f"expected 204 for an empty queue, got {resp.status_code}"
        return None

    suite.check("empty queue claim returns 204", check_empty_queue_returns_204)

    def check_nack_requeues() -> Optional[str]:
        task = client.post(message={"task": "nack"}, channel=channel, sender="conformance", queue=queue)
        claimed = client.claim(queue, worker, lease_seconds=args.lease_seconds)
        if claimed is None or claimed["id"] != task["id"]:
            return "could not claim the task to nack"
        client.nack(queue, claimed["id"], worker, claimed["lease_id"], requeue=True)
        again = client.claim(queue, f"{worker}-b", lease_seconds=args.lease_seconds)
        if again is None or again["id"] != task["id"]:
            return "nacked task was not returned to the queue"
        client.ack(queue, again["id"], f"{worker}-b", again["lease_id"])
        return None

    suite.check("nack returns work to the queue", check_nack_requeues)

    def check_stale_lease_rejected() -> Optional[str]:
        task = client.post(message={"task": "fencing"}, channel=channel, sender="conformance", queue=queue)
        claimed = client.claim(queue, worker, lease_seconds=args.lease_seconds)
        if claimed is None or claimed["id"] != task["id"]:
            return "could not claim the task for the fencing check"
        resp = requests.post(
            f"{args.bus_url.rstrip('/')}/bus/queues/ack",
            json={
                "queue": queue,
                "message_id": claimed["id"],
                "worker": "an-impostor",
                "lease_id": claimed["lease_id"],
            },
            headers=_auth_headers(args.token),
            timeout=args.timeout,
        )
        client.ack(queue, claimed["id"], worker, claimed["lease_id"])
        if resp.status_code != 409:
            return f"a worker that does not hold the lease could ack (got {resp.status_code})"
        return None

    suite.check("ack from a non-owner is rejected", check_stale_lease_rejected)

    def check_single_coordination_domain() -> Optional[str]:
        """Every connection must reach the same coordination state.

        All bus state lives in process memory, so running more than one worker
        process shards it: each process answers with its own queues, cursors,
        and jobs. Connection keep-alive pins a client to one process and hides
        this, so the probe deliberately opens fresh connections.
        """
        marker = client.post(message=f"domain-probe-{run}", channel=channel, sender="conformance")
        misses = 0
        attempts = 12
        for _ in range(attempts):
            resp = requests.get(
                f"{args.bus_url.rstrip('/')}/bus/messages",
                params={"channel": channel, "limit": 200},
                headers={**_auth_headers(args.token), "Connection": "close"},
                timeout=args.timeout,
            )
            ids = [m["id"] for m in resp.json().get("messages", [])]
            if marker["id"] not in ids:
                misses += 1
        if misses:
            return (
                f"{misses}/{attempts} fresh connections could not see a just-posted message; "
                f"the deployment is serving from more than one copy of the state "
                f"(run exactly one worker process)"
            )
        return None

    suite.check("all connections share one coordination state", check_single_coordination_domain)

    def check_validation() -> Optional[str]:
        cases = [
            ({"channel": channel}, "missing message"),
            ({"message": "x", "priority": "high"}, "non-integer priority"),
            ({"message": "x", "ttl_seconds": -1}, "negative ttl"),
        ]
        for payload, label in cases:
            resp = requests.post(
                f"{args.bus_url.rstrip('/')}/bus/messages",
                json=payload,
                headers=_auth_headers(args.token),
                timeout=args.timeout,
            )
            if resp.status_code != 400:
                return f"{label} should be rejected with 400, got {resp.status_code}"
        return None

    suite.check("malformed messages are rejected", check_validation)

    def check_orchestration_job() -> Optional[str]:
        job_id = f"conformance-job-{run}"
        job_queue = f"{queue}.orch"
        client.submit_job(
            {
                "job_id": job_id,
                "name": "conformance",
                "steps": [
                    {"id": "first", "queue": job_queue},
                    {"id": "second", "queue": job_queue, "depends_on": [{"step_id": "first"}]},
                ],
            }
        )
        ready = [step["id"] for step in client.get_ready_steps(job_id)]
        if "second" in ready:
            return "a dependent step was reported ready before its dependency completed"
        assignment = client.claim(job_queue, worker, lease_seconds=args.lease_seconds)
        if assignment is None:
            return "no assignment was dispatched for the ready step"
        if assignment["type"] != "orchestration.step.assign":
            return f"unexpected assignment type {assignment['type']}"
        client.ack(job_queue, assignment["id"], worker, assignment["lease_id"])
        client.report_step(job_id, "first", "completed", sender=worker)
        follow_up = client.claim(job_queue, worker, lease_seconds=args.lease_seconds)
        if follow_up is None:
            return "completing a dependency did not dispatch the next step"
        if follow_up["message"]["step"]["id"] != "second":
            return f"unexpected follow-up step {follow_up['message']['step']['id']}"
        client.ack(job_queue, follow_up["id"], worker, follow_up["lease_id"])
        client.report_step(job_id, "second", "completed", sender=worker)
        state = client.get_job(job_id)["state"]
        if state["second"]["status"] != "completed":
            return f"final job state is wrong: {state}"
        return None

    suite.check("orchestration dispatches steps in dependency order", check_orchestration_job)

    if args.slow:

        def check_lease_expiry_takeover() -> Optional[str]:
            expiry_queue = f"{queue}.expiry"
            task = client.post(
                message={"task": "expiry"}, channel=channel, sender="conformance", queue=expiry_queue
            )
            first = client.claim(expiry_queue, "worker-abandoning", lease_seconds=args.expiry_lease)
            if first is None or first["id"] != task["id"]:
                return "could not claim the task for the expiry check"
            if client.claim(expiry_queue, "worker-eager") is not None:
                return "a leased task was handed to a second worker before its lease expired"
            time.sleep(args.expiry_lease + 1.0)
            taken_over = client.claim(expiry_queue, "worker-taking-over", lease_seconds=args.lease_seconds)
            if taken_over is None:
                return "an abandoned task was not re-offered after its lease expired"
            if taken_over["lease_id"] == first["lease_id"]:
                return "lease id was reused after takeover, so fencing is not possible"
            client.ack(expiry_queue, taken_over["id"], "worker-taking-over", taken_over["lease_id"])
            return None

        suite.check("abandoned leases expire and are taken over", check_lease_expiry_takeover)

    print("\nCapability probes (reported, not required):", file=sys.stderr)

    def probe_agent_identity() -> bool:
        """Does the bus keep the `agent` handle documented in the README playbook?"""
        posted = client.post(
            message="identity probe",
            channel=channel,
            type="info",
            agent="agent:conformance.probe.0@ci",
        )
        return posted.get("agent") == "agent:conformance.probe.0@ci"

    def probe_metadata_passthrough() -> bool:
        posted = client.post(
            message="metadata probe",
            channel=channel,
            sender="conformance",
            metadata={"probe": True},
            ref="conformance-ref",
        )
        return posted.get("metadata") == {"probe": True} and posted.get("ref") == "conformance-ref"

    def probe_long_poll() -> bool:
        """A real long-poll blocks for the requested time when nothing arrives."""
        empty_channel = f"{channel}.longpoll"
        started = time.perf_counter()
        requests.get(
            f"{args.bus_url.rstrip('/')}/bus/messages",
            params={"channel": empty_channel, "wait_seconds": 2},
            headers=_auth_headers(args.token),
            timeout=args.timeout,
        )
        return (time.perf_counter() - started) > 1.5

    def probe_priority_ordering() -> bool:
        priority_queue = f"{queue}.priority"
        client.post(message="low", channel=channel, sender="conformance", queue=priority_queue, priority=0)
        client.post(message="high", channel=channel, sender="conformance", queue=priority_queue, priority=99)
        first = client.claim(priority_queue, worker, lease_seconds=args.lease_seconds)
        if first is None:
            return False
        client.ack(priority_queue, first["id"], worker, first["lease_id"])
        second = client.claim(priority_queue, worker, lease_seconds=args.lease_seconds)
        if second is not None:
            client.ack(priority_queue, second["id"], worker, second["lease_id"])
        return first["message"] == "high"

    def probe_presence_registry() -> bool:
        resp = requests.get(
            f"{args.bus_url.rstrip('/')}/bus/agents",
            headers=_auth_headers(args.token),
            timeout=args.timeout,
        )
        return resp.status_code == 200

    def probe_queue_stats() -> bool:
        resp = requests.get(
            f"{args.bus_url.rstrip('/')}/bus/queues/stats",
            headers=_auth_headers(args.token),
            timeout=args.timeout,
        )
        return resp.status_code == 200

    def probe_durability() -> bool:
        body = client.health()
        return bool(body.get("storage")) and body.get("storage") != "memory"

    suite.capability("agent identity preserved", probe_agent_identity, "plan phase S1")
    suite.capability("metadata/ref passthrough", probe_metadata_passthrough, "plan phase S1")
    suite.capability("server-side long poll (wait_seconds)", probe_long_poll, "plan phase S2")
    suite.capability("priority-ordered claim", probe_priority_ordering, "plan phase S3")
    suite.capability("agent presence registry", probe_presence_registry, "plan phase S4")
    suite.capability("queue depth stats", probe_queue_stats, "plan phase S5")
    suite.capability("durable storage backend", probe_durability, "plan phase S3")

    return suite


def _auth_headers(token: str) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a deployed IAC Bus against the protocol contract.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--bus-url", default=os.environ.get("BUS_URL", "http://127.0.0.1:8091"))
    parser.add_argument("--token", default=os.environ.get("BUS_API_TOKEN", ""))
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--lease-seconds", type=float, default=30.0)
    parser.add_argument("--expiry-lease", type=float, default=2.0, help="lease length for the slow expiry check")
    parser.add_argument("--slow", action="store_true", help="include time-based checks")
    parser.add_argument("--json", dest="json_path", default="", help="write the report to a file")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    print(f"Conformance suite against {args.bus_url}\n", file=sys.stderr)

    suite = run_suite(args)
    report = {
        "bus_url": args.bus_url,
        "generated_at": time.time(),
        "ok": suite.ok,
        "checks": [check.to_dict() for check in suite.checks],
        "capabilities": suite.capabilities,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            handle.write(rendered + "\n")

    failed = [check.name for check in suite.checks if not check.passed]
    if failed:
        print(f"\n{len(failed)} required check(s) failed: {failed}", file=sys.stderr)
        return 1
    print(f"\nAll {len(suite.checks)} required checks passed.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
