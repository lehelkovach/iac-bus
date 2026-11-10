#!/usr/bin/env python3
"""Outbound A2A bridge: bus work for registered external agents -> A2A -> back to the bus.

Every A2A agent registered with POST /agents/a2a/register gets an outbound
queue, ``a2a.<handle>``. Supervisors (or the bus's own A2A inbound side) put
work there. This bridge claims each item and:

1. locks the repo paths the item declares (``lock_paths``) as the bridge's
   own run, because external agents do not speak the bus's MCP tools;
2. sends the work with SendMessage (``returnImmediately``) to the agent's
   JSON-RPC endpoint from its card, with ``A2A-Version: 1.0`` and the token
   from the env var named at registration (``credential_env``), passing the
   fencing tokens in ``metadata.iacBus``;
3. follows the remote task: TASK_STATE_INPUT_REQUIRED / AUTH_REQUIRED become
   a bus ``request_decision`` (so a supervisor or human answers it like any
   other decision), and the answer goes back with SendMessage on the taskId;
4. maps the end state back (docs/A2A_MAPPING.md §3.3): COMPLETED -> ack with
   the artifacts as result; FAILED / REJECTED -> nack(requeue=false) with the
   reason; CANCELED -> ack with ``{"canceled": true}``; a refused lock ->
   nack(requeue=true) to retry later. It also posts a ``report_result``
   message on channel ``a2a.<handle>``; and releases its locks.

The bridge renews the item's lease while the remote task runs, so a task may
outlive ``--lease``.

  BUS_URL=https://bus.example BUS_API_TOKEN=... PARTNER_AGENT_TOKEN=... \\
    iac-bus-bridge                          # loop
  iac-bus-bridge --once                     # drain what is queued, then exit
  (or: python scripts/a2a_bridge.py ... from a checkout)
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from a2a_interop import (  # noqa: E402
    AUTH_REQUIRED,
    CANCELED,
    COMPLETED,
    FAILED,
    INPUT_REQUIRED,
    REJECTED,
    ROLE_USER,
    TERMINAL,
    A2AClient,
    JsonRpcError,
)

DECISIONS = "decisions"


def bus_effect(state: str) -> str:
    """Remote A2A task state -> what the bridge does on the bus (mapping §3.3)."""
    if state == COMPLETED:
        return "ack"
    if state in (FAILED, REJECTED):
        return "nack"
    if state == CANCELED:
        return "ack-canceled"
    if state in (INPUT_REQUIRED, AUTH_REQUIRED):
        return "ask"
    return "wait"


def text_of(parts: List[Dict[str, Any]]) -> str:
    return "\n".join(p["text"] for p in parts or [] if isinstance(p.get("text"), str))


class BusAPI:
    def __init__(self, url: str, token: str, session: Optional[requests.Session] = None):
        self.url = url.rstrip("/")
        self.session = session or requests.Session()
        self.token = token

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def get(self, path: str, **params: Any) -> Tuple[int, Any]:
        resp = self.session.get(self.url + path, params=params, headers=self._headers(), timeout=60)
        return resp.status_code, (resp.json() if resp.content else None)

    def post(self, path: str, body: Dict[str, Any]) -> Tuple[int, Any]:
        resp = self.session.post(self.url + path, json=body, headers=self._headers(), timeout=60)
        return resp.status_code, (resp.json() if resp.content else None)


class Bridge:
    def __init__(self, bus_url: str, shared_token: str, handle: str = "a2a-bridge", poll: float = 2.0,
                 lease: int = 900, decision_wait: float = 600.0, task_timeout: float = 3600.0,
                 session: Optional[requests.Session] = None, env: Optional[Dict[str, str]] = None):
        self.shared = BusAPI(bus_url, shared_token, session)
        self.bus = BusAPI(bus_url, shared_token, session)
        self.handle = handle
        self.poll = poll
        self.lease = lease
        self.decision_wait = decision_wait
        self.task_timeout = task_timeout
        self.session = session
        self.env = os.environ if env is None else env
        self.actor = handle
        self.log: List[str] = []

    def say(self, line: str) -> None:
        self.log.append(line)
        print(f"[bridge] {line}", flush=True)

    def start(self) -> "Bridge":
        status, data = self.shared.post("/agents/register", {"handle": self.handle, "role": "a2a-bridge",
                                                             "medium": "automation", "run_ttl_seconds": 86400})
        if status != 201 or not data.get("run_token"):
            raise SystemExit(f"could not register the bridge's run: {status} {data}")
        self.bus.token = data["run_token"]
        self.actor = data["actor"]
        self.say(f"running as {self.actor}")
        return self

    def stop(self) -> None:
        self.bus.post("/agents/runs/end", {})

    # ------------------------------------------------------------ loop

    def agents(self) -> List[Dict[str, Any]]:
        status, data = self.bus.get("/agents/a2a")
        if status != 200:
            raise RuntimeError(f"GET /agents/a2a -> {status} {data}")
        return [a for a in data["agents"] if a.get("card", {}).get("endpoint")]

    def process_once(self) -> int:
        done = 0
        for agent in self.agents():
            while True:
                status, data = self.bus.post("/bus/queues/claim", {"queue": agent["outbound_queue"], "worker": self.actor,
                                                                   "lease_seconds": self.lease})
                if status != 200:
                    break
                self.handle_item(agent, data["message"])
                done += 1
        return done

    def run_forever(self) -> None:
        while True:
            if not self.process_once():
                time.sleep(self.poll)

    # ------------------------------------------------------------ one item

    def handle_item(self, agent: Dict[str, Any], item: Dict[str, Any]) -> str:
        work = item.get("message")
        work = work if isinstance(work, dict) else {"instructions": str(work)}
        text = work.get("instructions") or work.get("text") or ""
        lock_paths = [p for p in work.get("lock_paths") or [] if isinstance(p, str)]
        self.say(f"{agent['handle']}: claimed {item['id']} ({text[:60]!r})")

        stop = threading.Event()
        threading.Thread(target=self._keepalive, args=(agent, item, stop), daemon=True).start()
        held, refused = self._lock(lock_paths, agent["handle"])
        if refused:
            stop.set()
            self._release(held)
            self._finish(agent, item, "nack", requeue=True, reason=f"path busy: {refused}")
            return "requeued"
        try:
            token = self.env.get(agent["credential_env"] or "", "") if agent.get("credential_env") else ""
            client = A2AClient(agent["card"]["endpoint"], token=token, session=self.session)
            parts: List[Dict[str, Any]] = [{"text": text}] if text else []
            parts += [{"data": d, "mediaType": "application/json"} for d in work.get("data") or []]
            message = {"messageId": uuid.uuid4().hex, "role": ROLE_USER, "parts": parts or [{"text": "(no instructions)"}],
                       "metadata": {"iacBus": {"workMessageId": item["id"], "lockPaths": lock_paths,
                                               "fencingTokens": {p: t for p, (_, t) in held.items()}}}}
            outcome = self._drive(agent, client, item, message)
        except JsonRpcError as exc:
            outcome = ("nack", {"reason": f"A2A error {exc.code}: {exc.message}"})
        except requests.RequestException as exc:
            outcome = ("nack-retry", {"reason": f"agent unreachable: {exc}"})
        finally:
            stop.set()
            self._release(held)
        effect, info = outcome
        if effect == "ack":
            self._finish(agent, item, "ack", result=info)
        elif effect == "nack-retry":
            self._finish(agent, item, "nack", requeue=True, reason=info["reason"])
        else:
            self._finish(agent, item, "nack", requeue=False, reason=info["reason"])
        return effect

    def _drive(self, agent: Dict[str, Any], client: A2AClient, item: Dict[str, Any],
               message: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        response = client.send_message(message, return_immediately=True)
        if "message" in response:
            return "ack", {"text": text_of(response["message"].get("parts", [])), "a2a": "message"}
        task = response["task"]
        self.say(f"{agent['handle']}: remote task {task['id']} {task['status']['state']}")
        deadline = time.time() + self.task_timeout
        while True:
            state = task["status"]["state"]
            effect = bus_effect(state)
            if effect == "ask":
                answer = self._ask(agent, item, task)
                if answer is None:
                    try:
                        client.cancel_task(task["id"])
                    except JsonRpcError:
                        pass
                    return "nack", {"reason": "no decision arrived for the agent's question"}
                reply = {"messageId": uuid.uuid4().hex, "role": ROLE_USER, "taskId": task["id"],
                         "contextId": task.get("contextId"),
                         "parts": [{"text": str(answer.get("decision") or answer.get("choice_id") or answer.get("notes") or "")},
                                   {"data": answer, "mediaType": "application/json"}]}
                task = client.send_message(reply, return_immediately=True)["task"]
                continue
            if state in TERMINAL:
                self.say(f"{agent['handle']}: remote task {task['id']} {state}")
                if effect == "ack":
                    artifacts = task.get("artifacts") or []
                    return "ack", {"a2a_task_id": task["id"], "artifacts": artifacts,
                                   "text": "\n".join(text_of(a.get("parts", [])) for a in artifacts)}
                if effect == "ack-canceled":
                    return "ack", {"a2a_task_id": task["id"], "canceled": True}
                reason = text_of((task["status"].get("message") or {}).get("parts", [])) or state
                return "nack", {"reason": f"{state}: {reason}"}
            if time.time() > deadline:
                return "nack", {"reason": "remote task did not finish in time"}
            time.sleep(self.poll)
            task = client.get_task(task["id"])

    def _ask(self, agent: Dict[str, Any], item: Dict[str, Any], task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        question = text_of((task["status"].get("message") or {}).get("parts", [])) or "The agent needs input."
        conversation_id = uuid.uuid4().hex
        self.say(f"{agent['handle']}: asks {question!r}; relaying as a bus decision")
        self.bus.post("/bus/messages", {
            "channel": DECISIONS, "type": "request_decision", "conversation_id": conversation_id,
            "headers": {"work_message_id": item["id"]},
            "message": {"kind": "control", "action": "request_decision", "from": self.actor,
                        "conversation_id": conversation_id, "state": "waiting_input",
                        "payload": {"prompt": f"[{agent['handle']}] {question}",
                                    "metadata": {"work_message_id": item["id"], "a2a_task_id": task["id"]}}},
        })
        since = ""
        deadline = time.time() + self.decision_wait
        while time.time() < deadline:
            params: Dict[str, Any] = {"channel": DECISIONS, "limit": 200, "wait_seconds": min(20, max(1, deadline - time.time()))}
            if since:
                params["since_id"] = since
            status, data = self.bus.get("/bus/messages", **params)
            for msg in (data or {}).get("messages", []) if status == 200 else []:
                since = msg["id"]
                body = msg.get("message") if isinstance(msg.get("message"), dict) else {}
                if msg.get("type") == "provide_decision" and (msg.get("conversation_id") or body.get("conversation_id")) == conversation_id:
                    payload = body.get("payload") if isinstance(body.get("payload"), dict) else body
                    self.say(f"{agent['handle']}: decision from {msg.get('sender')}: {payload}")
                    return payload
        return None

    # ----------------------------------------------------------- bus side

    def _keepalive(self, agent: Dict[str, Any], item: Dict[str, Any], stop: threading.Event) -> None:
        while not stop.wait(max(1.0, self.lease / 3.0)):
            try:
                self.bus.post("/bus/queues/renew", {"queue": agent["outbound_queue"], "message_id": item["id"],
                                                    "worker": self.actor, "lease_id": item["lease_id"],
                                                    "lease_seconds": self.lease})
            except requests.RequestException:
                pass

    def _lock(self, paths: List[str], agent_handle: str) -> Tuple[Dict[str, Tuple[str, int]], str]:
        held: Dict[str, Tuple[str, int]] = {}
        for path in paths:
            status, data = self.bus.post("/bus/locks/acquire", {"resource": path, "agent": self.actor, "ttl_seconds": 3600,
                                                                "wait_seconds": 30, "reason": f"A2A agent {agent_handle}"})
            if status != 200:
                return held, path
            held[path] = (data["lock_id"], data["fencing_token"])
        return held, ""

    def _release(self, held: Dict[str, Tuple[str, int]]) -> None:
        for lock_id, _token in held.values():
            self.bus.post("/bus/locks/release", {"lock_id": lock_id, "agent": self.actor})
        held.clear()

    def _finish(self, agent: Dict[str, Any], item: Dict[str, Any], verb: str, requeue: bool = False,
                reason: str = "", result: Any = None) -> None:
        body: Dict[str, Any] = {"queue": agent["outbound_queue"], "message_id": item["id"], "worker": self.actor,
                                "lease_id": item["lease_id"]}
        if verb == "ack":
            body["result"] = result
        else:
            body.update(requeue=requeue, reason=reason)
        self.bus.post(f"/bus/queues/{verb}", body)
        if verb == "ack" or not requeue:
            self.bus.post("/bus/messages", {
                "channel": f"a2a.{agent['handle']}", "type": "report_result", "reply_to": item["id"],
                "conversation_id": item.get("conversation_id"),
                "message": {"agent": agent["handle"], "work_message_id": item["id"],
                            "outcome": "completed" if verb == "ack" else "failed", "result": result, "reason": reason},
            })
        self.say(f"{agent['handle']}: {verb}{' (requeue)' if requeue else ''} {item['id']}"
                 + (f": {reason}" if reason else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="process what is queued now, then exit")
    ap.add_argument("--handle", default=os.environ.get("A2A_BRIDGE_HANDLE", "a2a-bridge"))
    ap.add_argument("--poll", type=float, default=2.0)
    ap.add_argument("--lease", type=int, default=900)
    args = ap.parse_args()
    bus_url = os.environ.get("BUS_URL", "").strip()
    if not bus_url:
        raise SystemExit("set BUS_URL (and BUS_API_TOKEN)")
    bridge = Bridge(bus_url, os.environ.get("BUS_API_TOKEN", ""), handle=args.handle, poll=args.poll, lease=args.lease).start()
    try:
        if args.once:
            print(f"processed {bridge.process_once()} item(s)")
        else:
            bridge.run_forever()
    finally:
        bridge.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
