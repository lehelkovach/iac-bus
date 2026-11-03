#!/usr/bin/env python3
"""Two coding agents, one file: a lock hand-over driven through the MCP adapter.

Spawns two `mcp_server.py` processes (agent A and agent B), talks to each over
MCP stdio exactly as Claude Code would, and prints a readable transcript:

  A acquires src/app.py -> B gets a conflict -> B waits -> A checks its lock,
  "commits" and releases -> B acquires with a higher fencing token -> A's old
  token is now stale -> B releases.

Run it through scripts/demo_two_agents.sh, which starts a throwaway bus when
BUS_URL is not set. Exits non-zero if any step does not behave as expected.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import anyio
from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
PATH = "src/app.py"


class DemoFailure(Exception):
    pass


def expect(cond: bool, what: str) -> None:
    if not cond:
        raise DemoFailure(what)


def say(who: str, text: str) -> None:
    print(f"[{who}] {text}", flush=True)


def result_of(call_result):
    if call_result.is_error:
        raise DemoFailure(call_result.content[0].text if call_result.content else "tool error")
    data = call_result.structured_content or {}
    return data.get("result", data)


def server_params(agent: str, repo: str) -> StdioServerParameters:
    env = {
        "BUS_URL": os.environ["BUS_URL"],
        "BUS_API_TOKEN": os.environ.get("BUS_API_TOKEN", ""),
        "BUS_AGENT_ID": agent,
        "BUS_REPO": repo,
        "BUS_REPO_ROOT": str(ROOT),
    }
    return StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp_server.py")], cwd=str(ROOT), env=env)


async def main() -> int:
    repo = os.environ.get("DEMO_REPO") or f"demo-{int(time.time())}"
    print("== iac-bus: two agents, one file ==")
    print(f"bus: {os.environ['BUS_URL']}   lock scope (repo): {repo}")
    print("Each agent is its own MCP server process (python mcp_server.py), driven over stdio.\n")

    async with Client(server_params("agent-a", repo)) as a, Client(server_params("agent-b", repo)) as b:
        held = result_of(await a.call_tool("list_locks", {}))
        a_id = held["you"]
        b_id = result_of(await b.call_tool("list_locks", {}))["you"]
        print(f"agent A is {a_id}")
        print(f"agent B is {b_id}\n")
        expect(a_id != b_id, "agents must have distinct identities")

        say("A", f'acquire_lock("{PATH}", reason="refactor login flow", ttl_seconds=120)')
        got_a = result_of(await a.call_tool("acquire_lock", {"path": PATH, "reason": "refactor login flow", "ttl_seconds": 120}))
        expect(got_a["acquired"], "A should acquire")
        say("A", f"  -> acquired. fencing_token={got_a['fencing_token']}, expires in {got_a['expires_in_seconds']}s")

        say("B", f'acquire_lock("{PATH}")')
        got_b = result_of(await b.call_tool("acquire_lock", {"path": PATH}))
        expect(not got_b["acquired"], "B should be refused while A holds the lock")
        say("B", f"  -> refused (HTTP 409): held by {got_b['held_by']} ({got_b['holder_mode']}, "
                 f"\"{got_b['holder_reason']}\"), expires in {got_b['expires_in_seconds']}s")
        say("B", f"     tool says: {got_b['next']}")

        waited = {}

        async def b_waits():
            started = time.monotonic()
            waited["result"] = result_of(await b.call_tool("acquire_lock", {"path": PATH, "wait_seconds": 15}))
            waited["elapsed"] = time.monotonic() - started

        async with anyio.create_task_group() as tg:
            say("B", f'acquire_lock("{PATH}", wait_seconds=15)   ... blocking until the path frees up')
            tg.start_soon(b_waits)
            await anyio.sleep(1.0)

            check = result_of(await a.call_tool("check_lock", {"path": PATH}))
            expect(check["valid"], "A's lock should still be valid before commit")
            say("A", f'check_lock("{PATH}") -> valid (fencing_token {check["fencing_token"]} is live)')
            say("A", f"(edits {PATH} and commits)")
            rel = result_of(await a.call_tool("release_lock", {"path": PATH}))
            expect(rel["released"] == [PATH], "A should release its lock")
            say("A", f'release_lock("{PATH}") -> released {rel["released"]}')

        got_b2 = waited["result"]
        expect(got_b2["acquired"], "B should acquire after A releases")
        expect(got_b2["fencing_token"] > got_a["fencing_token"], "B's token must be higher than A's")
        say("B", f"  -> acquired after waiting {waited['elapsed']:.1f}s. fencing_token={got_b2['fencing_token']} "
                 f"(> A's {got_a['fencing_token']})")

        stale = result_of(await a.call_tool("check_lock", {"path": PATH, "fencing_token": got_a["fencing_token"]}))
        expect(not stale["valid"], "A's old token must be stale now")
        say("A", f"check_lock with old token {got_a['fencing_token']} -> stale: {stale['reason']}")

        rel_b = result_of(await b.call_tool("release_lock", {"all": True}))
        say("B", f"release_lock(all=true) -> released {rel_b['released']}")

        events = result_of(await a.call_tool("read_messages", {"channel": "locks", "limit": 200}))
        mine = [e for e in events["messages"] if e["message"].get("repo") == repo]
        print('\nEvents the bus posted on channel "locks":')
        for e in mine:
            msg = e["message"]
            print(f"  {e['type']:<14} {msg['resource']:<12} {msg['agent']:<18} token={msg['fencing_token']}")
        expect([e["type"] for e in mine] == ["lock.acquired", "lock.released", "lock.acquired", "lock.released"],
               "unexpected lock event sequence")

    print("\nDemo OK: the lock kept B out while A edited, and handed over cleanly.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(anyio.run(main))
    except DemoFailure as exc:
        print(f"\nDEMO FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
