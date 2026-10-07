"""Opt-in SQLite store: IAC_BUS_STORE=sqlite + IAC_BUS_DB=<path>.

Each `_restart` tears the server module down and imports it again against the
same database file, which is what a process restart looks like to the store.
"""

import importlib
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _unload_server():
    mod = sys.modules.pop("server", None)
    if mod is not None and hasattr(mod, "_store"):
        mod._store.close()


def _load_server(monkeypatch, backend, db_path=None, token=""):
    monkeypatch.setenv("BUS_API_TOKEN", token)
    if backend is None:
        monkeypatch.delenv("IAC_BUS_STORE", raising=False)
    else:
        monkeypatch.setenv("IAC_BUS_STORE", backend)
    if db_path is None:
        monkeypatch.delenv("IAC_BUS_DB", raising=False)
    else:
        monkeypatch.setenv("IAC_BUS_DB", str(db_path))
    _unload_server()
    import server  # noqa: F401
    return importlib.reload(sys.modules["server"])


@pytest.fixture
def sqlite_server(monkeypatch, tmp_path):
    db_path = tmp_path / "data" / "bus.db"
    state = {"server": _load_server(monkeypatch, "sqlite", db_path)}

    def restart():
        state["server"] = _load_server(monkeypatch, "sqlite", db_path)
        return state["server"]

    state["restart"] = restart
    state["db_path"] = db_path
    yield state
    _unload_server()


def test_default_backend_is_memory_when_env_unset(monkeypatch):
    server = _load_server(monkeypatch, None)
    assert server._store.backend == "memory"
    assert server._store.durable is False
    # the memory store operates on the module-level containers tests clear
    assert server._store.messages is server._bus_messages
    assert server._store.agents is server._agents
    _unload_server()


def test_unknown_backend_fails_at_import(monkeypatch):
    monkeypatch.setenv("IAC_BUS_STORE", "redis")
    _unload_server()
    with pytest.raises(ValueError, match="IAC_BUS_STORE"):
        import server  # noqa: F401
    _unload_server()


def test_sqlite_backend_selected_and_db_created(sqlite_server):
    server = sqlite_server["server"]
    assert server._store.backend == "sqlite"
    assert server._store.durable is True
    assert sqlite_server["db_path"].exists()
    # memory containers are not the live state in sqlite mode
    assert server._bus_messages == []


def test_register_survives_restart(sqlite_server):
    server = sqlite_server["server"]
    client = server.app.test_client()

    resp = client.post(
        "/agents/register",
        json={"handle": "agent:cursor.iac-bus.0@web", "role": "worker", "purpose": "smoke"},
    )
    assert resp.status_code == 201
    agent = resp.get_json()["agent"]
    assert agent["ephemeral"] is False
    assert set(agent) == {
        "agent_uuid", "agent_handle", "role", "purpose", "status",
        "registered_at", "last_seen_at", "ephemeral",
    }

    server = sqlite_server["restart"]()
    stored = server._store.get_agent(agent["agent_uuid"])
    assert stored == agent
    assert server._store.agent_count() == 1
    assert [a["agent_handle"] for a in server._store.list_agents()] == ["agent:cursor.iac-bus.0@web"]


def test_publish_claim_ack_survive_reopen(sqlite_server):
    server = sqlite_server["server"]
    client = server.app.test_client()

    first = client.post(
        "/bus/messages", json={"queue": "work", "sender": "lead", "message": {"task": "a"}}
    ).get_json()["message"]
    second = client.post(
        "/bus/messages", json={"queue": "work", "sender": "lead", "message": {"task": "b"}}
    ).get_json()["message"]

    claim = client.post(
        "/bus/queues/claim", json={"queue": "work", "worker": "agent-a", "lease_seconds": 300}
    )
    assert claim.status_code == 200
    claimed = claim.get_json()["message"]
    assert claimed["id"] == first["id"]
    lease_id = claimed["lease_id"]

    # restart: the lease is still held by agent-a, b is still pending
    server = sqlite_server["restart"]()
    client = server.app.test_client()
    listed = client.get("/bus/messages?include_queue=true").get_json()["messages"]
    assert [(m["id"], m["status"], m["leased_by"]) for m in listed] == [
        (first["id"], "leased", "agent-a"),
        (second["id"], "pending", ""),
    ]
    assert listed[0]["lease_id"] == lease_id
    assert listed[0]["message"] == {"task": "a"}
    health = client.get("/health").get_json()
    assert health["queue_pending_count"] == 1
    assert health["queue_leased_count"] == 1

    # the lease minted before the restart still acks
    wrong = client.post(
        "/bus/queues/ack",
        json={"queue": "work", "message_id": first["id"], "worker": "agent-b", "lease_id": lease_id},
    )
    assert wrong.status_code == 409
    ack = client.post(
        "/bus/queues/ack",
        json={"queue": "work", "message_id": first["id"], "worker": "agent-a", "lease_id": lease_id},
    )
    assert ack.status_code == 200
    assert ack.get_json()["message"]["id"] == first["id"]

    # restart again: the ack is durable, only b remains and it is claimable
    server = sqlite_server["restart"]()
    client = server.app.test_client()
    listed = client.get("/bus/messages?include_queue=true").get_json()["messages"]
    assert [m["id"] for m in listed] == [second["id"]]
    again = client.post(
        "/bus/queues/ack",
        json={"queue": "work", "message_id": first["id"], "worker": "agent-a", "lease_id": lease_id},
    )
    assert again.status_code == 404
    claim = client.post("/bus/queues/claim", json={"queue": "work", "worker": "agent-c"})
    assert claim.status_code == 200
    assert claim.get_json()["message"]["id"] == second["id"]
    nack = client.post(
        "/bus/queues/nack",
        json={
            "queue": "work",
            "message_id": second["id"],
            "worker": "agent-c",
            "lease_id": claim.get_json()["message"]["lease_id"],
        },
    )
    assert nack.status_code == 200
    assert nack.get_json()["message"]["status"] == "pending"
    empty = client.post("/bus/queues/claim", json={"queue": "other", "worker": "agent-c"})
    assert empty.status_code == 204


