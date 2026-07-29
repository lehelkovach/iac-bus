import threading
import time

from conftest import load_server


def test_post_without_auth_allowed(monkeypatch):
    server = load_server(monkeypatch, token="")
    client = server.app.test_client()

    resp = client.post("/bus/messages", json={"message": "hello"})
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["message"]["message"] == "hello"


def test_post_requires_auth(monkeypatch):
    server = load_server(monkeypatch, token="secret")
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
    server = load_server(monkeypatch, token="")
    client = server.app.test_client()

    resp = client.post("/bus/messages", json={"channel": "ops", "message": "one"})
    first_id = resp.get_json()["message"]["id"]
    client.post("/bus/messages", json={"channel": "ops", "message": "two"})

    resp = client.get(f"/bus/messages?channel=ops&since_id={first_id}")
    data = resp.get_json()
    assert len(data["messages"]) == 1
    assert data["messages"][0]["message"] == "two"


def test_queue_claim_and_ack(monkeypatch):
    server = load_server(monkeypatch, token="")
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


def test_queue_no_double_lease(monkeypatch):
    server = load_server(monkeypatch, token="")
    client = server.app.test_client()

    client.post("/bus/messages", json={"queue": "work", "message": "only-one"})
    first = client.post(
        "/bus/queues/claim",
        json={"queue": "work", "worker": "agent-a", "lease_seconds": 60},
    )
    assert first.status_code == 200
    second = client.post(
        "/bus/queues/claim",
        json={"queue": "work", "worker": "agent-b", "lease_seconds": 60},
    )
    assert second.status_code == 204


def test_queue_lease_expires(monkeypatch):
    server = load_server(monkeypatch, token="")
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
    server = load_server(monkeypatch, token="")
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


def test_wait_seconds_returns_early_on_new_message(monkeypatch):
    server = load_server(monkeypatch, token="")
    results = {}

    def waiter():
        started = time.time()
        # Exercise store long-poll (HTTP test client is not thread-safe).
        msgs = server._store.wait_for_messages(
            channel="ops", since_id="", include_queue=False, limit=50, wait_seconds=5
        )
        results["elapsed"] = time.time() - started
        results["msgs"] = msgs

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.15)
    server._bus_add_message(
        {
            "protocol": "iac-bus/1.0",
            "channel": "ops",
            "sender": "tester",
            "message": "late",
            "type": "event",
            "queue": "",
            "priority": 0,
        }
    )
    thread.join(timeout=6)
    assert results["elapsed"] < 3.0
    assert len(results["msgs"]) == 1
    assert results["msgs"][0]["message"] == "late"

    # HTTP path accepts wait_seconds and returns quickly when messages exist
    client = server.app.test_client()
    resp = client.get("/bus/messages?channel=ops&wait_seconds=2")
    assert resp.status_code == 200
    assert len(resp.get_json()["messages"]) == 1


def test_wait_seconds_times_out_empty(monkeypatch):
    server = load_server(monkeypatch, token="")
    client = server.app.test_client()
    started = time.time()
    resp = client.get("/bus/messages?channel=empty&wait_seconds=0.3")
    elapsed = time.time() - started
    assert resp.status_code == 200
    assert resp.get_json()["messages"] == []
    assert elapsed >= 0.25


def test_health_reports_version(monkeypatch):
    server = load_server(monkeypatch, token="")
    client = server.app.test_client()
    resp = client.get("/health")
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["version"] == "0.1.0-dev"
