"""A2A 1.0 interop: Agent Card, JSON-RPC endpoint, task mapping, registration and grants."""

import importlib
import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import a2a_interop as a2a  # noqa: E402
from mcp_server import _decision_answer  # noqa: E402

SHARED = {"Authorization": "Bearer shared-secret"}
PARTNER_CARD = {
    "name": "Partner Agent",
    "description": "Writes release notes.",
    "version": "2.1.0",
    "supportedInterfaces": [{"url": "https://partner.example/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
    "capabilities": {"streaming": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [{"id": "notes", "name": "Release notes", "description": "Drafts release notes.", "tags": ["docs"]}],
}


def _load_server(monkeypatch, **env):
    monkeypatch.setenv("BUS_API_TOKEN", "shared-secret")
    monkeypatch.delenv("IAC_BUS_STORE", raising=False)
    monkeypatch.delenv("BUS_REQUIRE_RUN_TOKEN", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    if "server" in sys.modules:
        del sys.modules["server"]
    import server  # noqa: F401
    srv = importlib.reload(sys.modules["server"])
    srv._bus_messages.clear()
    return srv


@pytest.fixture
def server(monkeypatch):
    return _load_server(monkeypatch)


@pytest.fixture
def client(server):
    return server.app.test_client()


def _run(client, handle, grants=None):
    body = {"handle": handle}
    if grants is not None:
        body["grants"] = grants
    data = client.post("/agents/register", json=body, headers=SHARED).get_json()
    return {"token": data["run_token"], "actor": data["actor"], "auth": {"Authorization": "Bearer " + data["run_token"]}}


def rpc(client, token, method, params=None, version="1.0", raw=None, req_id="r1"):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if version:
        headers["A2A-Version"] = version
    body = raw if raw is not None else json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
    return client.post("/a2a", data=body, headers=headers)


def user_message(text="Add a /ping route", **extra):
    msg = {"messageId": f"m-{time.time_ns()}", "role": "ROLE_USER", "parts": [{"text": text}]}
    msg.update(extra)
    return msg


def send(client, token, text="Add a /ping route", metadata=None, immediately=True, **msg_extra):
    params = {"message": user_message(text, **msg_extra), "configuration": {"returnImmediately": immediately}}
    if metadata:
        params["metadata"] = metadata
    return rpc(client, token, "SendMessage", params).get_json()


def _pb():
    pytest.importorskip("a2a.types")
    from a2a.types import a2a_pb2
    from google.protobuf import json_format
    return a2a_pb2, json_format


# --------------------------------------------------------------- pure helpers


def test_state_mapping_tables():
    assert a2a.state_from_step_status("pending") == a2a.SUBMITTED
    assert a2a.state_from_step_status("running") == a2a.WORKING
    assert a2a.state_from_step_status("completed") == a2a.COMPLETED
    assert a2a.state_from_step_status("failed") == a2a.FAILED
    assert a2a.state_from_step_status("timed_out") == a2a.FAILED
    assert a2a.state_from_step_status("skipped") == a2a.FAILED
    assert a2a.state_from_step_status("skipped", canceled=True) == a2a.CANCELED
    assert a2a.state_from_queue_status("pending") == a2a.SUBMITTED
    assert a2a.state_from_queue_status("leased") == a2a.WORKING
    assert a2a.state_from_queue_status(None) is None


def test_iso_timestamps_have_milliseconds():
    assert a2a.iso(0) == "1970-01-01T00:00:00.000Z"
    assert a2a.iso(1.5) == "1970-01-01T00:00:01.500Z"
    assert a2a.parse_iso("1970-01-01T00:00:01.500Z") == pytest.approx(1.5)


def test_decision_from_message():
    assert a2a.decision_from_message(user_message("approve")) == {"decision": "approve", "approved": True}
    assert a2a.decision_from_message(user_message("no"))["approved"] is False
    msg = {"messageId": "x", "role": "ROLE_USER", "parts": [{"data": {"choiceId": "b", "notes": "fine"}}]}
    assert a2a.decision_from_message(msg) == {"choice_id": "b", "notes": "fine"}


def test_fetch_guard():
    with pytest.raises(ValueError, match="https"):
        a2a.check_fetch_url("http://partner.example/card")
    with pytest.raises(ValueError, match="non-public"):
        a2a.check_fetch_url("https://127.0.0.1/card")
    with pytest.raises(ValueError, match="non-public"):
        a2a.check_fetch_url("https://10.0.1.10/card")
    assert a2a.check_fetch_url("http://localhost:9000/card", allow_hosts=["localhost"]) == ("http", "localhost")


def test_validate_card():
    assert a2a.validate_card(PARTNER_CARD) == []
    broken = dict(PARTNER_CARD, skills=[], supportedInterfaces=[{"url": "x"}])
    errors = a2a.validate_card(broken)
    assert any("skills" in e for e in errors) and any("protocolBinding" in e for e in errors)


# ------------------------------------------------------------------- the card


def test_agent_card_is_public_and_valid(monkeypatch):
    srv = _load_server(monkeypatch, BUS_PUBLIC_URL="https://bus.example.test", BUS_VERSION="0.3.0-dev")
    resp = srv.app.test_client().get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    assert "max-age" in resp.headers["Cache-Control"]
    card = resp.get_json()
    assert a2a.validate_card(card) == []
    assert card["supportedInterfaces"][0] == {"url": "https://bus.example.test/a2a", "protocolBinding": "JSONRPC",
                                              "protocolVersion": "1.0"}
    assert card["version"] == "0.3.0-dev"
    assert {s["id"] for s in card["skills"]} == {"enqueue-work", "run-job"}
    pb, json_format = _pb()
    json_format.ParseDict(card, pb.AgentCard())


# ------------------------------------------------------------ JSON-RPC layer


def test_auth_and_envelope_errors(client):
    agent = _run(client, "caller")
    assert client.post("/a2a", json={}).status_code == 401
    assert rpc(client, "shared-secret", "GetTask", {"id": "x"}).status_code == 403
    assert rpc(client, agent["token"], "", raw="{not json").get_json()["error"]["code"] == a2a.PARSE_ERROR
    assert rpc(client, agent["token"], "", raw='{"jsonrpc":"2.0","id":1}').get_json()["error"]["code"] == a2a.INVALID_REQUEST
    assert rpc(client, agent["token"], "", raw="[]").get_json()["error"]["code"] == a2a.INVALID_REQUEST
    assert rpc(client, agent["token"], "Nope").get_json()["error"]["code"] == a2a.METHOD_NOT_FOUND
    assert rpc(client, agent["token"], "SendStreamingMessage").get_json()["error"]["code"] == a2a.UNSUPPORTED_OPERATION
    assert rpc(client, agent["token"], "CreateTaskPushNotificationConfig").get_json()["error"]["code"] == a2a.PUSH_NOT_SUPPORTED
    assert rpc(client, agent["token"], "GetExtendedAgentCard").get_json()["error"]["code"] == a2a.EXTENDED_CARD_NOT_CONFIGURED
    resp = rpc(client, agent["token"], "GetTask", {"id": "x"}, version=None)
    error = resp.get_json()["error"]
    assert error["code"] == a2a.VERSION_NOT_SUPPORTED
    assert error["data"][0]["reason"] == "VERSION_NOT_SUPPORTED"
    assert resp.headers["A2A-Version"] == "1.0"
    bad = rpc(client, agent["token"], "SendMessage", {"message": {"role": "ROLE_USER", "parts": []}}).get_json()
    assert bad["error"]["code"] == a2a.INVALID_PARAMS
    raw_part = {"message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"raw": "aGk="}]}}
    assert rpc(client, agent["token"], "SendMessage", raw_part).get_json()["error"]["code"] == a2a.CONTENT_TYPE_NOT_SUPPORTED
    assert rpc(client, agent["token"], "GetTask", {"id": "nope"}).get_json()["error"]["code"] == a2a.TASK_NOT_FOUND


# ------------------------------------------------------------ task lifecycle


def test_enqueue_work_lifecycle(client):
    caller = _run(client, "partner")
    worker = _run(client, "worker")
    sent = send(client, caller["token"], "Add a /ping route", metadata={"queue": "coding", "lockPaths": ["src/app.py"]})
    task = sent["result"]["task"]
    assert task["status"]["state"] == a2a.SUBMITTED
    assert task["metadata"]["skill"] == "enqueue-work"

    claimed = client.post("/bus/queues/claim", json={"queue": "coding", "worker": "w"}, headers=worker["auth"]).get_json()
    item = claimed["message"]
    assert item["headers"]["a2a_task_id"] == task["id"]
    assert item["message"]["instructions"] == "Add a /ping route"
    assert item["message"]["lock_paths"] == ["src/app.py"]
    assert rpc(client, caller["token"], "GetTask", {"id": task["id"]}).get_json()["result"]["status"]["state"] == a2a.WORKING

    client.post("/bus/queues/ack", json={"queue": "coding", "message_id": item["id"], "worker": "w",
                                         "lease_id": item["lease_id"], "result": "Added GET /ping returning ok"},
                headers=worker["auth"])
    done = rpc(client, caller["token"], "GetTask", {"id": task["id"]}).get_json()["result"]
    assert done["status"]["state"] == a2a.COMPLETED
    assert done["artifacts"][0]["parts"] == [{"text": "Added GET /ping returning ok"}]
    assert done["history"][0]["parts"][0]["text"] == "Add a /ping route"

    pb, json_format = _pb()
    json_format.ParseDict(sent["result"], pb.SendMessageResponse())
    json_format.ParseDict(done, pb.Task())


def test_nack_without_requeue_fails_the_task(client):
    caller, worker = _run(client, "partner"), _run(client, "worker")
    task = send(client, caller["token"], metadata={"queue": "coding"})["result"]["task"]
    item = client.post("/bus/queues/claim", json={"queue": "coding", "worker": "w"}, headers=worker["auth"]).get_json()["message"]
    client.post("/bus/queues/nack", json={"queue": "coding", "message_id": item["id"], "worker": "w",
                                          "lease_id": item["lease_id"], "requeue": False, "reason": "out of scope"},
                headers=worker["auth"])
    result = rpc(client, caller["token"], "GetTask", {"id": task["id"]}).get_json()["result"]
    assert result["status"]["state"] == a2a.FAILED
    assert result["status"]["message"]["parts"][0]["text"] == "out of scope"


def test_blocking_send_returns_after_cap(server, client):
    server.BUS_WAIT_SECONDS_MAX = 1
    caller = _run(client, "partner")
    started = time.time()
    task = send(client, caller["token"], immediately=False)["result"]["task"]
    assert task["status"]["state"] == a2a.SUBMITTED
    assert 0.9 <= time.time() - started < 3


def test_blocking_send_returns_when_done(server, client):
    caller = _run(client, "partner")
    worker = _run(client, "worker")
    worker_client = server.app.test_client()

    def finish_soon():
        for _ in range(40):
            resp = worker_client.post("/bus/queues/claim", json={"queue": "a2a-inbound", "worker": "w"}, headers=worker["auth"])
            if resp.status_code == 200:
                item = resp.get_json()["message"]
                worker_client.post("/bus/queues/ack", json={"queue": "a2a-inbound", "message_id": item["id"], "worker": "w",
                                                            "lease_id": item["lease_id"], "result": {"files": ["x.py"]}},
                                   headers=worker["auth"])
                return
            time.sleep(0.05)

    threading.Thread(target=finish_soon).start()
    task = send(client, caller["token"], immediately=False)["result"]["task"]
    assert task["status"]["state"] == a2a.COMPLETED
    assert task["artifacts"][0]["parts"][0]["data"] == {"files": ["x.py"]}


def test_run_job_skill_follows_the_step(client):
    caller, worker = _run(client, "partner"), _run(client, "worker")
    task = send(client, caller["token"], "Bump the version", metadata={"skill": "run-job", "queue": "jobs"})["result"]["task"]
    item = client.post("/bus/queues/claim", json={"queue": "jobs", "worker": "w"}, headers=worker["auth"]).get_json()["message"]
    assert item["message"]["job_id"] == f"a2a-{task['id']}"

    def status(state, **extra):
        body = {"job_id": item["message"]["job_id"], "step_id": "work", "status": state}
        body.update(extra)
        client.post("/bus/messages", json={"type": "orchestration.step.status", "message": body}, headers=worker["auth"])
        return rpc(client, caller["token"], "GetTask", {"id": task["id"]}).get_json()["result"]

    assert status("running")["status"]["state"] == a2a.WORKING
    done = status("completed", result="0.2.1")
    assert done["status"]["state"] == a2a.COMPLETED
    assert done["artifacts"][0]["parts"] == [{"text": "0.2.1"}]


def test_input_required_round_trip(client):
    caller, worker = _run(client, "partner"), _run(client, "worker")
    task = send(client, caller["token"], metadata={"queue": "coding"})["result"]["task"]
    item = client.post("/bus/queues/claim", json={"queue": "coding", "worker": "w"}, headers=worker["auth"]).get_json()["message"]
    client.post("/bus/messages", headers=worker["auth"], json={
        "channel": "decisions", "type": "request_decision", "conversation_id": "conv-1",
        "headers": {"work_message_id": item["id"]},
        "message": {"kind": "control", "action": "request_decision", "conversation_id": "conv-1",
                    "payload": {"prompt": "Delete legacy.py?", "choices": [{"id": "yes", "label": "Yes"},
                                                                          {"id": "no", "label": "No"}]}}})
    waiting = rpc(client, caller["token"], "GetTask", {"id": task["id"]}).get_json()["result"]
    assert waiting["status"]["state"] == a2a.INPUT_REQUIRED
    assert "Delete legacy.py?" in waiting["status"]["message"]["parts"][0]["text"]

    answer = rpc(client, caller["token"], "SendMessage", {
        "message": user_message("no", taskId=task["id"]), "configuration": {"returnImmediately": True}}).get_json()
    assert answer["result"]["task"]["status"]["state"] == a2a.WORKING
    decisions = client.get("/bus/messages?channel=decisions", headers=SHARED).get_json()["messages"]
    reply = [m for m in decisions if m["type"] == "provide_decision"][-1]
    assert reply["conversation_id"] == "conv-1"
    assert reply["sender"] == caller["actor"]
    assert _decision_answer(reply, "conv-1") == {"decision": "no", "approved": False}


def test_decision_answered_on_the_bus_resumes_the_task(client):
    caller, worker = _run(client, "partner"), _run(client, "worker")
    task = send(client, caller["token"], metadata={"queue": "coding"})["result"]["task"]
    item = client.post("/bus/queues/claim", json={"queue": "coding", "worker": "w"}, headers=worker["auth"]).get_json()["message"]
    client.post("/bus/messages", headers=worker["auth"], json={
        "channel": "decisions", "type": "request_decision", "conversation_id": "conv-2",
        "message": {"kind": "control", "action": "request_decision", "conversation_id": "conv-2",
                    "payload": {"prompt": "Ship?", "metadata": {"work_message_id": item["id"]}}}})
    assert rpc(client, caller["token"], "GetTask", {"id": task["id"]}).get_json()["result"]["status"]["state"] == a2a.INPUT_REQUIRED
    client.post("/bus/messages", headers=SHARED, json={"channel": "decisions", "type": "provide_decision",
                                                       "conversation_id": "conv-2", "sender": "lehel",
                                                       "message": {"payload": {"approved": True}}})
    assert rpc(client, caller["token"], "GetTask", {"id": task["id"]}).get_json()["result"]["status"]["state"] == a2a.WORKING


def test_cancel(client):
    caller = _run(client, "partner")
    task = send(client, caller["token"], metadata={"queue": "coding"})["result"]["task"]
    canceled = rpc(client, caller["token"], "CancelTask", {"id": task["id"]}).get_json()["result"]
    assert canceled["status"]["state"] == a2a.CANCELED
    assert client.post("/bus/queues/claim", json={"queue": "coding", "worker": "w"}, headers=SHARED).status_code == 204
    again = rpc(client, caller["token"], "CancelTask", {"id": task["id"]}).get_json()
    assert again["error"]["code"] == a2a.TASK_NOT_CANCELABLE
    late = rpc(client, caller["token"], "SendMessage", {"message": user_message("more", taskId=task["id"])}).get_json()
    assert late["error"]["code"] == a2a.UNSUPPORTED_OPERATION
    notes = client.get(f"/bus/messages?channel=a2a.task.{task['id']}", headers=SHARED).get_json()["messages"]
    assert notes[-1]["type"] == "cancel_task"


def test_cancel_run_job_skips_the_step(client):
    caller = _run(client, "partner")
    task = send(client, caller["token"], metadata={"skill": "run-job", "queue": "jobs"})["result"]["task"]
    assert rpc(client, caller["token"], "CancelTask", {"id": task["id"]}).get_json()["result"]["status"]["state"] == a2a.CANCELED
    job = client.get(f"/bus/orchestration/jobs/a2a-{task['id']}", headers=SHARED).get_json()
    assert job["state"]["work"]["status"] == "skipped"
    assert client.post("/bus/queues/claim", json={"queue": "jobs", "worker": "w"}, headers=SHARED).status_code == 204


def test_tasks_are_scoped_to_their_owner_and_listable(client):
    alice, bob = _run(client, "alice"), _run(client, "bob")
    ids = [send(client, alice["token"], f"task {i}", metadata={"queue": "q"})["result"]["task"]["id"] for i in range(3)]
    other = send(client, bob["token"], "bob's", metadata={"queue": "q"})["result"]["task"]["id"]
    assert rpc(client, bob["token"], "GetTask", {"id": ids[0]}).get_json()["error"]["code"] == a2a.TASK_NOT_FOUND
    page = rpc(client, alice["token"], "ListTasks", {"pageSize": 2}).get_json()["result"]
    assert page["totalSize"] == 3 and len(page["tasks"]) == 2 and page["nextPageToken"]
    rest = rpc(client, alice["token"], "ListTasks", {"pageSize": 2, "pageToken": page["nextPageToken"]}).get_json()["result"]
    assert rest["nextPageToken"] == "" and len(rest["tasks"]) == 1
    seen = {t["id"] for t in page["tasks"] + rest["tasks"]}
    assert seen == set(ids) and other not in seen
    assert rpc(client, alice["token"], "ListTasks", {"status": a2a.COMPLETED}).get_json()["result"]["totalSize"] == 0
    assert rpc(client, alice["token"], "ListTasks", {"pageSize": 500}).get_json()["error"]["code"] == a2a.INVALID_PARAMS
    pb, json_format = _pb()
    json_format.ParseDict(page, pb.ListTasksResponse())


# ------------------------------------------------- registration and grants


GRANTS = {"skills": ["enqueue-work"], "queues": ["partner-*"], "channels": ["a2a.partner"], "lock_paths": ["docs"]}


def _register_partner(client, **extra):
    body = {"card": PARTNER_CARD, "grants": GRANTS, "credential_env": "PARTNER_AGENT_TOKEN"}
    body.update(extra)
    return client.post("/agents/a2a/register", json=body, headers=SHARED)


def test_register_by_inline_card(client):
    resp = _register_partner(client)
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["run_token"].startswith("iacrun_")
    assert data["agent"]["handle"] == "partner-agent"
    assert data["outbound_queue"] == "a2a.partner-agent"
    assert data["agent"]["card"]["endpoint"] == "https://partner.example/a2a"
    assert data["run"]["grants"] == GRANTS
    again = _register_partner(client)
    assert again.status_code == 200 and again.get_json()["updated"] is True
    assert again.get_json()["agent"]["agent_uuid"] == data["agent"]["agent_uuid"]
    listed = client.get("/agents/a2a", headers=SHARED).get_json()
    assert listed["count"] == 1 and listed["agents"][0]["credential_env"] == "PARTNER_AGENT_TOKEN"
    assert "run_token" not in json.dumps(listed)


def test_register_rejects_bad_input(client):
    bad_card = _register_partner(client, card=dict(PARTNER_CARD, skills=[])).get_json()
    assert any("skills" in d for d in bad_card["details"])
    ssrf = client.post("/agents/a2a/register", json={"card_url": "https://127.0.0.1/card"}, headers=SHARED).get_json()
    assert any("non-public" in d for d in ssrf["details"])
    plain = client.post("/agents/a2a/register", json={"card_url": "http://partner.example/card"}, headers=SHARED).get_json()
    assert any("https" in d for d in plain["details"])
    assert _register_partner(client, credential_env="sk-live-123").status_code == 400
    assert _register_partner(client, grants={"queues": "all"}).status_code == 400
    agent = _run(client, "someone")
    assert client.post("/agents/a2a/register", json={"card": PARTNER_CARD}, headers=agent["auth"]).status_code == 403


def test_grants_restrict_a2a_and_bus_calls(client):
    data = _register_partner(client).get_json()
    token, auth = data["run_token"], {"Authorization": "Bearer " + data["run_token"]}

    no_queue = send(client, token)["error"]
    assert no_queue["code"] == a2a.PERMISSION_DENIED and "set metadata.queue" in no_queue["message"]
    assert send(client, token, metadata={"queue": "partner-docs"})["result"]["task"]["metadata"]["queue"] == "partner-docs"
    assert send(client, token, metadata={"queue": "coding"})["error"]["code"] == a2a.PERMISSION_DENIED
    assert send(client, token, metadata={"skill": "run-job", "queue": "partner-x"})["error"]["code"] == a2a.PERMISSION_DENIED

    assert client.post("/bus/messages", json={"channel": "ops", "message": "hi"}, headers=auth).status_code == 403
    assert client.post("/bus/messages", json={"channel": "a2a.partner", "message": "hi"}, headers=auth).status_code == 201
    assert client.get("/bus/messages", headers=auth).status_code == 403
    assert client.get("/bus/messages?channel=a2a.partner", headers=auth).status_code == 200
    assert client.post("/bus/queues/claim", json={"queue": "coding", "worker": "x"}, headers=auth).status_code == 403
    assert client.post("/bus/locks/acquire", json={"resource": "src/app.py", "agent": "x"}, headers=auth).status_code == 403
    assert client.post("/bus/locks/acquire", json={"resource": "docs/notes.md", "agent": "x"}, headers=auth).status_code == 200
    assert client.get("/agents/runs", headers=auth).status_code == 403
    assert client.get("/agents/a2a", headers=auth).status_code == 403
    job = {"type": "orchestration.job", "channel": "a2a.partner", "message": {"job": {"job_id": "j", "steps": [{"id": "s"}]}}}
    assert client.post("/bus/messages", json=job, headers=auth).status_code == 403
    assert client.get("/metrics").get_json()["counters"]["grant_denials"] >= 7


def test_default_queue_comes_from_grants(client):
    data = _register_partner(client, grants=dict(GRANTS, queues=["partner-inbox", "partner-*"])).get_json()
    task = send(client, data["run_token"])["result"]["task"]
    assert task["metadata"]["queue"] == "partner-inbox"


def test_health_counts_tasks(client):
    caller = _run(client, "partner")
    send(client, caller["token"])
    health = client.get("/health").get_json()
    assert health["a2a_tasks"] == 1
    assert client.get("/metrics").get_json()["counters"]["a2a_tasks_created"] == 1


def test_registration_matches_schema(client):
    from jsonschema import Draft202012Validator

    doc = json.loads((ROOT / "schemas" / "iac-bus.schema.json").read_text(encoding="utf-8"))

    def validator(name):
        return Draft202012Validator({"$schema": doc["$schema"], "$ref": f"#/$defs/{name}", "$defs": doc["$defs"]})

    body = {"card": PARTNER_CARD, "grants": GRANTS, "credential_env": "PARTNER_AGENT_TOKEN"}
    validator("A2ARegisterRequest").validate(body)
    validator("A2ARegisterResponse").validate(client.post("/agents/a2a/register", json=body, headers=SHARED).get_json())