def test_published_messages_and_since_id_survive_reopen(sqlite_server):
    server = sqlite_server["server"]
    client = server.app.test_client()
    one = client.post(
        "/bus/messages",
        json={"channel": "ops", "message": "one", "headers": {"k": "v"}, "conversation_id": "c1"},
    ).get_json()["message"]
    client.post("/bus/messages", json={"channel": "ops", "message": "two"})
    client.post("/bus/messages", json={"channel": "other", "message": "three"})

    server = sqlite_server["restart"]()
    client = server.app.test_client()
    all_ops = client.get("/bus/messages?channel=ops").get_json()["messages"]
    assert [m["message"] for m in all_ops] == ["one", "two"]
    assert all_ops[0] == one  # exact round-trip, optional keys included
    assert "lease_until" in all_ops[0] and all_ops[0]["lease_until"] == 0
    after = client.get(f"/bus/messages?channel=ops&since_id={one['id']}").get_json()["messages"]
    assert [m["message"] for m in after] == ["two"]
    # since_id anchored on a message outside the channel filter is ignored
    unfiltered = client.get(f"/bus/messages?channel=other&since_id={one['id']}").get_json()["messages"]
    assert [m["message"] for m in unfiltered] == ["three"]
    limited = client.get("/bus/messages?limit=1").get_json()["messages"]
    assert [m["message"] for m in limited] == ["three"]


def test_sqlite_lease_expires_and_ttl_prunes(sqlite_server, monkeypatch):
    server = sqlite_server["server"]
    client = server.app.test_client()
    now = {"t": 1000}
    monkeypatch.setattr(server.time, "time", lambda: now["t"])

    client.post("/bus/messages", json={"queue": "work", "message": "task"})
    client.post("/bus/messages", json={"channel": "ops", "message": "short", "ttl_seconds": 3})
    first = client.post(
        "/bus/queues/claim", json={"queue": "work", "worker": "agent-a", "lease_seconds": 5}
    ).get_json()["message"]

    now["t"] = 1010
    second = client.post(
        "/bus/queues/claim", json={"queue": "work", "worker": "agent-b", "lease_seconds": 5}
    )
    assert second.status_code == 200
    assert second.get_json()["message"]["leased_by"] == "agent-b"
    assert second.get_json()["message"]["lease_id"] != first["lease_id"]
    assert client.get("/bus/messages?channel=ops").get_json()["messages"] == []

    monkeypatch.setattr(server, "BUS_MAX_MESSAGES", 2)
    for i in range(4):
        client.post("/bus/messages", json={"channel": "cap", "message": str(i)})
    assert [m["message"] for m in client.get("/bus/messages?channel=cap").get_json()["messages"]] == ["2", "3"]
    assert server._store.message_count() == 2


def test_sqlite_long_poll_returns_early(sqlite_server):
    server = sqlite_server["server"]
    client = server.app.test_client()
    result = {}

    def poller():
        started = time.time()
        resp = server.app.test_client().get("/bus/messages?channel=wait&wait_seconds=5")
        result["elapsed"] = time.time() - started
        result["body"] = resp.get_json()

    thread = threading.Thread(target=poller)
    thread.start()
    time.sleep(0.3)
    client.post("/bus/messages", json={"channel": "wait", "message": "ping"})
    thread.join(timeout=6)
    assert not thread.is_alive()
    assert [m["message"] for m in result["body"]["messages"]] == ["ping"]
    assert result["elapsed"] < 3.0
