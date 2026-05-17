import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_server(monkeypatch, token=""):
    monkeypatch.setenv("BUS_API_TOKEN", token)
    if "server" in sys.modules:
        del sys.modules["server"]
    import server  # noqa: F401
    return importlib.reload(sys.modules["server"])


def test_post_without_auth_allowed(monkeypatch):
    server = _load_server(monkeypatch, token="")
    server._bus_messages.clear()
    client = server.app.test_client()

    resp = client.post("/bus/messages", json={"message": "hello"})
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["message"]["message"] == "hello"


def test_post_requires_auth(monkeypatch):
    server = _load_server(monkeypatch, token="secret")
    server._bus_messages.clear()
    client = server.app.test_client()

    resp = client.post("/bus/messages", json={"message": "hello"})
    assert resp.status_code == 401

    resp = client.post(
        "/bus/messages",
        json={"message": "hello"},
        headers={"Authorization": "Bearer secret"}
    )
    assert resp.status_code == 201


def test_since_id_filters(monkeypatch):
    server = _load_server(monkeypatch, token="")
    server._bus_messages.clear()
    client = server.app.test_client()

    resp = client.post("/bus/messages", json={"channel": "ops", "message": "one"})
    first_id = resp.get_json()["message"]["id"]
    client.post("/bus/messages", json={"channel": "ops", "message": "two"})

    resp = client.get(f"/bus/messages?channel=ops&since_id={first_id}")
    data = resp.get_json()
    assert len(data["messages"]) == 1
    assert data["messages"][0]["message"] == "two"


def test_directed_threaded_mock_agent_roundtrip(monkeypatch):
    server = _load_server(monkeypatch, token="")
    server._bus_messages.clear()
    client = server.app.test_client()

    thread_id = "feature-priority-001"
    client.post(
        "/bus/messages",
        json={
            "channel": "coordination",
            "sender": "orchestrator",
            "kind": "plan",
            "thread_id": thread_id,
            "message": "prioritize agent communication features",
            "metadata": {
                "features": ["presence", "directed routing", "acks", "handoff docs"]
            },
        },
    )
    task_resp = client.post(
        "/bus/messages",
        json={
            "channel": "coordination",
            "sender": "orchestrator",
            "target": "worker-a",
            "kind": "task",
            "thread_id": thread_id,
            "message": "draft the first Cursor-agent coordination test",
        },
    )
    task_id = task_resp.get_json()["message"]["id"]

    worker_inbox = client.get(
        "/bus/messages"
        "?channel=coordination&target=worker-a&kind=task"
        f"&thread_id={thread_id}&include_broadcast=false"
    )
    worker_messages = worker_inbox.get_json()["messages"]
    assert len(worker_messages) == 1
    assert worker_messages[0]["target"] == "worker-a"
    assert worker_messages[0]["kind"] == "task"

    client.post(
        "/bus/messages",
        json={
            "channel": "coordination",
            "sender": "worker-a",
            "target": "orchestrator",
            "kind": "ack",
            "thread_id": thread_id,
            "reply_to": task_id,
            "message": "test scaffold drafted",
            "metadata": {"status": "done"},
        },
    )

    orchestrator_inbox = client.get(
        "/bus/messages"
        "?channel=coordination&target=orchestrator&kind=ack"
        f"&thread_id={thread_id}"
    )
    ack = orchestrator_inbox.get_json()["messages"][0]
    assert ack["sender"] == "worker-a"
    assert ack["reply_to"] == task_id
    assert ack["metadata"]["status"] == "done"


def test_target_filter_includes_broadcasts_by_default(monkeypatch):
    server = _load_server(monkeypatch, token="")
    server._bus_messages.clear()
    client = server.app.test_client()

    client.post(
        "/bus/messages",
        json={"channel": "ops", "sender": "orchestrator", "message": "all-hands"},
    )
    client.post(
        "/bus/messages",
        json={
            "channel": "ops",
            "sender": "orchestrator",
            "target": "worker-a",
            "message": "private task",
        },
    )

    resp = client.get("/bus/messages?channel=ops&target=worker-a")
    messages = resp.get_json()["messages"]
    assert [msg["message"] for msg in messages] == ["all-hands", "private task"]

    resp = client.get("/bus/messages?channel=ops&target=worker-a&include_broadcast=false")
    messages = resp.get_json()["messages"]
    assert [msg["message"] for msg in messages] == ["private task"]


def test_rejects_invalid_coordination_fields(monkeypatch):
    server = _load_server(monkeypatch, token="")
    server._bus_messages.clear()
    client = server.app.test_client()

    resp = client.post(
        "/bus/messages",
        json={"message": "bad metadata", "metadata": ["not", "an", "object"]},
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "metadata must be an object"

    resp = client.get("/bus/messages?limit=zero")
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "limit must be an integer"
