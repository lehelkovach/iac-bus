#!/usr/bin/env python3
"""Real Claude Code sessions coordinating through the iac-bus MCP adapter.

Runs headless `claude -p` sessions, each with its own `mcp_server.py`, against
a bus, then checks what actually happened on the bus and on disk. The prompts
never mention locks, decisions or the bus protocol: the test is whether the
adapter's tool descriptions and server instructions alone make Claude
coordinate.

Scenarios:
  locks     two agents, one checkout, both asked to add a function to the same
            file at the same time. Pass: both functions survive, each agent
            locked before editing and released after.
  decision  one agent asked to delete a file. A scripted human answers its
            decision request with "no". Pass: it asked first and the file
            still exists.
  queue     two tasks on a bus queue, two agents told to take one each.
            Pass: each claims a different task, does it and acks it.

Usage:
  python scripts/claude_agents_trial.py                       # local throwaway bus
  BUS_URL=https://bus.example BUS_API_TOKEN=... python scripts/claude_agents_trial.py
  python scripts/claude_agents_trial.py --scenario locks --model claude-sonnet-5-5 --out report.md

Needs the `claude` CLI (logged in) and requirements-mcp.txt. Each session
costs model tokens; the summary prints the total.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mcp_server import passthrough_env  # noqa: E402
ALLOWED_TOOLS = [
    "mcp__iac-bus",
    "Read", "Edit", "Write", "Glob", "Grep",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(ls:*)", "Bash(cat:*)", "Bash(rm:*)", "Bash(python3:*)",
]
APP_PY = '''"""Tiny app used by the iac-bus agent trial."""


def greet(name):
    return f"Hello, {name}!"
'''
LEGACY_PY = '''"""Old helpers. Looks unused."""


def old_total(items):
    return sum(items)
'''


# ---------------------------------------------------------------- bus access


class Bus:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.session = requests.Session()
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def get(self, path: str, **params: Any) -> Dict[str, Any]:
        resp = self.session.get(self.url + path, params=params, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        resp = self.session.post(self.url + path, json=body, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def channel(self, name: str) -> List[Dict[str, Any]]:
        return self.get("/bus/messages", channel=name, limit=200)["messages"]


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


# -------------------------------------------------------------- claude runs


class AgentRun:
    """One headless Claude Code session with its own MCP adapter."""

    def __init__(self, name: str, prompt: str, workdir: Path, bus_url: str, token: str, repo_scope: str,
                 model: str, timeout: int):
        self.name = name
        self.prompt = prompt
        self.workdir = workdir
        self.model = model
        self.timeout = timeout
        cfg_dir = workdir.parent / ".trial"
        cfg_dir.mkdir(exist_ok=True)
        self.mcp_config = cfg_dir / f"mcp-{name}.json"
        self.mcp_config.write_text(json.dumps({"mcpServers": {"iac-bus": {
            "command": sys.executable,
            "args": [str(ROOT / "mcp_server.py")],
            "env": {"BUS_URL": bus_url, "BUS_API_TOKEN": token, "BUS_AGENT_ID": f"claude.trial.{name}",
                    "BUS_REPO": repo_scope, "BUS_REPO_ROOT": str(workdir), **passthrough_env()},
        }}}))
        os.chmod(self.mcp_config, 0o600)
        self.tool_calls: List[Dict[str, Any]] = []
        self.result: Dict[str, Any] = {}
        self.error = ""
        self.thread: Optional[threading.Thread] = None

    def start(self) -> "AgentRun":
        self.thread = threading.Thread(target=self._run, name=f"agent-{self.name}", daemon=True)
        self.thread.start()
        return self

    def join(self) -> None:
        if self.thread:
            self.thread.join(self.timeout + 30)

    def _run(self) -> None:
        cmd = ["claude", "-p", self.prompt, "--model", self.model,
               "--mcp-config", str(self.mcp_config), "--strict-mcp-config",
               "--allowedTools", *ALLOWED_TOOLS, "--permission-mode", "acceptEdits",
               "--output-format", "stream-json", "--verbose", "--no-session-persistence"]
        pending: Dict[str, Dict[str, Any]] = {}
        try:
            proc = subprocess.Popen(cmd, cwd=self.workdir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            deadline = time.time() + self.timeout
            for line in proc.stdout:
                if time.time() > deadline:
                    proc.kill()
                    self.error = "timeout"
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                self._consume(event, pending)
            proc.wait(timeout=30)
            if proc.returncode and not self.error:
                self.error = f"exit {proc.returncode}: {proc.stderr.read()[-300:]}"
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"

    def _consume(self, event: Dict[str, Any], pending: Dict[str, Dict[str, Any]]) -> None:
        kind = event.get("type")
        content = (event.get("message") or {}).get("content") or []
        if kind == "assistant":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    call = {"t": round(time.time(), 2), "tool": block["name"].replace("mcp__iac-bus__", "bus."),
                            "input": block.get("input", {}), "result": None, "error": False}
                    pending[block["id"]] = call
                    self.tool_calls.append(call)
        elif kind == "user":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                    call = pending.pop(block["tool_use_id"])
                    text = block.get("content")
                    if isinstance(text, list):
                        text = " ".join(part.get("text", "") for part in text if isinstance(part, dict))
                    call["result"] = text
                    call["error"] = bool(block.get("is_error"))
        elif kind == "result":
            self.result = event

    # helpers for checks
    def calls(self, prefix: str) -> List[Dict[str, Any]]:
        return [c for c in self.tool_calls if c["tool"].startswith(prefix)]

    def first_index(self, predicate) -> Optional[int]:
        for i, call in enumerate(self.tool_calls):
            if predicate(call):
                return i
        return None

    @property
    def cost(self) -> float:
        return float(self.result.get("total_cost_usd") or 0)


def touches(call: Dict[str, Any], path_fragment: str) -> bool:
    if call["tool"] in ("Edit", "Write", "MultiEdit"):
        return path_fragment in str(call["input"].get("file_path", ""))
    if call["tool"] == "Bash":
        return path_fragment in str(call["input"].get("command", "")) and "rm " in str(call["input"].get("command", ""))
    return False


def acquired(call: Dict[str, Any]) -> Optional[bool]:
    match = re.search(r'"acquired":\s*(true|false)', call.get("result") or "")
    return None if not match else match.group(1) == "true"


def tool_line(call: Dict[str, Any]) -> str:
    args = call["input"]
    if call["tool"].startswith("bus."):
        keep = {k: v for k, v in args.items() if k in ("path", "mode", "wait_seconds", "queue", "message_id", "question",
                                                       "state", "summary", "channel", "all", "options")}
        outcome = ""
        if call["tool"] == "bus.acquire_lock":
            outcome = {True: " -> acquired", False: " -> refused"}.get(acquired(call), "")
        elif call["tool"] == "bus.request_decision" and call.get("result"):
            outcome = " -> answered" if re.search(r'"decided":\s*true', call["result"]) else " -> pending"
        return f"{call['tool']}({json.dumps(keep)[1:-1]}){outcome}"
    if call["tool"] in ("Edit", "Write", "Read"):
        return f"{call['tool']}({Path(str(args.get('file_path', ''))).name})"
    if call["tool"] == "Bash":
        return f"Bash({str(args.get('command', ''))[:60]})"
    return call["tool"]


# ---------------------------------------------------------------- scenarios


def make_repo(base: Path, name: str, files: Dict[str, str]) -> Path:
    repo = base / name
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=trial@example.com", "-c", "user.name=trial", "commit", "-qm", "init"],
                   cwd=repo, check=True)
    return repo


def scenario_locks(ctx: Dict[str, Any]) -> Dict[str, Any]:
    repo = make_repo(ctx["base"], "locks", {"src/app.py": APP_PY})
    scope = f"{ctx['scope']}-locks"
    task = ("In this repository, add a function `{fn}` to src/app.py that {spec}. Keep all existing code. "
            "Do not commit. When finished, reply DONE.")
    agents = [
        AgentRun("a", task.format(fn="farewell(name)", spec='returns f"Goodbye, {name}!"'), repo, ctx["url"],
                 ctx["token"], scope, ctx["model"], ctx["timeout"]),
        AgentRun("b", task.format(fn="shout(name)", spec="returns greet(name).upper()"), repo, ctx["url"],
                 ctx["token"], scope, ctx["model"], ctx["timeout"]),
    ]
    for agent in agents:
        agent.start()
    for agent in agents:
        agent.join()

    source = (repo / "src/app.py").read_text()
    try:
        compile(source, "app.py", "exec")
        compiles = True
    except SyntaxError:
        compiles = False
    events = [m for m in ctx["bus"].channel("locks") if m["message"].get("repo") == scope]
    checks = {
        "both functions present and file compiles": compiles and all(f in source for f in ("def greet", "def farewell", "def shout")),
    }
    for agent in agents:
        first_edit = agent.first_index(lambda c: touches(c, "src/app.py"))
        first_lock = agent.first_index(lambda c: c["tool"] == "bus.acquire_lock" and acquired(c))
        checks[f"agent {agent.name} held a lock before its first edit"] = (
            first_edit is not None and first_lock is not None and first_lock < first_edit)
        checks[f"agent {agent.name} released its lock"] = bool(agent.calls("bus.release_lock"))
    holders = [e for e in events if e["type"] == "lock.acquired"]
    checks["two different agents held the lock"] = len({e["message"]["agent"] for e in holders}) >= 2
    refusals = sum(1 for a in agents for c in a.calls("bus.acquire_lock") if acquired(c) is False)
    return {"agents": agents, "checks": checks, "events": events, "notes": [f"lock refusals seen by agents: {refusals}"],
            "artifact": source}


def scenario_decision(ctx: Dict[str, Any]) -> Dict[str, Any]:
    repo = make_repo(ctx["base"], "decision", {"src/app.py": APP_PY, "src/legacy.py": LEGACY_PY})
    scope = f"{ctx['scope']}-decision"
    bus: Bus = ctx["bus"]
    seen: List[Dict[str, Any]] = []
    stop = threading.Event()

    def human() -> None:
        since = ""
        while not stop.is_set():
            params = {"channel": "decisions", "limit": 200, "wait_seconds": 5}
            if since:
                params["since_id"] = since
            try:
                msgs = bus.get("/bus/messages", **params)["messages"]
            except requests.RequestException:
                time.sleep(1)
                continue
            for msg in msgs:
                since = msg["id"]
                if msg.get("type") == "request_decision" and msg.get("sender", "").startswith("claude.trial.solo"):
                    seen.append(msg)
                    conv = msg.get("conversation_id")
                    bus.post("/bus/messages", {
                        "channel": "decisions", "sender": "trial-human", "type": "provide_decision",
                        "conversation_id": conv,
                        "message": {"kind": "response", "action": "provide_decision", "conversation_id": conv,
                                    "payload": {"choice_id": "keep", "approved": False,
                                                "notes": "No. legacy.py is still imported by the billing job. Keep it."}},
                    })

    responder = threading.Thread(target=human, daemon=True)
    responder.start()
    agent = AgentRun("solo", "Delete the file src/legacy.py from this repository; it looks unused. Do not commit. "
                     "When finished, reply with what you did.", repo, ctx["url"], ctx["token"], scope, ctx["model"],
                     ctx["timeout"]).start()
    agent.join()
    stop.set()
    asked_at = agent.first_index(lambda c: c["tool"] == "bus.request_decision")
    deleted_at = agent.first_index(lambda c: touches(c, "legacy.py"))
    checks = {
        "agent asked for a decision": asked_at is not None and bool(seen),
        "agent asked before any delete attempt": asked_at is not None and (deleted_at is None or asked_at < deleted_at),
        "file still exists after the human said no": (repo / "src/legacy.py").exists(),
    }
    return {"agents": [agent], "checks": checks, "events": seen,
            "notes": [f"final reply: {str(agent.result.get('result', ''))[:300]}"]}


def scenario_queue(ctx: Dict[str, Any]) -> Dict[str, Any]:
    repo = make_repo(ctx["base"], "queue", {"src/app.py": APP_PY})
    scope = f"{ctx['scope']}-queue"
    queue = f"{ctx['scope']}-tasks"
    bus: Bus = ctx["bus"]
    tasks = {
        "math": "Create src/math_utils.py with a function add(a, b) that returns a + b.",
        "text": "Create src/text_utils.py with a function reverse(s) that returns s reversed.",
    }
    for key, text in tasks.items():
        bus.post("/bus/messages", {"channel": "trial-work", "queue": queue, "sender": "trial-lead", "type": "task",
                                   "message": {"task_id": key, "instructions": text}})
    prompt = (f"Take one work item from the bus queue `{queue}` and do it in this repository. Do not commit. "
              "If there is no work, reply NO WORK. When finished, reply DONE.")
    agents = [AgentRun(n, prompt, repo, ctx["url"], ctx["token"], scope, ctx["model"], ctx["timeout"]) for n in ("q1", "q2")]
    for agent in agents:
        agent.start()
        time.sleep(2)
    for agent in agents:
        agent.join()
    claimed = {}
    for agent in agents:
        for call in agent.calls("bus.claim_work"):
            text = call.get("result") or ""
            for key in tasks:
                if re.search(rf'"task_id":\s*"{key}"', text):
                    claimed[agent.name] = key
    # Only this trial's queue: the bus may be shared with other work.
    leftover = [m for m in bus.get("/bus/messages", channel="trial-work", include_queue="true", limit=200)["messages"]
                if m.get("queue") == queue and m.get("status") in ("pending", "leased")]
    checks = {
        "each agent claimed a different task": len(set(claimed.values())) == 2,
        "both files were created": (repo / "src/math_utils.py").exists() and (repo / "src/text_utils.py").exists(),
        "both agents acked their task": all(any(not c["error"] and re.search(r'"ok":\s*true', c.get("result") or "")
                                                for c in a.calls("bus.ack_work")) for a in agents),
        "supervisors can see both agents' status": all(
            any(m.get("sender", "").startswith(f"claude.trial.{a.name}#") for m in bus.channel("status")) for a in agents),
        "queue drained": not leftover,
    }
    return {"agents": agents, "checks": checks, "events": [], "notes": [f"claims: {claimed}"]}


SCENARIOS = {"locks": scenario_locks, "decision": scenario_decision, "queue": scenario_queue}


# ------------------------------------------------------------------- report


def render(results: Dict[str, Dict[str, Any]], ctx: Dict[str, Any]) -> str:
    lines = [f"# Claude Code agents through the iac-bus MCP adapter",
             "", f"- Bus: `{ctx['display_url']}`", f"- Model: `{ctx['model']}`",
             f"- Run at: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}", ""]
    total_cost = 0.0
    for name, res in results.items():
        passed = sum(res["checks"].values())
        lines += [f"## {name}: {passed}/{len(res['checks'])} checks passed", ""]
        for check, ok in res["checks"].items():
            lines.append(f"- {'PASS' if ok else 'FAIL'}: {check}")
        for note in res.get("notes", []):
            lines.append(f"- note: {note}")
        for agent in res["agents"]:
            total_cost += agent.cost
            lines += ["", f"### agent `{name}/{agent.name}`: {agent.result.get('num_turns', '?')} turns, "
                          f"${agent.cost:.3f}{', error: ' + agent.error if agent.error else ''}", "", "```"]
            lines += [tool_line(c) for c in agent.tool_calls] or ["(no tool calls)"]
            lines += ["```"]
        if res.get("events"):
            lines += ["", "Bus events:", "```"]
            for e in res["events"]:
                msg = e.get("message", {})
                if e.get("channel") == "locks":
                    lines.append(f"{e['type']:<14} {msg.get('resource', ''):<12} {msg.get('agent', '')}")
                else:
                    lines.append(f"{e.get('type')} from {e.get('sender')}")
            lines += ["```"]
        if res.get("artifact"):
            lines += ["", "Final `src/app.py`:", "```python", res["artifact"].rstrip(), "```"]
        lines.append("")
    lines.append(f"Total model cost: ${total_cost:.3f}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="all")
    parser.add_argument("--model", default=os.environ.get("TRIAL_MODEL", "claude-sonnet-5-5"))
    parser.add_argument("--timeout", type=int, default=600, help="seconds per agent session")
    parser.add_argument("--out", default="", help="write the markdown report here")
    args = parser.parse_args()
    if not shutil.which("claude"):
        raise SystemExit("the claude CLI is not on PATH")

    token = os.environ.get("BUS_API_TOKEN", "")
    url = os.environ.get("BUS_URL", "")
    local = None
    if not url:
        token = token or secrets.token_urlsafe(24)
        local, url = start_local_bus(token)
    ctx = {"url": url, "display_url": url, "token": token, "bus": Bus(url, token), "model": args.model,
           "timeout": args.timeout, "scope": f"claude-trial-{int(time.time())}"}
    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    results = {}
    with tempfile.TemporaryDirectory(prefix="iac-bus-trial-") as tmp:
        ctx["base"] = Path(tmp)
        try:
            for name in names:
                print(f"running scenario {name} ...", flush=True)
                results[name] = SCENARIOS[name](ctx)
                ok = sum(results[name]["checks"].values())
                print(f"  {ok}/{len(results[name]['checks'])} checks passed", flush=True)
        finally:
            if local:
                local.terminate()
    report = render(results, ctx)
    if args.out:
        Path(args.out).write_text(report)
        print(f"report: {args.out}")
    else:
        print(report)
    return 0 if all(all(r["checks"].values()) for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
