"""Run tokens: the bus derives the caller from its token (identity rungs ID1-ID2)."""

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runs import MemoryRunTable, hash_token  # noqa: E402

SHARED = {"Authorization": "Bearer shared-secret"}


class FakeClock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def _load_server(monkeypatch, token="shared-secret", strict=False):
    monkeypatch.setenv("BUS_API_TOKEN", token)
    monkeypatch.setenv("BUS_REQUIRE_RUN_TOKEN", "1" if strict else "")
    monkeypatch.delenv("IAC_BUS_STORE", raising=False)
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


def _register(client, handle="claude.repo.a", headers=SHARED, **extra):
    body = {"handle": handle, "role": "coding-agent", "medium": "ide"}
    body.update(extra)
    return client.post("/agents/register", json=body, headers=headers)


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ table


def test_table_stores_only_token_hash():
    table = MemoryRunTable()
    run, token = table.create("h", "u", 60)
    assert token.startswith("iacrun_")
    assert "token_hash" not in run
    stored = table._runs[run["run_id"]]
    assert stored["token_hash"] == hash_token(token)
    assert token not in str(stored)


def test_table_sliding_expiry_and_end():
    clock = FakeClock()
    table = MemoryRunTable(clock=clock)
    run, token = table.create("h", "u", 60)
    clock.now += 50
    assert table.authenticate(token)[0] is not None  # slides to now + 60
    clock.now += 50
    assert table.authenticate(token)[0] is not None
    clock.now += 61
    assert table.authenticate(token) == (None, "run expired")
    run2, token2 = table.create("h", "u", 60)
    table.end(run2["run_id"])
    assert table.authenticate(token2) == (None, "run ended")
    assert table.authenticate("iacrun_nope") == (None, "unknown run token")


# --------------------------------------------------------------- register


def test_register_returns_run_token_and_actor(client):
    resp = _register(client)
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["run_token"].startswith("iacrun_")
    assert data["actor"] == f"claude.repo.a#{data['run']['run_id'][:6]}"
    assert data["agent"]["agent_handle"] == "claude.repo.a"
    assert "token_hash" not in data["run"]


def test_register_validation(client):
    assert _register(client, handle="has#hash").status_code == 400
    assert _register(client, handle="has space").status_code == 400
    assert _register(client, medium="pigeon").status_code == 400
    assert _register(client, run_ttl_seconds=0).status_code == 400
    assert _register(client, headers={}).status_code == 401


def test_run_token_cannot_register(client):
    token = _register(client).get_json()["run_token"]
    assert _register(client, headers=_bearer(token)).status_code == 403


def test_two_registrations_get_distinct_actors(client):
    a = _register(client, handle="same").get_json()["actor"]
    b = _register(client, handle="same").get_json()["actor"]
    assert a != b and a.startswith("same#") and b.startswith("same#")


# ------------------------------------------------------- derived identity


def test_bus_derives_sender_worker_and_agent_from_token(client):
    reg = _register(client).get_json()
    auth, actor = _bearer(reg["run_token"]), reg["actor"]

    msg = client.post("/bus/messages", json={"channel": "ops", "sender": "supervisor", "message": "do it"},
                      headers=auth).get_json()["message"]
    assert msg["sender"] == actor

    lock = client.post("/bus/locks/acquire", json={"resource": "src/app.py", "agent": "someone-else"},
                       headers=auth).get_json()
    assert lock["agent"] == actor

    client.post("/bus/messages", json={"channel": "q", "queue": "work", "message": "task"}, headers=SHARED)
    claimed = client.post("/bus/queues/claim", json={"queue": "work", "worker": "fake"}, headers=auth).get_json()
    assert claimed["message"]["leased_by"] == actor

    whoami = client.get("/agents/whoami", headers=auth).get_json()
    assert whoami == {"auth": "run", "actor": actor, "run": whoami["run"]}


def test_one_run_cannot_release_anothers_lock(client):
    a = _register(client, handle="a").get_json()
    b = _register(client, handle="b").get_json()
    lock = client.post("/bus/locks/acquire", json={"resource": "src/app.py", "agent": "x"},
                       headers=_bearer(a["run_token"])).get_json()
    # B knows A's lock id and actor name and claims to be A: the bus ignores the claim.
    resp = client.post("/bus/locks/release", json={"lock_id": lock["lock_id"], "agent": a["actor"]},
                       headers=_bearer(b["run_token"]))
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "lock owner mismatch"
    resp = client.post("/bus/locks/release", json={"lock_id": lock["lock_id"], "agent": "anything"},
                       headers=_bearer(a["run_token"]))
    assert resp.status_code == 200


def test_shared_token_keeps_claimed_names(client):
    msg = client.post("/bus/messages", json={"channel": "ops", "sender": "legacy-agent", "message": "hi"},
                      headers=SHARED).get_json()["message"]
    assert msg["sender"] == "legacy-agent"
    assert client.get("/agents/whoami", headers=SHARED).get_json()["auth"] == "shared"


