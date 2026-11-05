"""Agent runner: claims bus work, starts an agent with its own run, acks with the agent's result."""

import importlib
import json
import sys
import threading
from pathlib import Path

import pytest
import requests
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import agent_runner  # noqa: E402

SHARED = {"Authorization": "Bearer shared-secret"}
FAKE = f"{sys.executable} {ROOT / 'tests' / 'fixtures' / 'fake_claude.py'}"


@pytest.fixture
def bus(monkeypatch, tmp_path):
    monkeypatch.setenv("BUS_API_TOKEN", "shared-secret")
    monkeypatch.delenv("IAC_BUS_STORE", raising=False)
    if "server" in sys.modules:
        del sys.modules["server"]
    import server  # noqa: F401
    srv = importlib.reload(sys.modules["server"])
    srv._bus_messages.clear()
    httpd = make_server("127.0.0.1", 0, srv.app, threaded=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    session = requests.Session()
    session.trust_env = False
    workdir = tmp_path / "repo"
    workdir.mkdir()
    yield {"url": f"http://127.0.0.1:{httpd.server_port}", "session": session, "workdir": workdir}
    httpd.shutdown()


def _runner(bus, **kw):
    return agent_runner.Runner(bus["url"], "shared-secret", ["repo-tasks"], bus["workdir"], repo="demo",
                               agent_cmd=FAKE, session=bus["session"], **kw).start()


def _get(bus, path, token="shared-secret", **params):
    return bus["session"].get(bus["url"] + path, params=params, headers={"Authorization": "Bearer " + token})


def _enqueue(bus, instructions="Add src/x.py"):
    resp = bus["session"].post(bus["url"] + "/bus/messages", headers=SHARED, json={
        "channel": "work", "queue": "repo-tasks", "sender": "lead", "message": {"instructions": instructions}})
    return resp.json()["message"]["id"]


def test_prompt_carries_task_and_rules():
    item = {"id": "m1", "message": {"instructions": "Fix the login bug", "lock_paths": ["src/auth"],
                                    "a2a": {"task_id": "t1"}}}
    prompt = agent_runner.build_prompt(item, "repo-tasks")
    assert "Fix the login bug" in prompt and "src/auth" in prompt and "over A2A" in prompt
    assert "do not call claim_work" in prompt
    assert agent_runner.work_text({"id": "x", "message": "plain"})["text"] == "plain"
    assert agent_runner.parse_claude_output('noise\n{"result": "ok"}\n') == {"result": "ok"}


def test_runner_starts_agent_with_its_own_run_and_acks(bus):
    item_id = _enqueue(bus)
    runner = _runner(bus)
    assert runner.process_once() == 1
    record = runner.history[0]
    assert record["outcome"] == "completed"
    summary = json.loads(record["result"])
    assert summary["wrote"] == "src/x.py" and (bus["workdir"] / "src" / "x.py").exists()
    assert summary["lock_holder"] == record["agent"] and record["agent"].startswith("runner.agent#")
    assert summary["work_item"] == item_id
    assert summary["shared_token_leaked"] is False and summary["prompt_has_rules"] is True
    # the agent's run was ended: its lock is gone and the queue is empty
    assert _get(bus, "/bus/locks").json()["count"] == 0
    assert _get(bus, "/health").json()["queue_pending_count"] == 0
    states = [m["message"]["state"] for m in _get(bus, "/bus/messages", channel="status").json()["messages"]]
    assert states == ["running", "completed"]
    runner.stop()


def test_failed_agent_nacks_without_requeue(bus, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "fail")
    _enqueue(bus)
    runner = _runner(bus)
    runner.process_once()
    assert runner.history[0]["outcome"] == "failed"
    assert "simulated crash" in runner.history[0]["result"]
    health = _get(bus, "/health").json()
    assert health["queue_pending_count"] == 0 and health["queue_leased_count"] == 0


def test_a2a_task_is_done_by_a_started_agent(bus):
    caller = bus["session"].post(bus["url"] + "/agents/register", headers=SHARED, json={"handle": "partner"}).json()

    def rpc(method, params):
        return bus["session"].post(bus["url"] + "/a2a", data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}),
            headers={"Authorization": "Bearer " + caller["run_token"], "A2A-Version": "1.0"}).json()

    task = rpc("SendMessage", {"message": {"messageId": "u1", "role": "ROLE_USER", "parts": [{"text": "Add src/x.py"}]},
                               "configuration": {"returnImmediately": True},
                               "metadata": {"queue": "repo-tasks"}})["result"]["task"]
    _runner(bus).process_once()
    done = rpc("GetTask", {"id": task["id"]})["result"]
    assert done["status"]["state"] == "TASK_STATE_COMPLETED"
    assert json.loads(done["artifacts"][0]["parts"][0]["text"])["wrote"] == "src/x.py"


def test_max_items(bus):
    for _ in range(3):
        _enqueue(bus)
    runner = _runner(bus)
    assert runner.process_once(max_items=2) == 2
    assert _get(bus, "/health").json()["queue_pending_count"] == 1
