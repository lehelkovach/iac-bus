"""Outbound A2A bridge against a stub A2A agent, with the real bus over HTTP."""

import importlib
import json
import sys
import threading
import time
from pathlib import Path

import pytest
import requests
from flask import Flask, jsonify, request
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import a2a_bridge  # noqa: E402
import a2a_interop as a2a  # noqa: E402

SHARED = {"Authorization": "Bearer shared-secret"}


class StubAgent:
    """An A2A 1.0 agent that asks one question, then finishes with an artifact."""

    def __init__(self):
        self.app = Flask("stub-agent")
        self.received = []
        self.tasks = {}
        self.app.add_url_rule("/.well-known/agent-card.json", "card", self.card)
        self.app.add_url_rule("/a2a", "rpc", self.rpc, methods=["POST"])
        self.server = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def card(self):
        return jsonify({
            "name": "Greeter", "description": "Writes greetings.", "version": "1.0.0",
            "supportedInterfaces": [{"url": f"{self.url}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
            "capabilities": {}, "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"],
            "skills": [{"id": "greet", "name": "Greet", "description": "Writes a greeting.", "tags": ["text"]}],
            "securitySchemes": {"bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}},
            "securityRequirements": [{"schemes": {"bearer": {"list": []}}}],
        })

    def _task(self, task_id):
        t = self.tasks[task_id]
        status = {"state": t["state"], "timestamp": a2a.iso(time.time())}
        if t["state"] == a2a.INPUT_REQUIRED:
            status["message"] = {"messageId": "q1", "role": "ROLE_AGENT", "parts": [{"text": "Formal or casual?"}]}
        task = {"id": task_id, "contextId": t["context"], "status": status}
        if t["state"] == a2a.COMPLETED:
            task["artifacts"] = [{"artifactId": "a1", "name": "greeting", "parts": [{"text": f"Hello ({t['answer']})"}]}]
        return task

    def rpc(self):
        if request.headers.get("Authorization") != "Bearer partner-secret":
            return jsonify({"error": "unauthorized"}), 401
        if request.headers.get("A2A-Version") != "1.0":
            return jsonify({"jsonrpc": "2.0", "id": None, "error": {"code": -32009, "message": "version"}})
        body = request.get_json()
        self.received.append(body)
        params = body["params"]
        if body["method"] == "SendMessage":
            msg = params["message"]
            if msg.get("taskId"):
                t = self.tasks[msg["taskId"]]
                t["answer"] = msg["parts"][0]["text"]
                t["state"] = a2a.WORKING
                return jsonify({"jsonrpc": "2.0", "id": body["id"], "result": {"task": self._task(msg["taskId"])}})
            task_id = f"remote-{len(self.tasks) + 1}"
            self.tasks[task_id] = {"state": a2a.WORKING, "context": "ctx", "polls": 0, "answer": None}
            return jsonify({"jsonrpc": "2.0", "id": body["id"], "result": {"task": self._task(task_id)}})
        if body["method"] == "GetTask":
            t = self.tasks[params["id"]]
            t["polls"] += 1
            if t["answer"] is None:
                t["state"] = a2a.INPUT_REQUIRED
            else:
                t["state"] = a2a.COMPLETED
            return jsonify({"jsonrpc": "2.0", "id": body["id"], "result": self._task(params["id"])})
        if body["method"] == "CancelTask":
            self.tasks[params["id"]]["state"] = a2a.CANCELED
            return jsonify({"jsonrpc": "2.0", "id": body["id"], "result": self._task(params["id"])})
        return jsonify({"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "no"}})

    def close(self):
        self.server.shutdown()


@pytest.fixture
def stack(monkeypatch):
    monkeypatch.setenv("BUS_API_TOKEN", "shared-secret")
    monkeypatch.setenv("BUS_A2A_ALLOW_HOSTS", "127.0.0.1")
    monkeypatch.delenv("IAC_BUS_STORE", raising=False)
    if "server" in sys.modules:
        del sys.modules["server"]
    import server  # noqa: F401
    srv = importlib.reload(sys.modules["server"])
    srv._bus_messages.clear()
    bus = make_server("127.0.0.1", 0, srv.app, threaded=True)
    threading.Thread(target=bus.serve_forever, daemon=True).start()
    stub = StubAgent()
    session = requests.Session()
    session.trust_env = False  # localhost only; never route test traffic through a proxy
    yield {"bus_url": f"http://127.0.0.1:{bus.server_port}", "stub": stub, "session": session, "server": srv}
    stub.close()
    bus.shutdown()


def _register(stack):
    resp = stack["session"].post(stack["bus_url"] + "/agents/a2a/register", headers=SHARED, json={
        "card_url": stack["stub"].url + "/.well-known/agent-card.json", "handle": "greeter",
        "credential_env": "PARTNER_AGENT_TOKEN", "grants": {"skills": [], "queues": [], "channels": [], "lock_paths": []}})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _queue_work(stack, queue, lock_paths):
    resp = stack["session"].post(stack["bus_url"] + "/bus/messages", headers=SHARED, json={
        "channel": "supervisor", "queue": queue, "sender": "lead", "type": "task", "conversation_id": "conv-9",
        "message": {"instructions": "Write a greeting for the release notes.", "lock_paths": lock_paths}})
    assert resp.status_code == 201
    return resp.json()["message"]["id"]


def _answer_decisions(stack, answer, stop):
    since = ""
    while not stop.is_set():
        params = {"channel": "decisions", "limit": 200, "wait_seconds": 1}
        if since:
            params["since_id"] = since
        msgs = stack["session"].get(stack["bus_url"] + "/bus/messages", params=params, headers=SHARED).json()["messages"]
        for msg in msgs:
            since = msg["id"]
            if msg["type"] == "request_decision":
                stack["session"].post(stack["bus_url"] + "/bus/messages", headers=SHARED, json={
                    "channel": "decisions", "type": "provide_decision", "sender": "lehel",
                    "conversation_id": msg["conversation_id"],
                    "message": {"kind": "response", "action": "provide_decision",
                                "conversation_id": msg["conversation_id"], "payload": answer}})


def test_bus_effect_mapping():
    assert a2a_bridge.bus_effect(a2a.COMPLETED) == "ack"
    assert a2a_bridge.bus_effect(a2a.FAILED) == "nack"
    assert a2a_bridge.bus_effect(a2a.REJECTED) == "nack"
    assert a2a_bridge.bus_effect(a2a.CANCELED) == "ack-canceled"
    assert a2a_bridge.bus_effect(a2a.INPUT_REQUIRED) == "ask"
    assert a2a_bridge.bus_effect(a2a.AUTH_REQUIRED) == "ask"
    assert a2a_bridge.bus_effect(a2a.WORKING) == "wait"
    assert a2a_bridge.bus_effect(a2a.SUBMITTED) == "wait"


def test_bridge_relays_work_question_and_result(stack):
    reg = _register(stack)
    work_id = _queue_work(stack, reg["outbound_queue"], ["docs/release-notes.md"])
    stop = threading.Event()
    threading.Thread(target=_answer_decisions, args=(stack, {"decision": "formal", "approved": True}, stop),
                     daemon=True).start()
    bridge = a2a_bridge.Bridge(stack["bus_url"], "shared-secret", poll=0.05, decision_wait=10,
                               session=stack["session"], env={"PARTNER_AGENT_TOKEN": "partner-secret"}).start()
    try:
        assert bridge.process_once() == 1
    finally:
        stop.set()

    sent = [b for b in stack["stub"].received if b["method"] == "SendMessage"]
    first, reply = sent[0]["params"]["message"], sent[1]["params"]["message"]
    assert first["parts"][0]["text"] == "Write a greeting for the release notes."
    assert first["metadata"]["iacBus"]["workMessageId"] == work_id
    assert set(first["metadata"]["iacBus"]["fencingTokens"]) == {"docs/release-notes.md"}
    assert reply["taskId"] == "remote-1" and reply["parts"][0]["text"] == "formal"

    get = lambda path, **p: stack["session"].get(stack["bus_url"] + path, params=p, headers=SHARED).json()  # noqa: E731
    report = get("/bus/messages", channel="a2a.greeter")["messages"][-1]
    assert report["type"] == "report_result" and report["reply_to"] == work_id
    assert report["message"]["outcome"] == "completed"
    assert report["message"]["result"]["text"] == "Hello (formal)"
    assert get("/health")["queue_pending_count"] == 0 and get("/health")["queue_leased_count"] == 0
    assert get("/bus/locks")["count"] == 0
    lock_events = [m["type"] for m in get("/bus/messages", channel="locks")["messages"]]
    assert lock_events == ["lock.acquired", "lock.released"]
    asked = [m for m in get("/bus/messages", channel="decisions")["messages"] if m["type"] == "request_decision"]
    assert "Formal or casual?" in asked[0]["message"]["payload"]["prompt"]


def test_bridge_requeues_when_the_path_is_locked(stack):
    reg = _register(stack)
    held = stack["session"].post(stack["bus_url"] + "/bus/locks/acquire", headers=SHARED,
                                 json={"resource": "docs", "agent": "someone-editing", "ttl_seconds": 60}).json()
    assert held["lock_id"]
    _queue_work(stack, reg["outbound_queue"], ["docs/release-notes.md"])
    bridge = a2a_bridge.Bridge(stack["bus_url"], "shared-secret", poll=0.05, session=stack["session"],
                               env={"PARTNER_AGENT_TOKEN": "partner-secret"}).start()
    bridge._lock = lambda paths, handle: ({}, paths[0])  # skip the 30 s lock wait; the bus refusal path is tested in test_locks
    item = stack["session"].post(stack["bus_url"] + "/bus/queues/claim", headers={"Authorization": "Bearer " + bridge.bus.token},
                                 json={"queue": reg["outbound_queue"], "worker": "x"}).json()["message"]
    assert bridge.handle_item({"handle": "greeter", "outbound_queue": reg["outbound_queue"], "credential_env": None,
                               "card": {"endpoint": stack["stub"].url + "/a2a"}}, item) == "requeued"
    assert stack["stub"].received == []
    health = stack["session"].get(stack["bus_url"] + "/health").json()
    assert health["queue_pending_count"] == 1


def test_inbound_task_can_be_routed_to_an_external_agent(stack):
    """An A2A caller hands the bus a task; the bus routes it to the registered external agent and back."""
    reg = _register(stack)
    caller = stack["session"].post(stack["bus_url"] + "/agents/register", headers=SHARED,
                                   json={"handle": "upstream"}).json()
    rpc = lambda method, params: stack["session"].post(  # noqa: E731
        stack["bus_url"] + "/a2a", headers={"Authorization": "Bearer " + caller["run_token"], "A2A-Version": "1.0"},
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})).json()
    task = rpc("SendMessage", {"message": {"messageId": "u1", "role": "ROLE_USER", "parts": [{"text": "Greet the team"}]},
                               "configuration": {"returnImmediately": True},
                               "metadata": {"queue": reg["outbound_queue"]}})["result"]["task"]
    stop = threading.Event()
    threading.Thread(target=_answer_decisions, args=(stack, {"decision": "casual"}, stop), daemon=True).start()
    bridge = a2a_bridge.Bridge(stack["bus_url"], "shared-secret", poll=0.05, decision_wait=10,
                               session=stack["session"], env={"PARTNER_AGENT_TOKEN": "partner-secret"}).start()
    try:
        assert bridge.process_once() == 1
    finally:
        stop.set()
    done = rpc("GetTask", {"id": task["id"]})["result"]
    assert done["status"]["state"] == a2a.COMPLETED
    assert done["artifacts"][0]["parts"][0]["data"]["text"] == "Hello (casual)"
