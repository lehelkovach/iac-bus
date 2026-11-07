#!/usr/bin/env python3
"""Benchmarks for iac-bus: does it prevent lost updates, and what does it cost?

Three measurements, each against a bus (BUS_URL/BUS_API_TOKEN, or a throwaway
local one):

  lost-updates   N simulated agents each do R rounds of read-modify-write on
                 one shared file, with a short "edit" delay between read and
                 write. Without coordination, concurrent rounds overwrite each
                 other (lost updates). With bus locks, each round holds an
                 exclusive lock on the file and validates its fencing token
                 right before writing. The expected final count is N*R.
  fencing        A writer takes a lock with a short TTL, stalls past it, and
                 another writer takes the path over. The stale writer checks
                 its fencing token before writing: the write must be refused.
  latency        p50/p95/p99 and throughput for acquire+release, claim+ack and
                 post+read, with C concurrent clients.

Simulated agents are threads using the HTTP API with distinct names under the
shared token (identity is not what is measured here). Results print as
markdown; --json writes the raw numbers too.

  python scripts/bench.py                              # local throwaway bus
  BUS_URL=https://bus.example BUS_API_TOKEN=... python scripts/bench.py --agents 4 --rounds 25
"""

from __future__ import annotations

import argparse
import json
import os
import random
import secrets
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List

import requests

ROOT = Path(__file__).resolve().parents[1]


class Bus:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.local = threading.local()
        self.token = token

    def session(self) -> requests.Session:
        s = getattr(self.local, "s", None)
        if s is None:
            s = requests.Session()
            if self.token:
                s.headers["Authorization"] = f"Bearer {self.token}"
            self.local.s = s
        return s

    def post(self, path: str, body: Dict[str, Any]) -> requests.Response:
        return self.session().post(self.url + path, json=body, timeout=60)

    def get(self, path: str, **params: Any) -> requests.Response:
        return self.session().get(self.url + path, params=params, timeout=60)


