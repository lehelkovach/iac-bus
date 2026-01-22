#!/usr/bin/env python3
"""
Spawn Cursor Cloud Agents and register them on the IAC Bus.
"""

import argparse
import json
import os
import sys
from typing import Dict, List

import requests


def _cursor_headers(api_key: str) -> Dict[str, str]:
    # Cursor API uses Basic Auth with the key as username, blank password.
    return {"Authorization": f"Basic {api_key}", "Content-Type": "application/json"}


def _bus_headers(token: str) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def spawn_agents(endpoint: str, api_key: str, payload: Dict, count: int) -> List[Dict]:
    agents = []
    for i in range(count):
        body = dict(payload)
        body.setdefault("name", f"agent-{i + 1}")
        resp = requests.post(endpoint, headers=_cursor_headers(api_key), json=body, timeout=30)
        resp.raise_for_status()
        agents.append(resp.json())
    return agents


def announce_agents(bus_url: str, bus_token: str, agents: List[Dict], channel: str):
    for agent in agents:
        agent_id = agent.get("id") or agent.get("agent_id") or agent.get("name")
        msg = f"spawned:{agent_id}"
        payload = {"channel": channel, "sender": "controller", "message": msg}
        resp = requests.post(
            f"{bus_url}/bus/messages",
            headers=_bus_headers(bus_token),
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()


def main() -> int:
    parser = argparse.ArgumentParser(description="Spawn Cursor agents and announce via IAC Bus.")
    parser.add_argument("--cursor-endpoint", required=True, help="Cursor cloud agents endpoint URL")
    parser.add_argument("--count", type=int, default=1, help="Number of agents to spawn")
    parser.add_argument("--payload", default="{}", help="JSON payload for Cursor agent spawn")
    parser.add_argument("--bus-url", required=True, help="IAC Bus base URL, e.g. http://127.0.0.1:8091")
    parser.add_argument("--bus-token", default=os.environ.get("BUS_API_TOKEN", ""))
    parser.add_argument("--channel", default="ops")
    parser.add_argument("--cursor-api-key", default=os.environ.get("CURSOR_API_KEY", ""))
    args = parser.parse_args()

    if not args.cursor_api_key:
        print("Missing CURSOR_API_KEY", file=sys.stderr)
        return 2

    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as exc:
        print(f"Invalid payload JSON: {exc}", file=sys.stderr)
        return 2

    agents = spawn_agents(args.cursor_endpoint, args.cursor_api_key, payload, args.count)
    announce_agents(args.bus_url, args.bus_token, agents, args.channel)
    print(json.dumps({"spawned": agents}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
