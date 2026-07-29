"""Tests for agent registry, heartbeat, and repo/path locks."""

import time

from conftest import load_server


def test_agent_register_and_heartbeat(monkeypatch):
    server = load_server(monkeypatch, token="")
    client = server.app.test_client()

    resp = client.post(
        "/agents/register",
        json={
            "brand": "Cursor",
            "repo_locale": "iac-bus",
            "ordinal_path": "0",
            "role": "Master",
            "medium": "API",
        },
    )
    assert resp.status_code == 201
    created = resp.get_json()
    assert created["created"] is True
    assert created["logical_handle"] == "agent:cursor.iac-bus.0"
    agent_uuid = created["agent_uuid"]

    # Idempotent re-register
    resp = client.post(
        "/agents/register",
        json={
            "brand": "cursor",
            "repo_locale": "iac-bus",
            "ordinal_path": "0",
            "role": "master",
            "medium": "api",
        },
    )
    assert resp.status_code == 200
    assert resp.get_json()["created"] is False
    assert resp.get_json()["agent_uuid"] == agent_uuid

    hb = client.post(
        "/agents/heartbeat",
        json={"agent_uuid": agent_uuid, "medium": "api"},
    )
    assert hb.status_code == 200
    assert hb.get_json()["success"] is True

    get_resp = client.get(f"/agents/{agent_uuid}")
    assert get_resp.status_code == 200
    assert get_resp.get_json()["agent"]["role"] == "master"


def test_agent_register_parent_required(monkeypatch):
    server = load_server(monkeypatch, token="")
    client = server.app.test_client()
    resp = client.post(
        "/agents/register",
        json={
            "brand": "cursor",
            "repo_locale": "iac-bus",
            "ordinal_path": "0-1",
            "role": "worker",
            "medium": "api",
        },
    )
    assert resp.status_code == 400


def test_agent_register_conflict_parent(monkeypatch):
    server = load_server(monkeypatch, token="")
    client = server.app.test_client()
    master = client.post(
        "/agents/register",
        json={
            "brand": "cursor",
            "repo_locale": "iac-bus",
            "ordinal_path": "0",
            "role": "master",
            "medium": "api",
        },
    ).get_json()
    child = client.post(
        "/agents/register",
        json={
            "brand": "cursor",
            "repo_locale": "iac-bus",
            "ordinal_path": "0-1",
            "role": "worker",
            "medium": "api",
            "parent_agent_uuid": master["agent_uuid"],
        },
    )
    assert child.status_code == 201

    other = client.post(
        "/agents/register",
        json={
            "brand": "cursor",
            "repo_locale": "iac-bus",
            "ordinal_path": "0-2",
            "role": "worker",
            "medium": "api",
            "parent_agent_uuid": master["agent_uuid"],
        },
    ).get_json()

    conflict = client.post(
        "/agents/register",
        json={
            "brand": "cursor",
            "repo_locale": "iac-bus",
            "ordinal_path": "0-1",
            "role": "worker",
            "medium": "api",
            "parent_agent_uuid": other["agent_uuid"],
        },
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["conflict_code"] == "HANDLE_PARENT_MISMATCH"


def test_lock_acquire_renew_release(monkeypatch):
    server = load_server(monkeypatch, token="")
    client = server.app.test_client()
    resource = "repo:iac-bus/path:server.py"

    resp = client.post(
        "/bus/locks/acquire",
        json={"resource_key": resource, "holder": "agent-a", "lease_seconds": 30},
    )
    assert resp.status_code == 201
    lock = resp.get_json()["lock"]
    assert lock["holder"] == "agent-a"
    token = lock["fencing_token"]

    conflict = client.post(
        "/bus/locks/acquire",
        json={"resource_key": resource, "holder": "agent-b", "lease_seconds": 30},
    )
    assert conflict.status_code == 409

    renew = client.post(
        "/bus/locks/renew",
        json={
            "resource_key": resource,
            "holder": "agent-a",
            "fencing_token": token,
            "lease_seconds": 30,
        },
    )
    assert renew.status_code == 200

    bad = client.post(
        "/bus/locks/release",
        json={
            "resource_key": resource,
            "holder": "agent-a",
            "fencing_token": token + 1,
        },
    )
    assert bad.status_code == 409

    release = client.post(
        "/bus/locks/release",
        json={
            "resource_key": resource,
            "holder": "agent-a",
            "fencing_token": token,
        },
    )
    assert release.status_code == 200

    # After release, other holder can acquire
    again = client.post(
        "/bus/locks/acquire",
        json={"resource_key": resource, "holder": "agent-b", "lease_seconds": 30},
    )
    assert again.status_code == 201
    assert again.get_json()["lock"]["fencing_token"] != token


def test_lock_expires_and_can_be_taken_over(monkeypatch):
    server = load_server(monkeypatch, token="")
    client = server.app.test_client()
    resource = "repo:iac-bus/path:store.py"

    now = {"t": 5000.0}
    monkeypatch.setattr(server.time, "time", lambda: now["t"])
    # store.acquire_lock uses time.time from store module — patch both
    import store as store_mod

    monkeypatch.setattr(store_mod.time, "time", lambda: now["t"])

    first = client.post(
        "/bus/locks/acquire",
        json={"resource_key": resource, "holder": "agent-a", "lease_seconds": 5},
    )
    assert first.status_code == 201

    now["t"] = 5010.0
    second = client.post(
        "/bus/locks/acquire",
        json={"resource_key": resource, "holder": "agent-b", "lease_seconds": 5},
    )
    assert second.status_code == 201
    assert second.get_json()["lock"]["holder"] == "agent-b"


def test_messages_survive_store_roundtrip(monkeypatch, tmp_path):
    db = str(tmp_path / "bus.db")
    monkeypatch.setenv("BUS_API_TOKEN", "")
    monkeypatch.setenv("BUS_DB_PATH", db)

    import importlib
    import sys

    for name in ("server", "store"):
        if name in sys.modules:
            del sys.modules[name]
    import server

    server = importlib.reload(sys.modules["server"])
    server.reset_store(db)
    client = server.app.test_client()
    resp = client.post("/bus/messages", json={"channel": "ops", "message": "persist-me"})
    msg_id = resp.get_json()["message"]["id"]

    # Re-open same DB path
    server.reset_store(db)
    client = server.app.test_client()
    msgs = client.get("/bus/messages?channel=ops").get_json()["messages"]
    assert any(m["id"] == msg_id and m["message"] == "persist-me" for m in msgs)