def start_local_bus(token: str) -> tuple[subprocess.Popen, str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = dict(os.environ, BUS_PORT=str(port), BUS_HOST="127.0.0.1", BUS_API_TOKEN=token, BUS_LOG_LEVEL="WARNING")
    proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if requests.get(url + "/health", timeout=1).ok:
                return proc, url
        except requests.RequestException:
            time.sleep(0.1)
    proc.kill()
    raise SystemExit("local bus did not start")


# ------------------------------------------------------------ lost updates


def rmw_round(path: Path, agent: str, edit_seconds: float) -> None:
    """One read-modify-write of the shared file, like an agent editing it."""
    data = json.loads(path.read_text()) if path.exists() else {"count": 0, "by": []}
    time.sleep(edit_seconds)  # the agent "edits": the window where a concurrent writer is lost
    data["count"] += 1
    data["by"].append(agent)
    tmp = path.with_suffix(".tmp." + agent)
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def lost_updates(bus: Bus | None, agents: int, rounds: int, edit_ms: float, repo: str, workdir: Path) -> Dict[str, Any]:
    shared = workdir / "shared.json"
    shared.write_text(json.dumps({"count": 0, "by": []}))
    waits: List[float] = []
    errors: List[str] = []
    started = time.time()

    def worker(agent: str) -> None:
        for _ in range(rounds):
            edit = random.uniform(0.5, 1.5) * edit_ms / 1000.0
            if bus is None:
                rmw_round(shared, agent, edit)
                continue
            t0 = time.time()
            resp = bus.post("/bus/locks/acquire", {"repo": repo, "resource": "shared.json", "agent": agent,
                                                   "ttl_seconds": 30, "wait_seconds": 30})
            waits.append(time.time() - t0)
            if resp.status_code != 200:
                errors.append(f"{agent}: acquire {resp.status_code}")
                continue
            lock = resp.json()
            try:
                data = json.loads(shared.read_text())
                time.sleep(edit)
                check = bus.post("/bus/locks/validate", {"repo": repo, "resource": "shared.json",
                                                        "fencing_token": lock["fencing_token"], "agent": agent})
                if check.status_code != 200:
                    errors.append(f"{agent}: fenced")
                    continue
                data["count"] += 1
                data["by"].append(agent)
                shared.write_text(json.dumps(data))
            finally:
                bus.post("/bus/locks/release", {"lock_id": lock["lock_id"], "agent": agent})

    threads = [threading.Thread(target=worker, args=(f"agent-{i}",)) for i in range(agents)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - started
    final = json.loads(shared.read_text())["count"]
    expected = agents * rounds
    return {
        "mode": "locks" if bus else "none",
        "agents": agents, "rounds": rounds, "edit_ms": edit_ms,
        "expected": expected, "final": final, "lost": expected - final,
        "lost_pct": round(100.0 * (expected - final) / expected, 1),
        "seconds": round(elapsed, 2),
        "rounds_per_second": round(expected / elapsed, 1),
        "lock_wait_ms_p50": round(1000 * statistics.median(waits), 1) if waits else None,
        "lock_wait_ms_max": round(1000 * max(waits), 1) if waits else None,
        "errors": errors[:5],
    }


# ---------------------------------------------------------------- fencing


def fencing(bus: Bus, repo: str, workdir: Path) -> Dict[str, Any]:
    shared = workdir / "fenced.json"
    shared.write_text(json.dumps({"writes": []}))
    stale = bus.post("/bus/locks/acquire", {"repo": repo, "resource": "fenced.json", "agent": "stale",
                                            "ttl_seconds": 0.5}).json()
    time.sleep(0.8)  # the stale writer stalls past its lease
    fresh = bus.post("/bus/locks/acquire", {"repo": repo, "resource": "fenced.json", "agent": "fresh",
                                            "ttl_seconds": 30}).json()
    took_over = fresh.get("fencing_token", 0) > stale.get("fencing_token", 0)
    fresh_data = json.loads(shared.read_text())
    fresh_data["writes"].append("fresh")
    shared.write_text(json.dumps(fresh_data))
    check = bus.post("/bus/locks/validate", {"repo": repo, "resource": "fenced.json",
                                            "fencing_token": stale["fencing_token"], "agent": "stale"})
    stale_blocked = check.status_code == 409
    if not stale_blocked:
        d = json.loads(shared.read_text())
        d["writes"].append("stale")
        shared.write_text(json.dumps(d))
    bus.post("/bus/locks/release", {"lock_id": fresh["lock_id"], "agent": "fresh"})
    return {"takeover_after_expiry": took_over, "stale_write_refused": stale_blocked,
            "writes_on_disk": json.loads(shared.read_text())["writes"],
            "validate_reason": check.json().get("reason")}


# ---------------------------------------------------------------- latency


def timed(fn: Callable[[], None], clients: int, ops_per_client: int) -> Dict[str, Any]:
    samples: List[float] = []
    lock = threading.Lock()
    errors = [0]

    def worker() -> None:
        for _ in range(ops_per_client):
            t0 = time.perf_counter()
            try:
                fn()
            except Exception:  # noqa: BLE001
                errors[0] += 1
            dt = time.perf_counter() - t0
            with lock:
                samples.append(dt)

    started = time.perf_counter()
    threads = [threading.Thread(target=worker) for _ in range(clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - started
    samples.sort()

    def pct(p: float) -> float:
        return round(1000 * samples[min(len(samples) - 1, int(p * len(samples)))], 1)

    return {"ops": len(samples), "clients": clients, "p50_ms": pct(0.5), "p95_ms": pct(0.95), "p99_ms": pct(0.99),
            "ops_per_second": round(len(samples) / elapsed, 1), "errors": errors[0]}


def latency(bus: Bus, repo: str, clients: int, ops: int) -> Dict[str, Any]:
    counter = [0]
    counter_lock = threading.Lock()

    def unique() -> str:
        with counter_lock:
            counter[0] += 1
            return f"{counter[0]}"

    def acquire_release() -> None:
        n = unique()
        r = bus.post("/bus/locks/acquire", {"repo": repo, "resource": f"bench/{n}.py", "agent": f"a{n}", "ttl_seconds": 30})
        r.raise_for_status()
        bus.post("/bus/locks/release", {"lock_id": r.json()["lock_id"], "agent": f"a{n}"}).raise_for_status()

    def claim_ack() -> None:
        n = unique()
        q = f"bench-{repo}"
        bus.post("/bus/messages", {"channel": q, "queue": q, "sender": "bench", "message": n}).raise_for_status()
        r = bus.post("/bus/queues/claim", {"queue": q, "worker": f"w{n}", "lease_seconds": 30})
        r.raise_for_status()
        m = r.json()["message"]
        bus.post("/bus/queues/ack", {"queue": q, "message_id": m["id"], "worker": f"w{n}", "lease_id": m["lease_id"]}).raise_for_status()

    def post_read() -> None:
        n = unique()
        ch = f"bench-ch-{repo}"
        r = bus.post("/bus/messages", {"channel": ch, "sender": "bench", "message": n})
        r.raise_for_status()
        bus.get("/bus/messages", channel=ch, limit=1).raise_for_status()

    return {
        "acquire+release": timed(acquire_release, clients, ops),
        "claim+ack": timed(claim_ack, clients, ops),
        "post+read": timed(post_read, clients, ops),
    }


# ----------------------------------------------------------------- report


def render(results: Dict[str, Any]) -> str:
    out = [f"Bus: `{results['bus']}`  (run {results['when']})", ""]
    lu = results["lost_updates"]
    out += ["### Lost updates: N agents, R read-modify-write rounds each on one file", "",
            "| coordination | agents | rounds each | expected writes | writes kept | lost | time | lock wait p50 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in lu:
        out.append(f"| {'bus locks' if r['mode'] == 'locks' else 'none'} | {r['agents']} | {r['rounds']} | {r['expected']} | "
                   f"{r['final']} | **{r['lost']} ({r['lost_pct']}%)** | {r['seconds']}s | "
                   f"{'' if r['lock_wait_ms_p50'] is None else str(r['lock_wait_ms_p50']) + ' ms'} |")
    f = results["fencing"]
    out += ["", "### Fencing: a writer whose lease expired and was taken over", "",
            f"- takeover got a higher fencing token: **{f['takeover_after_expiry']}**",
            f"- stale writer's token refused before its write: **{f['stale_write_refused']}** ({f['validate_reason']})",
            f"- writes on disk: {f['writes_on_disk']}"]
    lat = results["latency"]
    out += ["", f"### API latency ({lat['acquire+release']['clients']} concurrent clients)", "",
            "| operation | ops | p50 | p95 | p99 | ops/s | errors |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for name, r in lat.items():
        out.append(f"| {name} | {r['ops']} | {r['p50_ms']} ms | {r['p95_ms']} ms | {r['p99_ms']} ms | {r['ops_per_second']} | {r['errors']} |")
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agents", type=int, default=4)
    ap.add_argument("--rounds", type=int, default=25)
    ap.add_argument("--edit-ms", type=float, default=30.0, help="simulated edit time between read and write")
    ap.add_argument("--clients", type=int, default=8, help="concurrent clients for the latency runs")
    ap.add_argument("--ops", type=int, default=50, help="operations per client for the latency runs")
    ap.add_argument("--json", default="", help="also write raw results here")
    args = ap.parse_args()

    token = os.environ.get("BUS_API_TOKEN", "")
    url = os.environ.get("BUS_URL", "")
    local = None
    if not url:
        token = token or secrets.token_urlsafe(16)
        local, url = start_local_bus(token)
    bus = Bus(url, token)
    repo = f"bench-{int(time.time())}"
    results: Dict[str, Any] = {"bus": "local throwaway" if local else url,
                               "when": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())}
    try:
        with tempfile.TemporaryDirectory(prefix="iac-bench-") as tmp:
            work = Path(tmp)
            print("lost-updates without coordination ...", flush=True)
            no_locks = lost_updates(None, args.agents, args.rounds, args.edit_ms, repo, work)
            print("lost-updates with bus locks ...", flush=True)
            with_locks = lost_updates(bus, args.agents, args.rounds, args.edit_ms, repo, work)
            results["lost_updates"] = [no_locks, with_locks]
            print("fencing ...", flush=True)
            results["fencing"] = fencing(bus, repo, work)
            print("latency ...", flush=True)
            results["latency"] = latency(bus, repo, args.clients, args.ops)
    finally:
        if local:
            local.terminate()
    report = render(results)
    print()
    print(report)
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2))
    ok = with_locks["lost"] == 0 and results["fencing"]["stale_write_refused"] and not with_locks["errors"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
