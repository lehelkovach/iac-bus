import importlib
import sys
import threading
import time
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


def test_health_exposes_ops_gauges(monkeypatch):
    server = _load_server(monkeypatch, token="")
    server._bus_messages.clear()
    server._jobs.clear()
    client = server.app.test_client()

    client.post("/bus/messages", json={"channel": "ops", "message": "hello"})
    client.post(
        "/bus/messages",
        json={"queue": "work", "message": "task"},
    )
    claim = client.post(
        "/bus/queues/claim",
        json={"queue": "work", "worker": "agent-a", "lease_seconds": 30},
    )
    assert claim.status_code == 200

    client.post(
        "/bus/messages",
        json={
            "type": "orchestration.job",
            "message": {
                "job": {
                    "job_id": "health-job",
                    "steps": [{"id": "a"}],
                }
            },
        },
    )

    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ok"
    assert data["messages_retained"] >= 2
    assert data["queue_pending_count"] >= 0
    assert data["queue_leased_count"] >= 1
    assert data["jobs_active"] >= 1
    assert "uptime_seconds" in data
    assert data["uptime_seconds"] >= 0
    assert "version" in data
    assert "process_rss_bytes" in data


def test_metrics_increment_on_post_claim_ack(monkeypatch):
    server = _load_server(monkeypatch, token="")
    server._bus_messages.clear()
    with server._metrics_lock:
        for key in (
            "messages_posted",
            "messages_polled",
            "queue_claims",
            "queue_acks",
            "queue_nacks",
            "orchestration_jobs",
            "orchestration_dispatches",
        ):
            server._metrics[key] = 0
        for key in ("post_latency_ms", "poll_latency_ms", "claim_latency_ms"):
            server._metrics[key].clear()
    client = server.app.test_client()

    post = client.post(
        "/bus/messages",
        json={"queue": "work", "message": "task"},
    )
    assert post.status_code == 201
    msg_id = post.get_json()["message"]["id"]

    claim = client.post(
        "/bus/queues/claim",
        json={"queue": "work", "worker": "agent-a", "lease_seconds": 30},
    )
    lease_id = claim.get_json()["message"]["lease_id"]

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

    client.get("/bus/messages")

    metrics = client.get("/metrics").get_json()
    assert metrics["counters"]["messages_posted"] >= 1
    assert metrics["counters"]["queue_claims"] == 1
    assert metrics["counters"]["queue_acks"] == 1
    assert metrics["counters"]["messages_polled"] >= 1
    assert metrics["gauges"]["messages_in_memory"] >= 0
    assert metrics["timers"]["post_latency_ms_avg"] is not None
    assert metrics["timers"]["claim_latency_ms_avg"] is not None


def test_wait_seconds_timeout_returns_empty(monkeypatch):
    server = _load_server(monkeypatch, token="")
    server._bus_messages.clear()
    client = server.app.test_client()

    started = time.time()
    resp = client.get("/bus/messages?channel=empty-wait&wait_seconds=1")
    elapsed = time.time() - started
    assert resp.status_code == 200
    assert resp.get_json()["messages"] == []
    assert 0.8 <= elapsed < 2.5


def test_wait_seconds_returns_early_on_new_message(monkeypatch):
    server = _load_server(monkeypatch, token="")
    server._bus_messages.clear()
    client = server.app.test_client()
    channel = "wait-early"
    result = {}

    def poller():
        poll_client = server.app.test_client()
        started = time.time()
        resp = poll_client.get(f"/bus/messages?channel={channel}&wait_seconds=5")
        result["elapsed"] = time.time() - started
        result["status"] = resp.status_code
        result["body"] = resp.get_json()

    thread = threading.Thread(target=poller)
    thread.start()
    time.sleep(0.3)
    client.post("/bus/messages", json={"channel": channel, "message": "ping"})
    thread.join(timeout=6)
    assert not thread.is_alive()
    assert result["status"] == 200
    assert len(result["body"]["messages"]) == 1
    assert result["body"]["messages"][0]["message"] == "ping"
    assert result["elapsed"] < 3.0


def test_agents_register_stub(monkeypatch):
    server = _load_server(monkeypatch, token="")
    server._agents.clear()
    client = server.app.test_client()

    resp = client.post(
        "/agents/register",
        json={"handle": "agent:cursor.iac-bus.0@web", "role": "worker", "purpose": "smoke"},
    )
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["success"] is True
    assert data["agent"]["ephemeral"] is True
    assert data["agent"]["agent_handle"] == "agent:cursor.iac-bus.0@web"
    assert data["agent"]["agent_uuid"]