def test_bad_or_dead_run_tokens_are_401(server, client):
    assert client.get("/bus/locks", headers=_bearer("iacrun_forged")).status_code == 401

    reg = _register(client).get_json()
    auth = _bearer(reg["run_token"])
    assert client.post("/agents/runs/end", json={}, headers=auth).status_code == 200
    resp = client.get("/bus/locks", headers=auth)
    assert resp.status_code == 401
    assert resp.get_json()["reason"] == "run ended"

    clock = FakeClock()
    server._store.runs._clock = clock
    reg = _register(client, run_ttl_seconds=60).get_json()
    clock.now += 61
    resp = client.get("/bus/locks", headers=_bearer(reg["run_token"]))
    assert resp.status_code == 401
    assert resp.get_json()["reason"] == "run expired"


def test_run_tokens_bind_identity_on_an_open_bus(monkeypatch):
    srv = _load_server(monkeypatch, token="")
    client = srv.app.test_client()
    reg = _register(client, headers={}).get_json()
    msg = client.post("/bus/messages", json={"channel": "ops", "sender": "spoof", "message": "x"},
                      headers=_bearer(reg["run_token"])).get_json()["message"]
    assert msg["sender"] == reg["actor"]


# --------------------------------------------------------- run lifecycle


def test_heartbeat_extends_and_needs_run_token(server, client):
    clock = FakeClock()
    server._store.runs._clock = clock
    reg = _register(client, run_ttl_seconds=100).get_json()
    clock.now += 90
    hb = client.post("/agents/heartbeat", headers=_bearer(reg["run_token"]))
    assert hb.status_code == 200
    assert hb.get_json()["expires_at"] == pytest.approx(clock.now + 100)
    assert client.post("/agents/heartbeat", headers=SHARED).status_code == 400


def test_ending_a_run_releases_its_locks(client):
    reg = _register(client).get_json()
    auth = _bearer(reg["run_token"])
    client.post("/bus/locks/acquire", json={"resource": "src", "agent": "x"}, headers=auth)
    client.post("/bus/locks/acquire", json={"resource": "docs/a.md", "agent": "x"}, headers=auth)
    other = _register(client, handle="other").get_json()
    client.post("/bus/locks/acquire", json={"resource": "lib", "agent": "x"}, headers=_bearer(other["run_token"]))

    ended = client.post("/agents/runs/end", json={}, headers=auth).get_json()
    assert sorted(ended["locks_released"]) == ["docs/a.md", "src"]
    remaining = client.get("/bus/locks", headers=SHARED).get_json()["locks"]
    assert [lock["agent"] for lock in remaining] == [other["actor"]]
    types = [m["type"] for m in client.get("/bus/messages?channel=locks", headers=SHARED).get_json()["messages"]]
    assert types.count("lock.released") == 2


def test_operator_can_end_a_run_but_runs_cannot_end_others(client):
    a = _register(client, handle="a").get_json()
    b = _register(client, handle="b").get_json()
    resp = client.post("/agents/runs/end", json={"run_id": a["run"]["run_id"]}, headers=_bearer(b["run_token"]))
    assert resp.status_code == 403
    assert client.post("/agents/runs/end", json={"run_id": a["run"]["run_id"]}, headers=SHARED).status_code == 200
    assert client.post("/agents/runs/end", json={"run_id": a["run"]["run_id"]}, headers=SHARED).status_code == 404
    runs = client.get("/agents/runs", headers=SHARED).get_json()
    assert [r["actor"] for r in runs["runs"]] == [b["actor"]]
    all_runs = client.get("/agents/runs?include_ended=true", headers=SHARED).get_json()
    assert all_runs["count"] == 2
    assert all("token_hash" not in r for r in all_runs["runs"])


# ------------------------------------------------------------ strict mode


def test_strict_mode_requires_run_tokens_on_bus_routes(monkeypatch):
    srv = _load_server(monkeypatch, strict=True)
    client = srv.app.test_client()
    resp = client.get("/bus/locks", headers=SHARED)
    assert resp.status_code == 403
    assert "run token required" in resp.get_json()["error"]
    reg = _register(client).get_json()
    assert client.get("/bus/locks", headers=_bearer(reg["run_token"])).status_code == 200
    assert client.get("/agents/runs", headers=SHARED).status_code == 200
    health = client.get("/health").get_json()
    assert health["require_run_token"] is True
    assert health["runs_active"] == 1


def test_metrics_count_runs(client):
    reg = _register(client).get_json()
    client.post("/agents/runs/end", json={}, headers=_bearer(reg["run_token"]))
    counters = client.get("/metrics").get_json()["counters"]
    assert counters["runs_registered"] == 1
    assert counters["runs_ended"] == 1


def _validator(name):
    import json

    from jsonschema import Draft202012Validator

    doc = json.loads((ROOT / "schemas" / "iac-bus.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator({"$schema": doc["$schema"], "$ref": f"#/$defs/{name}", "$defs": doc["$defs"]})


def test_run_responses_match_schema(client):
    body = {"handle": "claude.repo.a", "role": "coding-agent", "medium": "ide", "run_ttl_seconds": 600}
    _validator("AgentRegisterRequest").validate(body)
    reg = client.post("/agents/register", json=body, headers=SHARED).get_json()
    _validator("AgentRegisterResponse").validate(reg)
    _validator("Whoami").validate(client.get("/agents/whoami", headers=_bearer(reg["run_token"])).get_json())
    _validator("Whoami").validate(client.get("/agents/whoami", headers=SHARED).get_json())
    _validator("RunList").validate(client.get("/agents/runs", headers=SHARED).get_json())
