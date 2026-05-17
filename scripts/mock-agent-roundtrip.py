#!/usr/bin/env python3
"""
Run a small orchestrator/worker coordination exercise against the IAC Bus.
"""

import argparse
import json
import os
import uuid
from typing import Dict, List

import requests


def _bus_headers(token: str) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def post_message(bus_url: str, token: str, payload: Dict) -> Dict:
    resp = requests.post(
        f"{bus_url.rstrip('/')}/bus/messages",
        headers=_bus_headers(token),
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["message"]


def get_messages(bus_url: str, token: str, params: Dict[str, str]) -> List[Dict]:
    resp = requests.get(
        f"{bus_url.rstrip('/')}/bus/messages",
        headers=_bus_headers(token),
        params=params,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["messages"]


def run_roundtrip(bus_url: str, token: str, channel: str, worker_count: int) -> Dict:
    thread_id = f"mock-{uuid.uuid4().hex}"
    workers = [f"worker-{i + 1}" for i in range(worker_count)]
    plan = post_message(
        bus_url,
        token,
        {
            "channel": channel,
            "sender": "orchestrator",
            "kind": "plan",
            "thread_id": thread_id,
            "message": "Prioritize the first agent communication features.",
            "metadata": {
                "priority_order": [
                    "directed routing",
                    "thread correlation",
                    "acks and handoffs",
                    "presence",
                    "Slack or chat bridge",
                ]
            },
        },
    )

    tasks = []
    acknowledgements = []
    for index, worker in enumerate(workers, start=1):
        task = post_message(
            bus_url,
            token,
            {
                "channel": channel,
                "sender": "orchestrator",
                "target": worker,
                "kind": "task",
                "thread_id": thread_id,
                "message": f"Validate coordination feature #{index}.",
                "metadata": {"worker_index": index},
            },
        )
        tasks.append(task)

        inbox = get_messages(
            bus_url,
            token,
            {
                "channel": channel,
                "target": worker,
                "kind": "task",
                "thread_id": thread_id,
                "include_broadcast": "false",
            },
        )
        selected = next(message for message in inbox if message["id"] == task["id"])
        acknowledgements.append(
            post_message(
                bus_url,
                token,
                {
                    "channel": channel,
                    "sender": worker,
                    "target": "orchestrator",
                    "kind": "ack",
                    "thread_id": thread_id,
                    "reply_to": selected["id"],
                    "message": f"{worker} completed mock validation.",
                    "metadata": {"status": "done"},
                },
            )
        )

    observed_acks = get_messages(
        bus_url,
        token,
        {
            "channel": channel,
            "target": "orchestrator",
            "kind": "ack",
            "thread_id": thread_id,
            "include_broadcast": "false",
        },
    )
    return {
        "thread_id": thread_id,
        "plan": plan,
        "tasks": tasks,
        "acknowledgements": acknowledgements,
        "observed_ack_count": len(observed_acks),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Exercise orchestrator-to-worker messaging on the IAC Bus."
    )
    parser.add_argument("--bus-url", default="http://127.0.0.1:8091")
    parser.add_argument("--bus-token", default=os.environ.get("BUS_API_TOKEN", ""))
    parser.add_argument("--channel", default="coordination")
    parser.add_argument("--worker-count", type=int, default=2)
    args = parser.parse_args()

    if args.worker_count < 1:
        parser.error("--worker-count must be greater than 0")

    result = run_roundtrip(args.bus_url, args.bus_token, args.channel, args.worker_count)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
