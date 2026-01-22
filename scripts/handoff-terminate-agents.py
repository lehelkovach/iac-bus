#!/usr/bin/env python3
"""
Terminate Cursor Cloud Agents after a handoff.
"""

import argparse
import os
import re
import sys
from typing import List, Set

import requests

ID_RE = re.compile(r"[0-9a-fA-F-]{36}")


def _cursor_headers(api_key: str) -> dict:
    return {"Authorization": f"Basic {api_key}"}


def _bus_headers(token: str) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _parse_ids(text: str) -> List[str]:
    return ID_RE.findall(text or "")


def _fetch_bus_ids(bus_url: str, token: str, channel: str, prefix: str) -> List[str]:
    resp = requests.get(
        f"{bus_url}/bus/messages?channel={channel}&limit=200",
        headers=_bus_headers(token),
        timeout=10,
    )
    resp.raise_for_status()
    ids = []
    for msg in resp.json().get("messages", []):
        message = msg.get("message", "")
        if isinstance(message, str) and message.startswith(prefix):
            ids.extend(_parse_ids(message))
    return ids


def _fetch_file_ids(path: str) -> List[str]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return _parse_ids(handle.read())
    except FileNotFoundError:
        return []


def _terminate_agent(endpoint: str, api_key: str, agent_id: str) -> None:
    url = f"{endpoint.rstrip('/')}/{agent_id}"
    resp = requests.delete(url, headers=_cursor_headers(api_key), timeout=30)
    resp.raise_for_status()


def main() -> int:
    parser = argparse.ArgumentParser(description="Terminate Cursor Cloud Agents.")
    parser.add_argument("--cursor-endpoint", default="https://api.cursor.com/v0/agents")
    parser.add_argument("--cursor-api-key", default=os.environ.get("CURSOR_API_KEY", ""))
    parser.add_argument("--agent-ids", default="", help="Comma-separated agent IDs")
    parser.add_argument("--agent-ids-file", default="")
    parser.add_argument("--bus-url", default="")
    parser.add_argument("--bus-token", default=os.environ.get("BUS_API_TOKEN", ""))
    parser.add_argument("--channel", default="ops")
    parser.add_argument("--prefix", default="spawned:")
    args = parser.parse_args()

    if not args.cursor_api_key:
        print("Missing CURSOR_API_KEY", file=sys.stderr)
        return 2

    ids: Set[str] = set()
    if args.agent_ids:
        ids.update([i.strip() for i in args.agent_ids.split(",") if i.strip()])
    if args.agent_ids_file:
        ids.update(_fetch_file_ids(args.agent_ids_file))
    if args.bus_url:
        ids.update(_fetch_bus_ids(args.bus_url, args.bus_token, args.channel, args.prefix))

    if not ids:
        print("No agent IDs found. Provide --agent-ids, --agent-ids-file, or --bus-url.", file=sys.stderr)
        return 2

    failures = []
    for agent_id in sorted(ids):
        try:
            _terminate_agent(args.cursor_endpoint, args.cursor_api_key, agent_id)
            print(f"terminated:{agent_id}")
        except Exception as exc:
            failures.append((agent_id, str(exc)))

    if failures:
        for agent_id, err in failures:
            print(f"failed:{agent_id} {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
