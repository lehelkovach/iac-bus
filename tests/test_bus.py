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


def test_queue_claim_and_ack(monkeypatch):
    server = _load_server(monkeypatch, token="")
    server._bus_messages.clear()
    client = server.app.test_client()

    resp = client.post(
        "/bus/messages",
        json={"queue": "work", "sender": "lead", "message": {"task": "a"}}
    )
    msg_id = resp.get_json()["message"]["id"]

    claim = client.post(
        "/bus/queues/claim",
        json={"queue": "work", "worker": "agent-a", "lease_seconds": 30}
    )
    assert claim.status_code == 200
    claimed = claim.get_json()["message"]
    assert claimed["id"] == msg_id
    assert claimed["status"] == "leased"
    lease_id = claimed["lease_id"]
    assert lease_id

    ack = client.post(
        "/bus/queues/ack",
        json={
            "queue": "work",
            "message_id": msg_id,
            "worker": "agent-a",
            "lease_id": lease_id,
        },
    )
    assert ack.status_code == 200

    claim = client.post(
        "/bus/queues/claim",
        json={"queue": "work", "worker": "agent-a"},
    )
    assert claim.status_code == 204


def test_queue_lease_expires(monkeypatch):
    server = _load_server(monkeypatch, token="")
    server._bus_messages.clear()
    client = server.app.test_client()

    now = {"t": 1000}
    monkeypatch.setattr(server.time, "time", lambda: now["t"])

    client.post("/bus/messages", json={"queue": "work", "message": "task"})
    claim = client.post(
        "/bus/queues/claim",
        json={"queue": "work", "worker": "agent-a", "lease_seconds": 5},
    )
    first = claim.get_json()["message"]
    first_lease_id = first["lease_id"]

    now["t"] = 1010
    claim = client.post(
        "/bus/queues/claim",
        json={"queue": "work", "worker": "agent-b", "lease_seconds": 5},
    )
    assert claim.status_code == 200
    second = claim.get_json()["message"]
    assert second["leased_by"] == "agent-b"
    assert second["lease_id"] != first_lease_id


def test_poll_excludes_queue_by_default(monkeypatch):
    server = _load_server(monkeypatch, token="")
    server._bus_messages.clear()
    client = server.app.test_client()

    client.post("/bus/messages", json={"message": "broadcast"})
    client.post("/bus/messages", json={"queue": "work", "message": "task"})

    resp = client.get("/bus/messages")
    data = resp.get_json()
    assert len(data["messages"]) == 1
    assert data["messages"][0]["message"] == "broadcast"

    resp = client.get("/bus/messages?include_queue=true")
    data = resp.get_json()
    assert len(data["messages"]) == 2
