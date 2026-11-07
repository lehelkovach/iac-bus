#!/usr/bin/env python3
"""Agent runner: start a Claude Code session for each work item on a bus queue.

Neither MCP nor the bus can start an agent's turn; this runner is the waking
piece (identity plan rung ID6). For every item it claims it:

1. registers a fresh run for the agent with the shared token, so the agent
   gets its own identity and run token, never the shared token;
2. writes an MCP config for ``mcp_server.py`` with that run token and
   ``BUS_WORK_MESSAGE_ID`` (so the agent's questions are linked to the item
   and reach an A2A requester as TASK_STATE_INPUT_REQUIRED);
3. runs ``claude -p`` in the work directory with that config and a prompt
   built from the item;
4. acks the item with Claude's final reply as the result (for an A2A task
   that becomes the artifact), or nacks it without requeue with the reason;
5. ends the agent's run, which revokes its token and releases any locks it
   still holds; posts running / completed / failed on the ``status`` channel.

The runner holds the queue lease and renews it while the agent runs, so a
task may outlive ``--lease``; several runners can share a queue.

  BUS_URL=https://bus.example BUS_API_TOKEN=... \\
    iac-bus-runner --queue repo-tasks --workdir ~/src/my-repo --once
  (or: python scripts/agent_runner.py ... from a checkout)

Each item costs one Claude Code session. ``--max-items`` caps a run.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from a2a_bridge import BusAPI  # noqa: E402
from mcp_server import passthrough_env  # noqa: E402

DEFAULT_TOOLS = [
    "mcp__iac-bus", "Read", "Edit", "Write", "Glob", "Grep",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(ls:*)", "Bash(python3 -m pytest:*)",
]
RESULT_MAX_CHARS = 4000


def work_text(item: Dict[str, Any]) -> Dict[str, Any]:
    body = item.get("message")
    if isinstance(body, dict):
        text = body.get("instructions") or body.get("text") or body.get("task") or json.dumps(body)
        return {"text": str(text), "lock_paths": [p for p in body.get("lock_paths") or [] if isinstance(p, str)],
                "a2a": body.get("a2a") if isinstance(body.get("a2a"), dict) else None}
    return {"text": str(body), "lock_paths": [], "a2a": None}


def build_prompt(item: Dict[str, Any], queue: str) -> str:
    work = work_text(item)
    lines = [
        f"You were started by the iac-bus runner to do one work item from the bus queue `{queue}` "
        f"(item {item['id']}) in this repository.",
        "",
        "Task:",
        work["text"],
        "",
    ]
    if work["lock_paths"]:
        lines.append(f"The requester expects changes in: {', '.join(work['lock_paths'])}.")
    if work["a2a"]:
        lines.append("This task came from another agent over A2A; your final reply is returned to it as the result.")
    lines += [
        "The runner owns the work item: do not call claim_work, ack_work or nack_work.",
        "Follow the bus rules in your MCP server instructions: lock before editing, check_lock before "
        "committing, release after, and ask with request_decision before risky steps.",
        "Do not commit unless the task says so.",
        "When you are done, reply with a short summary of what you changed and anything left undone.",
    ]
    return "\n".join(lines)


def parse_claude_output(stdout: str) -> Dict[str, Any]:
    """`claude -p --output-format json` prints one JSON object; take the last parseable line."""
    for line in reversed(stdout.strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return {}


class Runner:
    def __init__(self, bus_url: str, shared_token: str, queues: List[str], workdir: Path, repo: str = "",
                 handle: str = "runner", agent_cmd: str = "claude", model: str = "", lease: int = 3600,
                 agent_timeout: int = 1800, allowed_tools: Optional[List[str]] = None, poll: float = 5.0,
                 session: Optional[requests.Session] = None):
        self.bus_url = bus_url.rstrip("/")
        self.shared = BusAPI(bus_url, shared_token, session)
        self.bus = BusAPI(bus_url, shared_token, session)
        self.queues = queues
        self.workdir = Path(workdir).resolve()
        self.repo = repo or self.workdir.name
        self.handle = handle
        self.agent_cmd = agent_cmd
        self.model = model
        self.lease = lease
        self.agent_timeout = agent_timeout
        self.allowed_tools = allowed_tools or DEFAULT_TOOLS
        self.poll = poll
        self.actor = handle
        self.history: List[Dict[str, Any]] = []

    def say(self, line: str) -> None:
        print(f"[runner] {line}", flush=True)

    def start(self) -> "Runner":
        status, data = self.shared.post("/agents/register", {"handle": self.handle, "role": "runner",
                                                             "medium": "automation", "run_ttl_seconds": 86400})
        if status != 201 or not (data or {}).get("run_token"):
            raise SystemExit(f"could not register the runner's run: {status} {data}")
        self.bus.token = data["run_token"]
        self.actor = data["actor"]
        self.say(f"running as {self.actor}; queues {self.queues}; workdir {self.workdir}")
        return self

    def stop(self) -> None:
        self.bus.post("/agents/runs/end", {})

    def process_once(self, max_items: int = 0) -> int:
        done = 0
        for queue in self.queues:
            while not max_items or done < max_items:
                status, data = self.bus.post("/bus/queues/claim", {"queue": queue, "worker": self.actor,
                                                                   "lease_seconds": self.lease})
                if status != 200:
                    break
                self.handle_item(queue, data["message"])
                done += 1
        return done

    def run_forever(self, max_items: int = 0) -> None:
        total = 0
        while not max_items or total < max_items:
            n = self.process_once(max_items - total if max_items else 0)
            total += n
            if not n:
                time.sleep(self.poll)

    # ------------------------------------------------------------ one item

    def _status(self, state: str, summary: str, item_id: str) -> None:
        self.bus.post("/bus/messages", {
            "channel": "status", "type": "report_status",
            "message": {"kind": "status", "action": "report_status", "from": self.actor, "state": state,
                        "payload": {"summary": summary, "state": state, "details": {"work_message_id": item_id}}},
        })

    def handle_item(self, queue: str, item: Dict[str, Any]) -> Dict[str, Any]:
        record: Dict[str, Any] = {"item": item["id"], "queue": queue}
        status, reg = self.shared.post("/agents/register", {
            "handle": f"{self.handle}.agent", "role": "worker", "medium": "automation", "session_id": item["id"],
            "run_ttl_seconds": self.agent_timeout + 600})
        if status != 201:
            self._finish(queue, item, ok=False, text=f"could not register the agent's run: {status}")
            record.update(outcome="failed", reason="register")
            self.history.append(record)
            return record
        agent_actor, run_id = reg["actor"], reg["run"]["run_id"]
        record["agent"] = agent_actor
        self.say(f"{item['id']}: starting {agent_actor} for {work_text(item)['text'][:70]!r}")
        self._status("running", f"started {agent_actor} for work {item['id']} on {queue}", item["id"])

        stop = threading.Event()
        threading.Thread(target=self._keepalive, args=(queue, item, stop), daemon=True).start()
        with tempfile.TemporaryDirectory(prefix="iac-runner-") as tmp:
            cfg_path = Path(tmp) / "mcp.json"
            cfg_path.write_text(json.dumps({"mcpServers": {"iac-bus": {
                "command": sys.executable,
                "args": [str(ROOT / "mcp_server.py")],
                "env": {"BUS_URL": self.bus_url, "BUS_RUN_TOKEN": reg["run_token"], "BUS_AGENT_ID": f"{self.handle}.agent",
                        "BUS_REPO": self.repo, "BUS_REPO_ROOT": str(self.workdir),
                        "BUS_WORK_MESSAGE_ID": item["id"], **passthrough_env()},
            }}}))
            os.chmod(cfg_path, 0o600)
            cmd = shlex.split(self.agent_cmd) + [
                "-p", build_prompt(item, queue), "--mcp-config", str(cfg_path), "--strict-mcp-config",
                "--allowedTools", *self.allowed_tools, "--permission-mode", "acceptEdits",
                "--output-format", "json", "--no-session-persistence"]
            if self.model:
                cmd += ["--model", self.model]
            # The agent never sees the shared bus token.
            env = {k: v for k, v in os.environ.items() if k not in ("BUS_API_TOKEN", "BUS_RUN_TOKEN")}
            started = time.time()
            try:
                proc = subprocess.run(cmd, cwd=self.workdir, env=env, capture_output=True, text=True,
                                      timeout=self.agent_timeout)
                out = parse_claude_output(proc.stdout)
                ok = proc.returncode == 0 and bool(out) and not out.get("is_error")
                text = str(out.get("result") or "").strip() if ok else (
                    str(out.get("result") or "").strip() or proc.stderr.strip()[-500:] or f"agent exited {proc.returncode}")
                record["cost_usd"] = out.get("total_cost_usd")
            except subprocess.TimeoutExpired:
                ok, text = False, f"agent did not finish within {self.agent_timeout}s"
            record["seconds"] = round(time.time() - started, 1)
        stop.set()
        try:
            self._finish(queue, item, ok=ok, text=text)
        finally:
            self.shared.post("/agents/runs/end", {"run_id": run_id})
        record.update(outcome="completed" if ok else "failed", result=text[:RESULT_MAX_CHARS])
        self.history.append(record)
        return record

    def _keepalive(self, queue: str, item: Dict[str, Any], stop: threading.Event) -> None:
        """Renew the item's lease every third of its length while the agent runs."""
        while not stop.wait(max(1.0, self.lease / 3.0)):
            try:
                self.bus.post("/bus/queues/renew", {"queue": queue, "message_id": item["id"], "worker": self.actor,
                                                    "lease_id": item["lease_id"], "lease_seconds": self.lease})
            except requests.RequestException:
                pass

    def _finish(self, queue: str, item: Dict[str, Any], ok: bool, text: str) -> None:
        body = {"queue": queue, "message_id": item["id"], "worker": self.actor, "lease_id": item["lease_id"]}
        if ok:
            body["result"] = text[:RESULT_MAX_CHARS] or "done"
            self.bus.post("/bus/queues/ack", body)
            self._status("completed", f"work {item['id']} done: {text[:200]}", item["id"])
        else:
            body.update(requeue=False, reason=text[:RESULT_MAX_CHARS])
            self.bus.post("/bus/queues/nack", body)
            self._status("failed", f"work {item['id']} failed: {text[:200]}", item["id"])
        self.say(f"{item['id']}: {'acked' if ok else 'nacked'}: {text[:120]!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--queue", action="append", required=True, help="bus queue to take work from (repeatable)")
    ap.add_argument("--workdir", default=".", help="repository checkout the agent works in")
    ap.add_argument("--repo", default="", help="lock scope (default: the workdir's folder name)")
    ap.add_argument("--handle", default=os.environ.get("RUNNER_HANDLE", "runner"))
    ap.add_argument("--agent-cmd", default="claude")
    ap.add_argument("--model", default="")
    ap.add_argument("--lease", type=int, default=3600)
    ap.add_argument("--agent-timeout", type=int, default=1800)
    ap.add_argument("--allow", action="append", default=[],
                    help="extra Claude Code tool permission for the agent, e.g. 'Bash(pytest:*)' (repeatable)")
    ap.add_argument("--once", action="store_true", help="process what is queued now, then exit")
    ap.add_argument("--max-items", type=int, default=0, help="stop after this many items (0 = no limit)")
    args = ap.parse_args()
    bus_url = os.environ.get("BUS_URL", "").strip()
    if not bus_url:
        raise SystemExit("set BUS_URL and BUS_API_TOKEN")
    runner = Runner(bus_url, os.environ.get("BUS_API_TOKEN", ""), args.queue, Path(args.workdir), repo=args.repo,
                    handle=args.handle, agent_cmd=args.agent_cmd, model=args.model, lease=args.lease,
                    agent_timeout=args.agent_timeout, allowed_tools=DEFAULT_TOOLS + args.allow).start()
    try:
        if args.once:
            print(f"processed {runner.process_once(args.max_items)} item(s)")
        else:
            runner.run_forever(args.max_items)
    finally:
        runner.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
